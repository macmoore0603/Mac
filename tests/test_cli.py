"""Tests for the command-line surface.

`cli.py` is not just presentation: it owns state persistence, and the threshold
it loads and saves is the input to every sizing decision. A bug that silently
resets a threshold would hand back drawdown room the account does not have, so
the round-trip is tested directly rather than assumed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from nqcopilot.apex import APEX_50K_INTRADAY, AccountState
from nqcopilot.bars import ET
from nqcopilot.cli import (
    Palette,
    build_parser,
    directive_to_dict,
    load_state,
    main,
    render,
    save_state,
)
from nqcopilot.contracts import MNQ
from nqcopilot.data import generate_demo_bars
from nqcopilot.playbook import evaluate

PLAIN = Palette(enabled=False)


def _args(*argv: str):
    return build_parser().parse_args(list(argv))


class TestArgumentParsing:
    def test_defaults_are_conservative(self):
        args = _args("--demo")
        assert args.symbol == "MNQ"          # micros, not minis
        assert args.profile == "apex50k-pa"
        assert args.style == "conservative"
        # Dollar limits are derived from the drawdown, not hardcoded.
        assert args.risk_per_trade is None
        assert args.daily_loss is None
        assert args.max_trades is None

    def test_rejects_unknown_profile(self):
        with pytest.raises(SystemExit):
            _args("--demo", "--profile", "not-a-real-account")

    def test_accepts_repeated_news_windows(self):
        args = _args("--demo", "--news", "08:30", "--news", "14:00")
        assert args.news == ["08:30", "14:00"]


class TestStatePersistence:
    def test_round_trip_preserves_the_threshold(self, tmp_path):
        path = tmp_path / "state.json"
        state = AccountState.fresh(APEX_50K_INTRADAY)
        state.mark(1_500.0)   # ratchet the threshold up
        state.mark(0.0)
        save_state(path, state)

        restored = load_state(_args("--demo", "--state", str(path)))
        assert restored.threshold == pytest.approx(state.threshold)
        assert restored.closed_balance == pytest.approx(state.closed_balance)
        # The ratcheted room must survive the round trip, not reset to $2,000.
        assert restored.room == pytest.approx(500.0)

    def test_explicit_flags_override_the_state_file(self, tmp_path):
        path = tmp_path / "state.json"
        save_state(path, AccountState.fresh(APEX_50K_INTRADAY))
        args = _args(
            "--demo", "--state", str(path), "--balance", "51000", "--threshold", "49500"
        )
        state = load_state(args)
        assert state.closed_balance == 51_000.0
        assert state.threshold == 49_500.0

    def test_impossible_threshold_is_raised_and_flagged(self, tmp_path):
        """A threshold below what the drawdown allows means the wrong profile."""
        from nqcopilot.apex import check_threshold_consistency

        warning = check_threshold_consistency(APEX_50K_INTRADAY, 51_000.0, 48_900.0)
        assert warning is not None
        assert "wrong profile" in warning

        args = _args(
            "--demo", "--state", str(tmp_path / "s.json"),
            "--balance", "51000", "--threshold", "48900",
        )
        # The engine uses the arithmetically forced value, never the lower one.
        assert load_state(args).threshold == pytest.approx(49_000.0)

    def test_consistent_threshold_produces_no_warning(self):
        from nqcopilot.apex import check_threshold_consistency

        assert check_threshold_consistency(APEX_50K_INTRADAY, 51_000.0, 49_500.0) is None

    def test_missing_state_file_falls_back_to_profile_defaults(self, tmp_path):
        state = load_state(_args("--demo", "--state", str(tmp_path / "absent.json")))
        assert state.closed_balance == APEX_50K_INTRADAY.starting_balance
        assert state.threshold == APEX_50K_INTRADAY.initial_threshold

    def test_corrupt_state_file_is_rejected_not_ignored(self, tmp_path):
        from nqcopilot.data import DataError

        path = tmp_path / "state.json"
        path.write_text("{not json at all")
        with pytest.raises(DataError):
            load_state(_args("--demo", "--state", str(path)))

    def test_stale_session_does_not_carry_counters_into_today(self, tmp_path):
        """Yesterday's trade count must not block today's first trade."""
        path = tmp_path / "state.json"
        state = AccountState.fresh(APEX_50K_INTRADAY)
        state.session_date = (datetime.now(ET) - timedelta(days=3)).date()
        state.trades_today = 4
        state.consecutive_losses = 3
        save_state(path, state)

        restored = load_state(_args("--demo", "--state", str(path)))
        assert restored.trades_today == 0
        assert restored.consecutive_losses == 0

    def test_same_session_keeps_counters(self, tmp_path):
        path = tmp_path / "state.json"
        state = AccountState.fresh(APEX_50K_INTRADAY, today=datetime.now(ET).date())
        state.close_trade(-50.0)
        state.close_trade(-50.0)
        save_state(path, state)

        restored = load_state(_args("--demo", "--state", str(path)))
        assert restored.trades_today == 2
        assert restored.consecutive_losses == 2

    def test_open_pnl_marks_the_account(self, tmp_path):
        path = tmp_path / "state.json"
        save_state(path, AccountState.fresh(APEX_50K_INTRADAY))
        state = load_state(_args("--demo", "--state", str(path), "--open-pnl", "800"))
        assert state.equity == pytest.approx(50_800.0)
        # An intraday account ratchets on unrealised profit immediately.
        assert state.threshold == pytest.approx(48_800.0)


class TestStateCommands:
    def test_record_trade_updates_balance_and_persists(self, tmp_path, capsys):
        path = tmp_path / "state.json"
        assert main(["--state", str(path), "--record-trade", "-125.50"]) == 0
        assert "Recorded loss of $125.50" in capsys.readouterr().out

        stored = json.loads(path.read_text())
        assert stored["closed_balance"] == pytest.approx(49_874.50)
        assert stored["trades_today"] == 1

    def test_record_trade_ratchets_the_threshold_on_a_win(self, tmp_path):
        path = tmp_path / "state.json"
        main(["--state", str(path), "--record-trade", "500"])
        stored = json.loads(path.read_text())
        assert stored["closed_balance"] == pytest.approx(50_500.0)
        assert stored["threshold"] == pytest.approx(48_500.0)

    def test_record_trade_requires_a_state_file(self):
        with pytest.raises(SystemExit):
            main(["--record-trade", "100"])

    def test_reset_day_clears_counters_and_persists(self, tmp_path):
        path = tmp_path / "state.json"
        main(["--state", str(path), "--record-trade", "-100"])
        assert main(["--state", str(path), "--reset-day"]) == 0
        stored = json.loads(path.read_text())
        assert stored["trades_today"] == 0
        assert stored["day_start_balance"] == pytest.approx(49_900.0)

    def test_reset_day_requires_a_state_file(self):
        with pytest.raises(SystemExit):
            main(["--reset-day"])


class TestEndToEnd:
    def test_demo_run_succeeds(self, capsys):
        assert main(["--demo", "--no-color"]) == 0
        out = capsys.readouterr().out
        assert "NQ COPILOT" in out
        assert "MARKET" in out

    def test_missing_data_source_is_a_clean_error(self, capsys):
        assert main(["--symbol", "MNQ"]) == 2
        assert "choose a data source" in capsys.readouterr().err

    def test_bad_news_value_is_a_clean_error(self, capsys):
        assert main(["--demo", "--news", "half-past-nine"]) == 2
        assert "--news" in capsys.readouterr().err

    def test_missing_csv_is_a_clean_error(self, capsys):
        assert main(["--csv", "/nonexistent/bars.csv"]) == 2
        assert "no such file" in capsys.readouterr().err

    def test_json_output_is_valid_and_complete(self, capsys):
        assert main(["--demo", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["action"] in {
            "GO_LONG", "GO_SHORT", "WAIT", "STAND_DOWN", "FLATTEN"
        }
        for key in ("headline", "regime", "price", "account", "blockers"):
            assert key in payload
        assert "threshold" in payload["account"]

    def test_backtest_run_succeeds(self, capsys):
        assert main(["--demo", "--backtest", "--trades", "--no-color"]) == 0
        assert "REPLAY" in capsys.readouterr().out

    def test_backtest_json_is_valid(self, capsys):
        assert main(["--demo", "--backtest", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert "metrics" in payload and "trades" in payload
        assert payload["metrics"]["trades"] == len(payload["trades"])

    def test_state_is_written_after_a_normal_run(self, tmp_path):
        path = tmp_path / "state.json"
        assert main(["--demo", "--state", str(path), "--no-color"]) == 0
        assert path.exists()
        assert "threshold" in json.loads(path.read_text())

    def test_round_turn_override_changes_risk(self, capsys):
        """A commission override must reach the sizing math, not just display."""
        assert main(["--demo", "--round-turn", "5.00", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        if payload["sizing"]:
            # Per-contract risk carries the overridden commission.
            assert payload["sizing"]["risk_per_contract"] > 5.0


class TestRendering:
    def _directive(self, **kwargs):
        bars = generate_demo_bars(count=300, seed=7, end=datetime(2026, 7, 27, 15, 0, tzinfo=ET))
        state = AccountState.fresh(APEX_50K_INTRADAY, today=datetime(2026, 7, 27).date())
        return evaluate(bars, MNQ, state, **kwargs), state

    def test_card_renders_without_colour(self):
        directive, state = self._directive()
        out = render(directive, state, MNQ, PLAIN, verbose=False)
        assert "NQ COPILOT" in out
        assert "\033[" not in out  # no ANSI codes leaked

    def test_verbose_mode_renders(self):
        directive, state = self._directive()
        assert render(directive, state, MNQ, PLAIN, verbose=True)

    def test_colour_palette_emits_codes_when_enabled(self):
        assert "\033[" in Palette(enabled=True)("x", "\033[32m")
        assert Palette(enabled=False)("x", "\033[32m") == "x"

    def test_every_action_renders(self):
        """Including the breached path, which must never crash."""
        bars = generate_demo_bars(count=300, seed=7, end=datetime(2026, 7, 27, 15, 0, tzinfo=ET))
        breached = AccountState.resume(
            APEX_50K_INTRADAY, closed_balance=47_400.0, threshold=47_500.0,
            today=datetime(2026, 7, 27).date(),
        )
        directive = evaluate(bars, MNQ, breached)
        out = render(directive, breached, MNQ, PLAIN, verbose=True)
        assert "STAND DOWN" in out
        assert "BLOCKED BY" in out

    def test_directive_dict_is_json_serialisable(self):
        directive, state = self._directive()
        json.dumps(directive_to_dict(directive, state))


class TestWatchMode:
    """One long-lived process, resilient to transient failures.

    Relaunching a binary on a timer makes the OS treat each iteration as a new
    program and re-prompt for permissions, so the loop must stay in-process.
    """

    def test_interval_is_floored(self, monkeypatch, capsys):
        from nqcopilot.cli import MIN_WATCH_SECONDS, run_watch

        slept: list[int] = []

        def fake_sleep(seconds):
            slept.append(seconds)
            raise KeyboardInterrupt  # stop after one cycle

        monkeypatch.setattr("nqcopilot.cli._time.sleep", fake_sleep)
        monkeypatch.setattr("nqcopilot.cli.run_once", lambda a, c: 0)

        args = build_parser().parse_args(["--demo", "--watch", "1"])
        with pytest.raises(KeyboardInterrupt):
            run_watch(args, PLAIN)
        assert slept == [MIN_WATCH_SECONDS]
        assert "raised to" in capsys.readouterr().err

    def test_transient_error_does_not_kill_the_loop(self, monkeypatch, capsys):
        from nqcopilot.cli import run_watch
        from nqcopilot.data import DataError

        calls = {"n": 0}

        def flaky(args, c):
            calls["n"] += 1
            if calls["n"] == 1:
                raise DataError("rate limited")
            return 0

        def fake_sleep(_):
            if calls["n"] >= 2:
                raise KeyboardInterrupt

        monkeypatch.setattr("nqcopilot.cli.run_once", flaky)
        monkeypatch.setattr("nqcopilot.cli._time.sleep", fake_sleep)

        args = build_parser().parse_args(["--demo", "--watch", "5"])
        with pytest.raises(KeyboardInterrupt):
            run_watch(args, PLAIN)

        # It survived the failure and ran again rather than exiting.
        assert calls["n"] == 2
        assert "rate limited" in capsys.readouterr().err

    def test_does_not_clear_the_screen_by_default(self, monkeypatch, capsys):
        from nqcopilot.cli import run_watch

        monkeypatch.setattr("nqcopilot.cli.run_once", lambda a, c: 0)
        monkeypatch.setattr(
            "nqcopilot.cli._time.sleep",
            lambda _: (_ for _ in ()).throw(KeyboardInterrupt),
        )
        args = build_parser().parse_args(["--demo", "--watch", "5"])
        with pytest.raises(KeyboardInterrupt):
            run_watch(args, PLAIN)
        assert "\033[2J" not in capsys.readouterr().out

    def test_clear_flag_opts_in(self, monkeypatch, capsys):
        from nqcopilot.cli import run_watch

        monkeypatch.setattr("nqcopilot.cli.run_once", lambda a, c: 0)
        monkeypatch.setattr(
            "nqcopilot.cli._time.sleep",
            lambda _: (_ for _ in ()).throw(KeyboardInterrupt),
        )
        args = build_parser().parse_args(["--demo", "--watch", "5", "--clear"])
        with pytest.raises(KeyboardInterrupt):
            run_watch(args, PLAIN)
        assert "\033[2J" in capsys.readouterr().out


class TestSierraSource:
    """The .scid path through the CLI.

    Covered here rather than only in test_sierra.py because the flag surface is
    where a tick file gets silently misread: `--scid` with the wrong interval or
    an unparseable window should fail loudly, not return a shorter series.
    """

    def _write(self, tmp_path, count: int = 400):
        from datetime import timedelta

        from nqcopilot.sierra import make_tick_record, write_scid

        base = datetime(2026, 7, 27, 9, 30, tzinfo=ET)
        path = tmp_path / "NQ.scid"
        write_scid(
            path,
            [
                make_tick_record(
                    base + timedelta(seconds=30 * i),
                    price=20_000.0 + (i % 11) * 0.25,
                    volume=2,
                    bid=19_000.0,
                    ask=21_000.0,
                )
                for i in range(count)
            ],
        )
        return path

    def test_scid_is_accepted_as_a_data_source(self, tmp_path):
        from nqcopilot.cli import load_bars

        args = _args("--scid", str(self._write(tmp_path)))
        bars = load_bars(args)
        assert bars
        # Bid/ask are far outside the trades; a bar-shaped misread would show it.
        assert all(19_500.0 < b.low <= b.high < 20_500.0 for b in bars)

    def test_scid_interval_controls_bar_size(self, tmp_path):
        from nqcopilot.cli import load_bars

        path = str(self._write(tmp_path))
        five = load_bars(_args("--scid", path, "--scid-interval", "5"))
        one = load_bars(_args("--scid", path, "--scid-interval", "1"))
        assert len(one) > len(five)

    def test_scid_info_requires_scid(self, capsys):
        with pytest.raises(SystemExit):
            main(["--scid-info"])

    def test_scid_info_summarises_the_file(self, tmp_path, capsys):
        assert main(["--scid", str(self._write(tmp_path)), "--scid-info"]) == 0
        out = capsys.readouterr().out
        assert "tick data" in out and "records" in out

    def test_unparseable_window_is_rejected(self, tmp_path):
        from nqcopilot.cli import load_bars
        from nqcopilot.data import DataError

        args = _args("--scid", str(self._write(tmp_path)), "--scid-start", "not-a-time")
        with pytest.raises(DataError, match="ISO-8601"):
            load_bars(args)

    def test_a_scid_read_failure_exits_cleanly(self, tmp_path, capsys):
        bad = tmp_path / "bad.scid"
        bad.write_bytes(b"NOPE" + b"\x00" * 200)
        assert main(["--scid", str(bad)]) == 2
        assert "not a Sierra Chart" in capsys.readouterr().err

    def test_no_source_names_scid_in_the_error(self, capsys):
        assert main([]) == 2
        assert "--scid" in capsys.readouterr().err
