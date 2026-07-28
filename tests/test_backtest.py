"""Tests for the replay harness.

The claims under test are about *pessimism*. A backtest that resolves ambiguity
in its own favour manufactures confidence, so each modelling choice is asserted
directly rather than assumed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nqcopilot.apex import APEX_50K_INTRADAY, AccountState, RiskLimits
from nqcopilot.backtest import (
    BacktestConfig,
    _manage,
    _OpenPosition,
    compute_metrics,
    run_backtest,
)
from nqcopilot.bars import ET, Bar
from nqcopilot.contracts import MNQ
from nqcopilot.data import generate_demo_bars
from nqcopilot.signals import Setup

DEMO_END = datetime(2026, 7, 27, 15, 0, tzinfo=ET)


def _state() -> AccountState:
    return AccountState.fresh(APEX_50K_INTRADAY, today=DEMO_END.date())


def _long_setup(entry=20_000.0, stop=19_980.0, t1=20_030.0, t2=20_060.0) -> Setup:
    return Setup(
        name="Test", direction=1, entry=entry, stop=stop,
        target1=t1, target2=t2, score=80.0,
    )


def _position(setup: Setup, quantity: int = 2) -> _OpenPosition:
    return _OpenPosition(
        setup=setup,
        quantity=quantity,
        remaining=quantity,
        entry_price=setup.entry,
        entry_ts=datetime(2026, 7, 27, 10, 0, tzinfo=ET),
        stop=setup.stop,
        planned_risk=MNQ.points_to_dollars(abs(setup.entry - setup.stop), quantity),
    )


class TestPessimisticResolution:
    def test_bar_containing_both_stop_and_target_is_a_loss(self):
        """The core anti-flattery rule: ambiguity resolves against the trade."""
        setup = _long_setup()
        position = _position(setup)
        state = _state()
        # This bar's range spans the stop AND both targets.
        bar = Bar(
            datetime(2026, 7, 27, 10, 5, tzinfo=ET),
            open=20_000.0, high=20_070.0, low=19_970.0, close=20_050.0,
        )
        remaining, trade = _manage(position, bar, MNQ, state, BacktestConfig())
        assert remaining is None
        assert trade.exit_reason == "stop"
        assert trade.net_pnl < 0

    def test_gap_through_stop_fills_at_the_open(self):
        setup = _long_setup()
        position = _position(setup)
        state = _state()
        # Opens far below the stop: the fill is the open, not the stop price.
        bar = Bar(
            datetime(2026, 7, 27, 10, 5, tzinfo=ET),
            open=19_900.0, high=19_910.0, low=19_890.0, close=19_900.0,
        )
        _, trade = _manage(position, bar, MNQ, state, BacktestConfig())
        assert trade.exit_reason == "gap_through_stop"
        # A stop fill would have lost ~20 points; the gap loses ~100.
        assert trade.exit_price == pytest.approx(19_900.0)
        assert trade.net_pnl < -MNQ.points_to_dollars(50.0, 2)

    def test_stop_fill_includes_adverse_slippage(self):
        setup = _long_setup()
        position = _position(setup, quantity=1)
        state = _state()
        bar = Bar(
            datetime(2026, 7, 27, 10, 5, tzinfo=ET),
            open=20_000.0, high=20_001.0, low=19_975.0, close=19_980.0,
        )
        cfg = BacktestConfig(slippage_ticks=2.0)
        _, trade = _manage(position, bar, MNQ, state, cfg)
        # Filled two ticks below the 19,980 stop.
        assert trade.exit_price == pytest.approx(19_980.0 - 0.5)

    def test_entry_fills_on_the_next_bar_open_with_slippage(self):
        """A signal bar's close has already happened and cannot be traded."""
        bars = generate_demo_bars(count=300, seed=7, end=DEMO_END)
        result = run_backtest(
            bars, MNQ, _state(),
            config=BacktestConfig(slippage_ticks=1.0, warmup_bars=80),
        )
        for trade in result.trades:
            index = next(i for i, b in enumerate(bars) if b.ts == trade.entry_ts)
            # The fill is that bar's open, moved against the trade by one tick.
            expected = bars[index].open + MNQ.tick_size * trade.direction
            assert trade.entry_price == pytest.approx(expected, abs=MNQ.tick_size)


