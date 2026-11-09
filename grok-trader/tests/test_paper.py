from datetime import datetime, timedelta, timezone

import pytest

from paper import PaperBroker

CFG = dict(paper_starting_balance_usd=1000.0, max_position_pct=0.02, stop_loss_pct=0.25, take_profit_pct=0.5,
           max_daily_loss_pct=0.05, max_open_positions=2, paper_fee_pct=0.01)
T0 = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def broker(tmp_path, **over):
    return PaperBroker(str(tmp_path / "state.json"), {**CFG, **over})


def test_open_sizes_position_and_charges_fee(tmp_path):
    b = broker(tmp_path)
    pos = b.open("MINT1", "AAA", 2.0, T0)
    assert pos["cost_usd"] == 20.0 and pos["entry_fee_usd"] == pytest.approx(0.2)
    assert pos["qty"] == pytest.approx(19.8 / 2.0)
    assert pos["stop_price"] == pytest.approx(1.5) and pos["take_profit_price"] == pytest.approx(3.0)
    assert b.state["balance_usd"] == 980.0 and b.equity() == pytest.approx(980.0 + 19.8 * 0.99)


def test_stop_loss_and_take_profit(tmp_path):
    b = broker(tmp_path)
    b.open("A", "A", 1.0, T0)
    assert b.mark("A", 0.8, T0) is None and "A" in b.state["positions"]
    rec = b.mark("A", 0.75, T0)
    assert rec["reason"] == "stop_loss" and rec["pnl_usd"] == pytest.approx(19.8 * 0.75 * 0.99 - 20)
    b.open("B", "B", 1.0, T0)
    rec = b.mark("B", 1.5, T0)
    assert rec["reason"] == "take_profit" and rec["pnl_usd"] > 0
    assert b.summary(T0)["closed_trades"] == 2 and b.summary(T0)["stop_losses_fired"] == 1


def test_bad_prices_are_ignored(tmp_path):
    b = broker(tmp_path)
    b.open("A", "A", 1.0, T0)
    assert b.mark("A", None, T0) is None and b.mark("A", 0, T0) is None and "A" in b.state["positions"]
    with pytest.raises(ValueError):
        b.open("B", "B", 0, T0)


def test_duplicate_and_max_positions(tmp_path):
    b = broker(tmp_path)
    b.open("A", "A", 1.0, T0)
    assert b.can_open("A", T0) == (False, "already holding this token")
    b.open("B", "B", 1.0, T0)
    assert b.can_open("C", T0)[0] is False
    with pytest.raises(ValueError):
        b.open("C", "C", 1.0, T0)


def test_daily_loss_limit_halts_until_next_utc_day(tmp_path):
    b = broker(tmp_path, max_daily_loss_pct=0.005)  # $5 on $1000
    b.open("A", "A", 1.0, T0)
    rec = b.mark("A", 0.7, T0)  # loses about $6.3
    assert rec["pnl_usd"] < -5 and b.halted(T0)
    assert b.can_open("B", T0)[0] is False
    assert b.can_open("B", T0 + timedelta(hours=5))[0] is False  # same UTC day
    assert b.can_open("B", T0 + timedelta(days=1))[0] is True
    assert b.summary(T0)["halted_today"] is True


def test_state_survives_restart(tmp_path):
    b = broker(tmp_path)
    b.open("A", "A", 1.0, T0)
    again = broker(tmp_path)
    assert "A" in again.state["positions"] and again.state["balance_usd"] == 980.0
    assert not (tmp_path / "state.json.tmp").exists()


def test_summary_shape(tmp_path):
    s = broker(tmp_path).summary(T0)
    for key in ("balance_usd", "equity_usd", "open_positions", "wins", "losses", "avg_win_usd",
                "avg_loss_usd", "biggest_loss_usd", "realized_pnl_usd", "today_realized_pnl_usd", "halted_today"):
        assert key in s


def test_new_day_starts_from_pre_trade_equity(tmp_path):
    b = broker(tmp_path, max_position_pct=0.5)
    b.open("A", "A", 1.0, T0)  # $500 position, equity 1000
    rec = b.mark("A", 0.5, T0 + timedelta(days=1))  # first event of day 2 is a big stop-out
    assert rec["reason"] == "stop_loss"
    day2 = b.state["days"][(T0 + timedelta(days=1)).date().isoformat()]
    assert day2["start_equity"] == pytest.approx(1000.0) and day2["halted"] is True


def test_non_finite_prices_are_rejected(tmp_path):
    import math
    b = broker(tmp_path)
    for bad in (math.nan, math.inf, -math.inf, True, "1.0", None, 0, -1):
        with pytest.raises(ValueError):
            b.open("A", "A", bad, T0)
    b.open("A", "A", 1.0, T0)
    assert b.mark("A", math.nan, T0) is None and b.mark("A", math.inf, T0) is None and "A" in b.state["positions"]
    assert b.mark("A", 0.5, T0)["reason"] == "stop_loss"


def test_day_key_is_utc_even_for_other_timezones(tmp_path):
    from datetime import timedelta, timezone as tz
    b = broker(tmp_path)
    late_in_new_york = datetime(2026, 1, 1, 22, tzinfo=tz(timedelta(hours=-5)))  # 03:00 UTC on Jan 2
    b._day(late_in_new_york)
    assert list(b.state["days"]) == ["2026-01-02"]


def test_corrupt_state_file_raises_a_clear_error(tmp_path):
    (tmp_path / "state.json").write_text("{oops")
    with pytest.raises(RuntimeError, match="unreadable"):
        broker(tmp_path)
    (tmp_path / "state.json").write_text("[]")
    with pytest.raises(RuntimeError, match="unexpected shape"):
        broker(tmp_path)
