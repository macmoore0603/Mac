"""Tests for the Apex account model.

This module carries the strongest claims in the project. Signal quality is a
matter of opinion; the trailing-threshold arithmetic is not, and if any test
here fails the tool is actively dangerous to use.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from nqcopilot.apex import (
    APEX_50K_EOD,
    APEX_50K_EVAL,
    APEX_50K_INTRADAY,
    AccountState,
    RiskEngine,
    RiskLimits,
    RiskStyle,
    ScalingLadder,
    Severity,
    TrailingMode,
    payout_status,
)
from nqcopilot.bars import ET
from nqcopilot.contracts import MNQ, NQ


class TestInitialState:
    def test_fresh_account_starts_at_published_numbers(self, fresh_state):
        # $2,000 intraday trailing drawdown on a $50K account.
        assert fresh_state.equity == 50_000.0
        assert fresh_state.threshold == 48_000.0
        assert fresh_state.room == 2_000.0
        assert not fresh_state.is_breached

    def test_safety_net_is_start_plus_one_hundred(self):
        assert APEX_50K_INTRADAY.safety_net == pytest.approx(50_100.0)
        assert APEX_50K_INTRADAY.threshold_lock == pytest.approx(50_100.0)

    def test_resume_infers_peak_from_threshold(self):
        state = AccountState.resume(
            APEX_50K_INTRADAY, closed_balance=50_800.0, threshold=48_900.0
        )
        # Threshold 48,900 implies a peak of 48,900 + 2,000.
        assert state.peak_equity == pytest.approx(50_900.0)
        assert state.room == pytest.approx(1_900.0)


class TestIntradayTrailing:
    """The mechanic that ends most Apex accounts."""

    def test_unrealised_profit_ratchets_the_threshold(self, fresh_state):
        fresh_state.mark(1_500.0)
        assert fresh_state.peak_equity == 51_500.0
        assert fresh_state.threshold == 49_500.0

    def test_round_trip_to_breakeven_permanently_costs_room(self, fresh_state):
        """A winner given back costs real drawdown room without booking a cent."""
        assert fresh_state.room == 2_000.0

        fresh_state.mark(1_500.0)   # trade runs +$1,500 unrealised
        fresh_state.mark(0.0)       # and gives it all back

        assert fresh_state.closed_balance == 50_000.0  # nothing was booked
        assert fresh_state.threshold == 49_500.0       # but the threshold moved
        assert fresh_state.room == 500.0               # 75% of the buffer, gone

    def test_room_cost_is_predicted_before_the_fact(self, fresh_state):
        predicted = fresh_state.room_cost_of_running_profit(1_500.0)
        fresh_state.mark(1_500.0)
        fresh_state.mark(0.0)
        actual = 2_000.0 - fresh_state.room
        assert predicted == pytest.approx(actual)
        assert predicted == pytest.approx(1_500.0)

    def test_threshold_never_moves_down(self, fresh_state):
        fresh_state.mark(800.0)
        high_water = fresh_state.threshold
        fresh_state.mark(-400.0)
        assert fresh_state.threshold == high_water

    def test_threshold_freezes_at_the_lock(self, fresh_state):
        fresh_state.mark(2_600.0)  # peak 52,600 -> raw threshold 50,100
        assert fresh_state.threshold == pytest.approx(50_100.0)
        assert fresh_state.threshold_is_locked

        fresh_state.mark(5_000.0)  # peak 55,000 -> raw 52,500, but locked
        assert fresh_state.threshold == pytest.approx(50_100.0)

    def test_profit_needed_to_lock_is_reported(self, fresh_state):
        # Lock at $50,100 requires peak equity of 50,100 + 2,000 = $52,100.
        assert fresh_state.profit_to_lock_threshold() == pytest.approx(2_100.0)
        fresh_state.mark(1_000.0)
        assert fresh_state.profit_to_lock_threshold() == pytest.approx(1_100.0)
        fresh_state.mark(2_100.0)
        assert fresh_state.profit_to_lock_threshold() == 0.0
        assert fresh_state.threshold_is_locked

    def test_breach_detected_at_the_threshold(self, fresh_state):
        fresh_state.mark(-1_999.0)
        assert not fresh_state.is_breached
        fresh_state.mark(-2_000.0)
        assert fresh_state.is_breached  # touching the threshold is a breach

    def test_threshold_is_monotonic_under_random_marks(self, fresh_state):
        """Property check: no sequence of marks may ever lower the threshold."""
        rng = random.Random(42)
        previous = fresh_state.threshold
        for _ in range(500):
            fresh_state.mark(rng.uniform(-2_000, 4_000))
            assert fresh_state.threshold >= previous - 1e-9
            assert fresh_state.threshold <= APEX_50K_INTRADAY.threshold_lock + 1e-9
            previous = fresh_state.threshold


class TestEndOfDayTrailing:
    def test_open_profit_does_not_move_an_eod_threshold(self):
        state = AccountState.fresh(APEX_50K_EOD)
        start = state.threshold
        state.mark(1_500.0)
        assert state.threshold == start

    def test_threshold_advances_on_session_end(self):
        state = AccountState.fresh(APEX_50K_EOD)
        state.close_trade(1_000.0)
        assert state.threshold == 48_000.0  # unchanged during the session
        state.end_session()
        assert state.threshold == pytest.approx(49_000.0)

    def test_no_room_cost_from_running_profit(self):
        state = AccountState.fresh(APEX_50K_EOD)
        assert state.room_cost_of_running_profit(2_000.0) == 0.0


class TestTradeBooking:
    def test_closing_a_trade_updates_balance_and_counters(self, fresh_state):
        fresh_state.close_trade(250.0)
        assert fresh_state.closed_balance == 50_250.0
        assert fresh_state.trades_today == 1
        assert fresh_state.day_pnl == 250.0
        assert fresh_state.consecutive_losses == 0

    def test_loss_streak_counts_and_resets(self, fresh_state):
        fresh_state.close_trade(-100.0)
        fresh_state.close_trade(-100.0)
        assert fresh_state.consecutive_losses == 2
        fresh_state.close_trade(50.0)
        assert fresh_state.consecutive_losses == 0

    def test_session_roll_clears_daily_counters(self, fresh_state):
        fresh_state.close_trade(-100.0)
        fresh_state.end_session(datetime(2026, 7, 28).date())
        assert fresh_state.trades_today == 0
        assert fresh_state.consecutive_losses == 0
        assert fresh_state.day_pnl == 0.0
        assert fresh_state.closed_balance == 49_900.0


class TestSizing:
    def test_size_respects_per_trade_risk_cap(self, fresh_state):
        # pct_of_room relaxed so the per-trade cap is the one under test.
        engine = RiskEngine(
            fresh_state, RiskLimits(max_risk_per_trade=200.0, max_risk_pct_of_room=1.0)
        )
        # MNQ at $2/point: a 20pt stop risks $40 + $1.34 commission per contract.
        sizing = engine.size_position(MNQ, 20.0)
        assert sizing.risk_per_contract == pytest.approx(41.34)
        assert sizing.quantity == 4  # 4 x 41.34 = 165.36; a 5th would exceed 200
        assert sizing.risk_dollars <= 200.0

    def test_commission_is_included_in_risk(self, fresh_state):
        engine = RiskEngine(fresh_state)
        sizing = engine.size_position(MNQ, 10.0)
        gross = MNQ.points_to_dollars(10.0)
        assert sizing.risk_per_contract > gross
        assert sizing.risk_per_contract == pytest.approx(gross + MNQ.round_turn_cost)

    def test_never_sizes_past_the_threshold(self):
        """With almost no room left, size must collapse rather than gamble."""
        state = AccountState.resume(
            APEX_50K_INTRADAY, closed_balance=48_000.0, threshold=47_800.0
        )
        engine = RiskEngine(state, RiskLimits(threshold_safety_buffer=150.0))
        sizing = engine.size_position(MNQ, 20.0)
        assert sizing.risk_dollars <= state.room - 150.0
        # The worst case must leave the account alive.
        assert state.equity - sizing.risk_dollars > state.threshold

    def test_dollar_limits_bind_before_the_contract_cap(self, fresh_state):
        """The tightest constraint wins, and the engine names it.

        With conservative defaults on a $2,000 drawdown the per-trade cap is
        $100 — 5% of the allowance — which binds far before the 100-micro
        account cap. This ordering is the point of the whole calculation.
        """
        sizing = RiskEngine(fresh_state).size_position(MNQ, 8.0)
        assert sizing.limiting_factor == "max_risk_per_trade"
        assert sizing.risk_budget == pytest.approx(100.0)
        assert sizing.quantity == 5  # floor(100 / 17.34)
        assert sizing.quantity < fresh_state.profile.contract_cap(MNQ)

    def test_contract_cap_is_enforced(self, fresh_state):
        # Relax every dollar limit so only the account's contract cap remains.
        engine = RiskEngine(
            fresh_state,
            RiskLimits(
                max_risk_per_trade=100_000.0,
                max_risk_pct_of_room=10.0,
                daily_loss_limit=100_000.0,
            ),
        )
        sizing = engine.size_position(MNQ, 8.0)
        assert sizing.quantity == 100  # 10 minis -> 100 micros
        assert sizing.limiting_factor == "contract_cap"

    def test_override_caps_below_account_limit(self, fresh_state):
        engine = RiskEngine(
            fresh_state,
            RiskLimits(
                max_risk_per_trade=100_000.0,
                max_risk_pct_of_room=10.0,
                daily_loss_limit=100_000.0,
                max_contracts_override=2,
            ),
        )
        assert engine.size_position(MNQ, 8.0).quantity == 20  # 2 minis -> 20 micros
        assert engine.size_position(NQ, 8.0).quantity == 2

    def test_daily_loss_budget_shrinks_size_after_losses(self, fresh_state):
        limits = RiskLimits(max_risk_per_trade=500.0, daily_loss_limit=600.0)
        before = RiskEngine(fresh_state, limits).size_position(MNQ, 20.0).quantity
        fresh_state.close_trade(-450.0)
        after = RiskEngine(fresh_state, limits).size_position(MNQ, 20.0).quantity
        assert after < before
        assert after * 41.34 <= 150.0 + 1e-9

    def test_zero_size_when_stop_too_expensive(self):
        state = AccountState.resume(
            APEX_50K_INTRADAY, closed_balance=48_000.0, threshold=47_800.0
        )
        engine = RiskEngine(state, RiskLimits(threshold_safety_buffer=150.0))
        sizing = engine.size_position(NQ, 50.0)  # $1,000+ per contract
        assert sizing.quantity == 0
        assert not sizing.is_tradeable

    def test_rejects_nonpositive_stop(self, fresh_state):
        with pytest.raises(ValueError):
            RiskEngine(fresh_state).size_position(MNQ, 0.0)


class TestStopValidation:
    def test_rejects_stop_inside_the_noise_band(self, fresh_state):
        engine = RiskEngine(fresh_state, RiskLimits(min_stop_points=8.0))
        blocker = engine.validate_stop(4.0)
        assert blocker is not None and blocker.code == "stop_too_tight"

    def test_rejects_stop_too_wide_for_intraday(self, fresh_state):
        engine = RiskEngine(fresh_state, RiskLimits(max_stop_points=60.0))
        blocker = engine.validate_stop(90.0)
        assert blocker is not None and blocker.code == "stop_too_wide"

    def test_accepts_reasonable_stop(self, fresh_state):
        assert RiskEngine(fresh_state).validate_stop(20.0) is None


class TestGuards:
    def _codes(self, blockers):
        return {b.code for b in blockers}

    def test_clean_account_in_prime_hours_has_no_hard_blockers(self, fresh_state, rth_open):
        blockers = RiskEngine(fresh_state).check(rth_open)
        assert [b for b in blockers if b.is_hard] == []

    def test_breached_account_is_blocked(self, rth_open):
        state = AccountState.resume(
            APEX_50K_INTRADAY, closed_balance=47_400.0, threshold=47_500.0
        )
        blockers = RiskEngine(state).check(rth_open)
        assert "account_breached" in self._codes(blockers)

    def test_daily_loss_limit_blocks(self, fresh_state, rth_open):
        fresh_state.close_trade(-600.0)
        blockers = RiskEngine(fresh_state, RiskLimits(daily_loss_limit=600.0)).check(rth_open)
        assert "daily_loss_limit" in self._codes(blockers)

    def test_daily_profit_lock_blocks(self, fresh_state, rth_open):
        fresh_state.close_trade(950.0)
        blockers = RiskEngine(
            fresh_state, RiskLimits(daily_profit_lock=900.0, max_trades_per_day=99)
        ).check(rth_open)
        assert "daily_profit_lock" in self._codes(blockers)

    def test_trade_count_blocks(self, fresh_state, rth_open):
        for _ in range(4):
            fresh_state.close_trade(10.0)
        blockers = RiskEngine(fresh_state, RiskLimits(max_trades_per_day=4)).check(rth_open)
        assert "trade_count" in self._codes(blockers)

    def test_loss_streak_blocks(self, fresh_state, rth_open):
        fresh_state.close_trade(-50.0)
        fresh_state.close_trade(-50.0)
        blockers = RiskEngine(
            fresh_state, RiskLimits(max_consecutive_losses=2, max_trades_per_day=99)
        ).check(rth_open)
        assert "loss_streak" in self._codes(blockers)

    def test_insufficient_room_blocks(self, rth_open):
        state = AccountState.resume(
            APEX_50K_INTRADAY, closed_balance=47_700.0, threshold=47_500.0
        )
        blockers = RiskEngine(state, RiskLimits(min_room_to_trade=400.0)).check(rth_open)
        assert "insufficient_room" in self._codes(blockers)

    def test_maintenance_halt_blocks(self, fresh_state):
        halt = datetime(2026, 7, 27, 17, 30, tzinfo=ET)
        assert "market_closed" in self._codes(RiskEngine(fresh_state).check(halt))

    def test_weekend_blocks(self, fresh_state):
        saturday = datetime(2026, 7, 25, 12, 0, tzinfo=ET)
        assert "market_closed" in self._codes(RiskEngine(fresh_state).check(saturday))

    def test_near_flatten_deadline_blocks_new_entries(self, fresh_state):
        late = datetime(2026, 7, 27, 16, 45, tzinfo=ET)
        assert "near_flatten" in self._codes(RiskEngine(fresh_state).check(late))

    def test_past_flatten_deadline_blocks(self, fresh_state):
        past = datetime(2026, 7, 27, 16, 59, 30, tzinfo=ET)
        codes = self._codes(RiskEngine(fresh_state).check(past, in_position=True))
        assert "past_flatten" in codes

    def test_opening_auction_blocks_first_minutes(self, fresh_state):
        just_open = datetime(2026, 7, 27, 9, 32, tzinfo=ET)
        assert "opening_auction" in self._codes(RiskEngine(fresh_state).check(just_open))

    def test_news_blackout_blocks(self, fresh_state):
        release = datetime(2026, 7, 27, 10, 0, tzinfo=ET)
        engine = RiskEngine(fresh_state, RiskLimits(), news_times=[release])
        assert "news_blackout" in self._codes(engine.check(release + timedelta(minutes=5)))
        assert "news_blackout" not in self._codes(engine.check(release + timedelta(minutes=45)))

    def test_lunch_is_soft_not_hard(self, fresh_state):
        lunch = datetime(2026, 7, 27, 12, 30, tzinfo=ET)
        blockers = RiskEngine(fresh_state).check(lunch)
        lunch_blockers = [b for b in blockers if b.code == "lunch_chop"]
        assert lunch_blockers and lunch_blockers[0].severity is Severity.SOFT

    def test_hard_blockers_sort_first(self, fresh_state):
        lunch = datetime(2026, 7, 27, 12, 30, tzinfo=ET)
        fresh_state.close_trade(-600.0)
        blockers = RiskEngine(fresh_state, RiskLimits(daily_loss_limit=600.0)).check(lunch)
        assert blockers[0].is_hard


class TestPayoutRules:
    """The 50% consistency rule, qualifying days, minimum amount, lifetime cap."""

    @staticmethod
    def _trade_days(state, profits: list[float], start_day: int = 20) -> None:
        """Book one profit per day, rolling the session between each."""
        day = datetime(2026, 7, start_day).date()
        for i, profit in enumerate(profits):
            state.session_date = day + timedelta(days=i)
            state.close_trade(profit)
            state.end_session(day + timedelta(days=i + 1))

    def test_one_big_day_raises_the_bar_rather_than_disqualifying(self, fresh_state):
        self._trade_days(fresh_state, [1_500.0, 500.0])
        status = payout_status(fresh_state)
        assert status.best_day == pytest.approx(1_500.0)
        assert status.total_profit == pytest.approx(2_000.0)
        # Under the 50% rule, $1,500 needs $3,000 of total profit.
        assert status.required_total == pytest.approx(3_000.0)
        assert not status.compliant

    def test_compliant_when_spread_across_days(self, fresh_state):
        self._trade_days(fresh_state, [800.0, 700.0, 900.0, 700.0])
        status = payout_status(fresh_state)
        assert status.best_day == pytest.approx(900.0)
        assert status.total_profit == pytest.approx(3_100.0)
        # 900 < 50% of 3,100, so the rule is satisfied.
        assert status.compliant

    def test_exactly_fifty_percent_fails(self, fresh_state):
        """The rule bites at '50% or more', so equality is not compliant."""
        self._trade_days(fresh_state, [1_000.0, 1_000.0])
        status = payout_status(fresh_state)
        assert status.best_day == pytest.approx(1_000.0)
        assert status.total_profit == pytest.approx(2_000.0)
        assert not status.compliant

    def test_headroom_for_today_is_reported(self, fresh_state):
        self._trade_days(fresh_state, [600.0, 600.0])
        fresh_state.session_date = datetime(2026, 7, 24).date()
        fresh_state.close_trade(200.0)
        status = payout_status(fresh_state)
        # Banking more today pushes today toward being half the (larger) total.
        assert status.max_additional_today is not None
        assert status.max_additional_today >= 0

    def test_qualifying_days_must_reach_the_minimum(self, fresh_state):
        self._trade_days(fresh_state, [200.0, 200.0, 200.0])
        status = payout_status(fresh_state)
        assert status.required_days == 5
        assert status.qualifying_days == 3
        assert not status.days_ok
        assert not status.eligible

    def test_days_below_the_daily_minimum_do_not_qualify(self, fresh_state):
        # $10 days fall under the $50 qualifying threshold.
        self._trade_days(fresh_state, [10.0, 10.0, 10.0, 10.0, 10.0])
        assert payout_status(fresh_state).qualifying_days == 0

    def test_losing_days_do_not_qualify(self, fresh_state):
        self._trade_days(fresh_state, [300.0, -100.0, 300.0])
        assert payout_status(fresh_state).qualifying_days == 2

    def test_only_profit_above_the_safety_net_is_withdrawable(self, fresh_state):
        self._trade_days(fresh_state, [400.0] * 5)  # balance 52,000
        status = payout_status(fresh_state)
        # Safety net is $50,100, so $1,900 is withdrawable rather than $2,000.
        assert status.withdrawable == pytest.approx(1_900.0)

    def test_below_the_safety_net_nothing_is_withdrawable(self, fresh_state):
        self._trade_days(fresh_state, [50.0] * 5)  # balance 50,250
        status = payout_status(fresh_state)
        assert status.withdrawable == pytest.approx(150.0)
        assert not status.amount_ok  # under the $500 minimum
        assert not status.eligible

    def test_fully_eligible_account(self, fresh_state):
        self._trade_days(fresh_state, [350.0] * 6)  # 6 days, balance 52,100
        status = payout_status(fresh_state)
        assert status.compliant and status.days_ok and status.amount_ok
        assert status.cap_ok and status.eligible
        assert "eligible" in status.message.lower()

    def test_lifetime_cap_blocks_further_payouts(self, fresh_state):
        self._trade_days(fresh_state, [350.0] * 6)
        fresh_state.payouts_taken = 6
        status = payout_status(fresh_state)
        assert not status.cap_ok
        assert not status.eligible
        assert "lifetime" in status.message

    def test_payout_resets_the_consistency_window(self, fresh_state):
        """The rule is measured since the last payout, not since account open."""
        self._trade_days(fresh_state, [2_000.0, 100.0])
        assert not payout_status(fresh_state).compliant  # 2,000 dwarfs the total

        fresh_state.record_payout(500.0, on_date=datetime(2026, 7, 22).date())
        self._trade_days(fresh_state, [300.0, 300.0, 300.0], start_day=23)

        status = payout_status(fresh_state)
        # The outsized day predates the payout and no longer counts.
        assert status.best_day == pytest.approx(300.0)
        assert status.total_profit == pytest.approx(900.0)
        assert status.compliant

    def test_payout_reduces_balance_and_room(self, fresh_state):
        self._trade_days(fresh_state, [400.0] * 5)
        before_threshold = fresh_state.threshold
        before_room = fresh_state.room

        fresh_state.record_payout(1_000.0)

        assert fresh_state.closed_balance == pytest.approx(51_000.0)
        # A withdrawal cannot lower the threshold, so room falls by the amount.
        assert fresh_state.threshold == pytest.approx(before_threshold)
        assert fresh_state.room == pytest.approx(before_room - 1_000.0)
        assert fresh_state.payouts_taken == 1

    def test_payout_must_be_positive(self, fresh_state):
        with pytest.raises(ValueError):
            fresh_state.record_payout(-100.0)

    def test_evaluation_profile_has_no_payout_requirements(self):
        state = AccountState.fresh(APEX_50K_EVAL)
        status = payout_status(state)
        assert status.compliant
        assert status.required_days == 0


class TestEvaluationProfile:
    def test_matches_published_evaluation_rules(self):
        assert APEX_50K_EVAL.profit_target == pytest.approx(3_000.0)
        assert APEX_50K_EVAL.drawdown_amount == pytest.approx(2_000.0)
        assert APEX_50K_EVAL.initial_threshold == pytest.approx(48_000.0)
        # No consistency rule and no firm daily loss limit during evaluation.
        assert APEX_50K_EVAL.consistency_pct is None
        assert APEX_50K_EVAL.firm_daily_loss_limit is None

    def test_unset_lock_never_stops_trailing(self):
        """Conservative default: understate room rather than overstate it."""
        state = AccountState.fresh(APEX_50K_EVAL)
        state.mark(10_000.0)
        assert state.threshold == pytest.approx(58_000.0)
        assert not state.threshold_is_locked
        assert state.profit_to_lock_threshold() is None


class TestFirmDailyLossLimit:
    """A PA tier's daily loss limit ends the account, unlike a self-imposed one."""

    def _profile(self, limit: float):
        from dataclasses import replace

        return replace(APEX_50K_INTRADAY, firm_daily_loss_limit=limit)

    def test_breach_is_reported_distinctly(self, rth_open):
        state = AccountState.fresh(self._profile(1_000.0))
        state.close_trade(-1_000.0)
        codes = {b.code for b in RiskEngine(state, RiskLimits(daily_loss_limit=5_000.0)).check(rth_open)}
        assert "firm_daily_loss_limit" in codes

    def test_caps_position_size(self, rth_open):
        state = AccountState.fresh(self._profile(100.0))
        engine = RiskEngine(state, RiskLimits(max_risk_per_trade=10_000.0, max_risk_pct_of_room=10.0))
        sizing = engine.size_position(MNQ, 8.0)
        assert sizing.limiting_factor == "firm_daily_loss_limit"
        assert sizing.risk_dollars <= 100.0

    def test_absent_by_default_during_evaluation(self, rth_open):
        state = AccountState.fresh(APEX_50K_EVAL)
        state.close_trade(-5_000.0)
        codes = {b.code for b in RiskEngine(state, RiskLimits(daily_loss_limit=10_000.0)).check(rth_open)}
        assert "firm_daily_loss_limit" not in codes