class TestThresholdDuringReplay:
    def test_threshold_ratchets_on_the_favourable_extreme(self):
        """Not the close — the punishing reading of the intraday rule."""
        setup = _long_setup()
        position = _position(setup, quantity=10)
        state = _state()
        before = state.threshold
        # Runs 25 points in favour intrabar, closes back at entry.
        bar = Bar(
            datetime(2026, 7, 27, 10, 5, tzinfo=ET),
            open=20_000.0, high=20_025.0, low=19_999.0, close=20_000.0,
        )
        _manage(position, bar, MNQ, state, BacktestConfig())
        # 25 points on 10 micros is $500 of unrealised profit at the extreme.
        assert state.threshold == pytest.approx(before + 500.0)

    def test_threshold_never_decreases_across_a_replay(self):
        bars = generate_demo_bars(count=400, seed=3, end=DEMO_END)
        result = run_backtest(bars, MNQ, _state())
        thresholds = [t for _, t in result.threshold_curve]
        for earlier, later in zip(thresholds, thresholds[1:]):
            assert later >= earlier - 1e-9

    def test_replay_under_default_limits_does_not_breach(self):
        """The risk layer's purpose, tested end to end over a full account life."""
        for seed in (3, 7, 11, 23):
            bars = generate_demo_bars(count=400, seed=seed, end=DEMO_END)
            result = run_backtest(bars, MNQ, _state())
            assert not result.breached, f"seed {seed} breached the account"


class TestAccounting:
    def test_commission_is_charged_on_every_exit(self):
        setup = _long_setup()
        position = _position(setup, quantity=4)
        state = _state()
        bar = Bar(
            datetime(2026, 7, 27, 10, 5, tzinfo=ET),
            open=20_000.0, high=20_065.0, low=19_999.0, close=20_060.0,
        )
        _, trade = _manage(position, bar, MNQ, state, BacktestConfig())
        # Scaled 2 at target 1 and closed 2 at target 2: 4 contracts of cost.
        assert trade.commission == pytest.approx(MNQ.commission(4))
        assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.commission)

    def test_scale_out_moves_the_stop_to_breakeven(self):
        setup = _long_setup()
        position = _position(setup, quantity=4)
        state = _state()
        # Reaches target 1 only.
        bar = Bar(
            datetime(2026, 7, 27, 10, 5, tzinfo=ET),
            open=20_000.0, high=20_035.0, low=19_999.0, close=20_030.0,
        )
        remaining, trade = _manage(position, bar, MNQ, state, BacktestConfig())
        assert trade is None
        assert remaining.scaled
        assert remaining.remaining == 2
        assert remaining.stop == pytest.approx(remaining.entry_price)

    def test_single_contract_exits_fully_at_target_one(self):
        setup = _long_setup()
        position = _position(setup, quantity=1)
        state = _state()
        bar = Bar(
            datetime(2026, 7, 27, 10, 5, tzinfo=ET),
            open=20_000.0, high=20_035.0, low=19_999.0, close=20_030.0,
        )
        _, trade = _manage(position, bar, MNQ, state, BacktestConfig())
        assert trade.exit_reason == "target1"
        assert trade.net_pnl > 0

    def test_realised_pnl_reaches_the_account(self):
        bars = generate_demo_bars(count=400, seed=7, end=DEMO_END)
        state = _state()
        result = run_backtest(bars, MNQ, state)
        expected = APEX_50K_INTRADAY.starting_balance + sum(t.net_pnl for t in result.trades)
        assert state.closed_balance == pytest.approx(expected)

    def test_mae_and_mfe_are_recorded(self):
        setup = _long_setup()
        position = _position(setup)
        state = _state()
        bar = Bar(
            datetime(2026, 7, 27, 10, 5, tzinfo=ET),
            open=20_000.0, high=20_015.0, low=19_990.0, close=20_010.0,
        )
        remaining, _ = _manage(position, bar, MNQ, state, BacktestConfig())
        assert remaining.mfe_points == pytest.approx(15.0)
        assert remaining.mae_points == pytest.approx(10.0)


