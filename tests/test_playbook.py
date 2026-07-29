"""Integration tests for the decision layer.

The central claim under test: **a hard rule gate always beats a good setup.**
If any of these fail, the tool can talk you into a trade that breaches your
account, which is the one failure mode that must not exist.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import series_from_closes

from nqcopilot.apex import (
    APEX_50K_INTRADAY,
    AccountState,
    RiskEngine,
    RiskLimits,
)
from nqcopilot.bars import ET
from nqcopilot.contracts import MNQ
from nqcopilot.data import generate_demo_bars
from nqcopilot.playbook import Action, PlaybookConfig, evaluate

# A fixed Monday afternoon so every run sees identical bars.
DEMO_END = datetime(2026, 7, 27, 15, 0, tzinfo=ET)


@pytest.fixture(scope="module")
def demo_bars():
    return generate_demo_bars(count=400, seed=7, end=DEMO_END)


def _fresh() -> AccountState:
    return AccountState.fresh(APEX_50K_INTRADAY, today=DEMO_END.date())


def _scan_for_entry(bars, *, start: int = 120, config=None):
    """Find the first index whose evaluation produces an executable entry."""
    cfg = config or PlaybookConfig()
    for i in range(start, len(bars)):
        state = _fresh()
        directive = evaluate(bars[: i + 1], MNQ, state, config=cfg)
        if directive.is_actionable:
            return i, directive
    return None, None


class TestWarmup:
    def test_insufficient_history_waits(self):
        bars = series_from_closes([100.0 + i for i in range(10)])
        directive = evaluate(bars, MNQ, _fresh())
        assert directive.action is Action.WAIT
        assert "arming up" in directive.headline or "more bars" in directive.headline

    def test_never_actionable_during_warmup(self):
        bars = series_from_closes([20_000.0 + i for i in range(15)])
        assert not evaluate(bars, MNQ, _fresh()).is_actionable


class TestSignalGeneration:
    def test_demo_data_produces_at_least_one_entry(self, demo_bars):
        """Sanity: the detectors are reachable, not dead code."""
        index, directive = _scan_for_entry(demo_bars)
        assert index is not None, "no setup fired across the whole demo series"
        assert directive.action in (Action.GO_LONG, Action.GO_SHORT)
        assert directive.setup is not None
        assert directive.sizing.quantity >= 1

    def test_entry_plan_geometry_is_valid(self, demo_bars):
        _, directive = _scan_for_entry(demo_bars)
        setup = directive.setup
        if setup.is_long:
            assert setup.stop < setup.entry < setup.target1 <= setup.target2
        else:
            assert setup.stop > setup.entry > setup.target1 >= setup.target2

    def test_all_prices_land_on_legal_ticks(self, demo_bars):
        _, directive = _scan_for_entry(demo_bars)
        setup = directive.setup
        for price in (setup.entry, setup.stop, setup.target1, setup.target2):
            assert abs(price / MNQ.tick_size - round(price / MNQ.tick_size)) < 1e-9

    def test_headline_is_always_populated(self, demo_bars):
        for i in range(100, len(demo_bars), 17):
            directive = evaluate(demo_bars[: i + 1], MNQ, _fresh())
            assert directive.headline.strip()


class TestRiskVeto:
    """A hard blocker must override any setup, at every index."""

    def test_breached_account_never_produces_an_entry(self, demo_bars):
        index, clean = _scan_for_entry(demo_bars)
        assert clean.is_actionable  # the same bars did produce a trade

        breached = AccountState.resume(
            APEX_50K_INTRADAY, closed_balance=47_400.0, threshold=47_500.0,
            today=DEMO_END.date(),
        )
        directive = evaluate(demo_bars[: index + 1], MNQ, breached)
        assert directive.action is Action.STAND_DOWN
        assert not directive.is_actionable

    def test_daily_loss_limit_overrides_a_valid_setup(self, demo_bars):
        index, clean = _scan_for_entry(demo_bars)
        state = _fresh()
        state.close_trade(-600.0)
        engine = RiskEngine(state, RiskLimits(daily_loss_limit=600.0))
        directive = evaluate(demo_bars[: index + 1], MNQ, state, risk=engine)
        assert directive.action is Action.STAND_DOWN
        assert any(b.code == "daily_loss_limit" for b in directive.hard_blockers)

    def test_loss_streak_overrides_a_valid_setup(self, demo_bars):
        index, _ = _scan_for_entry(demo_bars)
        state = _fresh()
        state.close_trade(-40.0)
        state.close_trade(-40.0)
        engine = RiskEngine(state, RiskLimits(max_consecutive_losses=2))
        directive = evaluate(demo_bars[: index + 1], MNQ, state, risk=engine)
        assert directive.action is Action.STAND_DOWN

    def test_open_position_with_hard_blocker_is_told_to_flatten(self, demo_bars):
        index, _ = _scan_for_entry(demo_bars)
        state = _fresh()
        state.close_trade(-600.0)
        engine = RiskEngine(state, RiskLimits(daily_loss_limit=600.0))
        directive = evaluate(
            demo_bars[: index + 1], MNQ, state, risk=engine, in_position=True
        )
        assert directive.action is Action.FLATTEN

    def test_invariant_no_entry_while_any_hard_blocker_exists(self, demo_bars):
        """Swept across the full series, not just a hand-picked bar."""
        checked = 0
        for i in range(80, len(demo_bars), 3):
            directive = evaluate(demo_bars[: i + 1], MNQ, _fresh())
            if directive.hard_blockers:
                assert not directive.is_actionable
                assert directive.action in (Action.STAND_DOWN, Action.FLATTEN)
                checked += 1
        assert checked > 0, "the sweep never exercised a blocked state"

    def test_open_position_never_yields_a_new_entry(self, demo_bars):
        for i in range(120, len(demo_bars), 11):
            directive = evaluate(demo_bars[: i + 1], MNQ, _fresh(), in_position=True)
            assert not directive.action.is_entry


class TestSizingIntegration:
    def test_risk_never_exceeds_remaining_room(self, demo_bars):
        for i in range(80, len(demo_bars), 5):
            state = _fresh()
            directive = evaluate(demo_bars[: i + 1], MNQ, state)
            if directive.sizing:
                assert directive.sizing.risk_dollars < state.room

    def test_quantity_never_exceeds_the_contract_cap(self, demo_bars):
        cap = APEX_50K_INTRADAY.contract_cap(MNQ)
        for i in range(80, len(demo_bars), 5):
            directive = evaluate(demo_bars[: i + 1], MNQ, _fresh())
            if directive.sizing:
                assert directive.sizing.quantity <= cap

    def test_thin_room_suppresses_entries(self, demo_bars):
        """With almost no drawdown room, nothing should be tradeable."""
        index, _ = _scan_for_entry(demo_bars)
        thin = AccountState.resume(
            APEX_50K_INTRADAY, closed_balance=47_950.0, threshold=47_800.0,
            today=DEMO_END.date(),
        )
        directive = evaluate(demo_bars[: index + 1], MNQ, thin)
        assert not directive.is_actionable

    def test_actionable_trade_survives_its_own_stop(self, demo_bars):
        """Taking the full planned loss must not breach the account."""
        for i in range(80, len(demo_bars), 5):
            state = _fresh()
            directive = evaluate(demo_bars[: i + 1], MNQ, state)
            if not directive.is_actionable:
                continue
            worst_case = state.equity - directive.sizing.risk_dollars
            assert worst_case > state.threshold


class TestQualityThresholds:
    def test_raising_min_score_reduces_entries(self, demo_bars):
        def count(min_score: float) -> int:
            total = 0
            for i in range(80, len(demo_bars), 3):
                directive = evaluate(
                    demo_bars[: i + 1], MNQ, _fresh(),
                    config=PlaybookConfig(min_score=min_score),
                )
                total += directive.is_actionable
            return total

        assert count(90.0) <= count(40.0)

    def test_min_net_r_is_enforced_after_commission(self, demo_bars):
        cfg = PlaybookConfig(min_net_r=1.2)
        for i in range(80, len(demo_bars), 3):
            directive = evaluate(demo_bars[: i + 1], MNQ, _fresh(), config=cfg)
            if directive.is_actionable:
                net = directive.setup.net_r_t1(MNQ, directive.sizing.quantity)
                assert net >= 1.2


class TestExplanation:
    def test_actionable_directives_explain_themselves(self, demo_bars):
        _, directive = _scan_for_entry(demo_bars)
        assert directive.reasons, "an entry with no stated rationale is unusable"
        assert directive.notes

    def test_intraday_threshold_warning_appears_on_entries(self, demo_bars):
        _, directive = _scan_for_entry(demo_bars)
        joined = " ".join(directive.notes).lower()
        assert "breakeven" in joined

    def test_blocked_directives_name_the_rule(self, demo_bars):
        state = _fresh()
        state.close_trade(-600.0)
        engine = RiskEngine(state, RiskLimits(daily_loss_limit=600.0))
        directive = evaluate(demo_bars[:200], MNQ, state, risk=engine)
        assert "daily loss limit" in directive.headline.lower()