class TestPayoutHistoryIntegrity:
    """A day's contribution must survive a mid-day session reset."""

    def test_reset_day_does_not_erase_the_days_profit(self, fresh_state):
        fresh_state.session_date = datetime(2026, 7, 20).date()
        fresh_state.close_trade(420.0)
        assert payout_status(fresh_state).qualifying_days == 1

        # Restarting the session mid-day resets day_start_balance; the recorded
        # daily P&L must not be discarded along with it.
        fresh_state.end_session(datetime(2026, 7, 20).date())
        status = payout_status(fresh_state)
        assert status.qualifying_days == 1
        assert status.best_day == pytest.approx(420.0)

    def test_five_calendar_days_qualify(self, fresh_state):
        for i in range(5):
            fresh_state.session_date = datetime(2026, 7, 20).date() + timedelta(days=i)
            fresh_state.close_trade(420.0)
            fresh_state.end_session(datetime(2026, 7, 21).date() + timedelta(days=i))
        status = payout_status(fresh_state)
        assert status.qualifying_days == 5
        assert status.days_ok

    def test_explicit_today_profit_still_overrides(self, fresh_state):
        fresh_state.session_date = datetime(2026, 7, 20).date()
        fresh_state.close_trade(100.0)
        assert payout_status(fresh_state, today_profit=900.0).best_day == pytest.approx(900.0)