class TestPositionDiscipline:
    def test_never_holds_more_than_one_position(self):
        bars = generate_demo_bars(count=400, seed=7, end=DEMO_END)
        result = run_backtest(bars, MNQ, _state())
        # Overlapping entry/exit windows would mean concurrent positions.
        ordered = sorted(result.trades, key=lambda t: t.entry_ts)
        for earlier, later in zip(ordered, ordered[1:]):
            assert later.entry_ts >= earlier.exit_ts

    def test_no_position_survives_the_end_of_the_run(self):
        bars = generate_demo_bars(count=400, seed=7, end=DEMO_END)
        result = run_backtest(bars, MNQ, _state())
        assert result.state.open_pnl == 0.0

    def test_quantities_respect_the_contract_cap(self):
        bars = generate_demo_bars(count=400, seed=7, end=DEMO_END)
        result = run_backtest(bars, MNQ, _state())
        cap = APEX_50K_INTRADAY.contract_cap(MNQ)
        for trade in result.trades:
            assert 1 <= trade.quantity <= cap


class TestMetrics:
    def test_arithmetic_is_consistent(self):
        bars = generate_demo_bars(count=400, seed=7, end=DEMO_END)
        result = run_backtest(bars, MNQ, _state())
        m = result.metrics
        assert m.trades == m.wins + m.losses + sum(
            1 for t in result.trades if t.net_pnl == 0
        )
        assert m.net_pnl == pytest.approx(sum(t.net_pnl for t in result.trades))
        if m.trades:
            assert m.expectancy == pytest.approx(m.net_pnl / m.trades)

    def test_profit_factor_matches_gross_ratio(self):
        bars = generate_demo_bars(count=400, seed=7, end=DEMO_END)
        m = run_backtest(bars, MNQ, _state()).metrics
        if m.gross_loss > 0:
            assert m.profit_factor == pytest.approx(m.gross_profit / m.gross_loss)

    def test_max_drawdown_is_non_negative(self):
        bars = generate_demo_bars(count=400, seed=11, end=DEMO_END)
        assert run_backtest(bars, MNQ, _state()).metrics.max_drawdown >= 0.0

    def test_empty_result_metrics_do_not_divide_by_zero(self):
        bars = generate_demo_bars(count=150, seed=7, end=DEMO_END)
        # A score nobody can reach means no trades at all.
        from nqcopilot.playbook import PlaybookConfig

        result = run_backtest(
            bars, MNQ, _state(), playbook=PlaybookConfig(min_score=999.0)
        )
        assert result.trades == []
        m = result.metrics
        assert m.trades == 0 and m.win_rate == 0.0 and m.expectancy == 0.0


class TestRiskIntegration:
    def test_daily_trade_cap_is_respected_across_the_run(self):
        bars = generate_demo_bars(count=400, seed=7, end=DEMO_END)
        result = run_backtest(
            bars, MNQ, _state(), limits=RiskLimits(max_trades_per_day=2)
        )
        by_day: dict[object, int] = {}
        for trade in result.trades:
            from nqcopilot.bars import trading_date

            day = trading_date(trade.entry_ts)
            by_day[day] = by_day.get(day, 0) + 1
        # The cap gates new entries, so a day can never start a third trade.
        assert all(count <= 2 for count in by_day.values())

    def test_tighter_risk_produces_smaller_positions(self):
        bars = generate_demo_bars(count=400, seed=7, end=DEMO_END)
        loose = run_backtest(bars, MNQ, _state(), limits=RiskLimits(max_risk_per_trade=400.0))
        tight = run_backtest(bars, MNQ, _state(), limits=RiskLimits(max_risk_per_trade=100.0))
        if loose.trades and tight.trades:
            avg_loose = sum(t.quantity for t in loose.trades) / len(loose.trades)
            avg_tight = sum(t.quantity for t in tight.trades) / len(tight.trades)
            assert avg_tight <= avg_loose