class TestScalingLadder:
    """Tier-based contract and daily-loss limits, derived from balance."""

    LADDER = [
        {"balance": 50_000, "contracts": 2, "daily_loss": 1_000},
        {"balance": 51_000, "contracts": 4, "daily_loss": 1_250},
        {"balance": 52_000, "contracts": 7, "daily_loss": 1_500},
        {"balance": 53_000, "contracts": 10, "daily_loss": 2_000},
    ]

    def _profile(self):
        from dataclasses import replace

        return replace(APEX_50K_INTRADAY, scaling=ScalingLadder.from_rows(self.LADDER))

    def test_tier_selected_by_balance(self):
        ladder = ScalingLadder.from_rows(self.LADDER)
        assert ladder.for_balance(50_500).max_contracts == 2
        assert ladder.for_balance(51_000).max_contracts == 4
        assert ladder.for_balance(52_999).max_contracts == 7
        assert ladder.for_balance(99_999).max_contracts == 10

    def test_below_the_lowest_rung_still_has_limits(self):
        """An account under its opening balance is at the bottom, not exempt."""
        ladder = ScalingLadder.from_rows(self.LADDER)
        assert ladder.for_balance(48_500).max_contracts == 2
        assert ladder.for_balance(48_500).daily_loss_limit == 1_000

    def test_rows_are_sorted_regardless_of_input_order(self):
        ladder = ScalingLadder.from_rows(list(reversed(self.LADDER)))
        assert [t.max_contracts for t in ladder.tiers] == [2, 4, 7, 10]

    def test_empty_ladder_is_rejected(self):
        with pytest.raises(ValueError, match="at least one tier"):
            ScalingLadder(())

    def test_malformed_row_is_rejected(self):
        with pytest.raises(ValueError, match="bad scaling tier"):
            ScalingLadder.from_rows([{"balance": 1, "contracts": "many"}])

    def test_intraday_profit_does_not_raise_todays_allowance(self):
        """The trap: the tier comes from yesterday's close, not today's equity."""
        state = AccountState.fresh(self._profile())
        assert state.contract_cap(MNQ) == 20  # 2 minis

        state.close_trade(1_500.0)   # balance now 51,500, would be tier 2
        assert state.contract_cap(MNQ) == 20  # still today's tier

        state.end_session(datetime(2026, 7, 28).date())
        assert state.contract_cap(MNQ) == 40  # 4 minis, from tomorrow

    def test_a_losing_day_demotes_the_tier(self):
        state = AccountState.fresh(self._profile())
        state.close_trade(2_500.0)
        state.end_session(datetime(2026, 7, 28).date())
        assert state.contract_cap(MNQ) == 70   # 52,500 -> 7 minis

        state.close_trade(-1_000.0)            # back to 51,500
        state.end_session(datetime(2026, 7, 29).date())
        assert state.contract_cap(MNQ) == 40   # demoted to 4 minis

    def test_daily_loss_limit_moves_with_the_tier(self):
        state = AccountState.fresh(self._profile())
        assert state.firm_daily_loss_limit() == pytest.approx(1_000.0)
        state.close_trade(3_200.0)
        state.end_session(datetime(2026, 7, 28).date())
        assert state.firm_daily_loss_limit() == pytest.approx(2_000.0)

    def test_tier_daily_loss_limit_is_enforced(self, rth_open):
        state = AccountState.fresh(self._profile())
        state.close_trade(-1_000.0)   # exactly the tier-1 limit
        codes = {
            b.code
            for b in RiskEngine(state, RiskLimits(daily_loss_limit=99_000.0)).check(rth_open)
        }
        assert "firm_daily_loss_limit" in codes

    def test_tier_caps_position_size(self):
        state = AccountState.fresh(self._profile())
        engine = RiskEngine(
            state,
            RiskLimits(
                max_risk_per_trade=100_000.0,
                max_risk_pct_of_room=10.0,
                daily_loss_limit=100_000.0,
            ),
        )
        assert engine.size_position(MNQ, 8.0).quantity == 20  # 2 minis of micros

    def test_current_tier_is_reported(self):
        state = AccountState.fresh(self._profile())
        assert state.current_tier.max_contracts == 2
        assert AccountState.fresh(APEX_50K_INTRADAY).current_tier is None

    def test_no_ladder_falls_back_to_the_flat_cap(self):
        state = AccountState.fresh(APEX_50K_INTRADAY)
        assert state.contract_cap(MNQ) == 100  # 10 minis
        assert state.firm_daily_loss_limit() is None


class TestRiskStyles:
    """Limits scaled to the drawdown allowance, not to the account label.

    A $600 daily cap is prudent on a $10,000 buffer and reckless on a $2,000
    one. Only the ratio makes that visible, so only the ratio is configured.
    """

    def test_conservative_survives_more_losing_days(self):
        conservative = RiskLimits.for_drawdown(2_000, RiskStyle.CONSERVATIVE)
        aggressive = RiskLimits.for_drawdown(2_000, RiskStyle.AGGRESSIVE)
        assert conservative.days_to_breach(2_000) > aggressive.days_to_breach(2_000)
        # Roughly a week of full stop-out days versus under three.
        assert conservative.days_to_breach(2_000) >= 6.0
        assert aggressive.days_to_breach(2_000) < 3.5

    def test_conservative_defaults_for_a_two_thousand_drawdown(self):
        limits = RiskLimits.for_drawdown(2_000, RiskStyle.CONSERVATIVE)
        assert limits.max_risk_per_trade == pytest.approx(100.0)
        assert limits.daily_loss_limit == pytest.approx(300.0)
        assert limits.daily_profit_lock == pytest.approx(400.0)
        assert limits.min_room_to_trade == pytest.approx(500.0)
        assert limits.threshold_safety_buffer == pytest.approx(200.0)
        assert limits.max_trades_per_day == 3
        assert limits.max_consecutive_losses == 2

    def test_limits_scale_with_the_allowance(self):
        small = RiskLimits.for_drawdown(2_000, RiskStyle.CONSERVATIVE)
        large = RiskLimits.for_drawdown(10_000, RiskStyle.CONSERVATIVE)
        assert large.daily_loss_limit == pytest.approx(5 * small.daily_loss_limit)
        # The survivability is identical, which is the point of scaling.
        assert large.days_to_breach(10_000) == pytest.approx(small.days_to_breach(2_000))

    def test_styles_are_ordered(self):
        drawdown = 2_000
        risks = [
            RiskLimits.for_drawdown(drawdown, s).max_risk_per_trade
            for s in (RiskStyle.CONSERVATIVE, RiskStyle.BALANCED, RiskStyle.AGGRESSIVE)
        ]
        assert risks == sorted(risks)

    def test_overrides_win(self):
        limits = RiskLimits.for_drawdown(
            2_000, RiskStyle.CONSERVATIVE, daily_loss_limit=125.0
        )
        assert limits.daily_loss_limit == pytest.approx(125.0)
        assert limits.max_risk_per_trade == pytest.approx(100.0)  # still derived

    def test_rejects_a_nonpositive_drawdown(self):
        with pytest.raises(ValueError):
            RiskLimits.for_drawdown(0)

    def test_days_to_breach_handles_no_limit(self):
        assert RiskLimits(daily_loss_limit=0.0).days_to_breach(2_000) == float("inf")

    def test_default_limits_are_the_conservative_ones(self):
        """The out-of-the-box setting must be the safe one, not the middle one."""
        default = RiskLimits()
        conservative = RiskLimits.for_drawdown(2_000, RiskStyle.CONSERVATIVE)
        assert default.max_risk_per_trade == conservative.max_risk_per_trade
        assert default.daily_loss_limit == conservative.daily_loss_limit
        assert default.max_trades_per_day == conservative.max_trades_per_day
