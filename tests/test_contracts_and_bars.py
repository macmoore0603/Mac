"""Tests for contract tick math and session/clock handling."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from conftest import series_from_closes

from nqcopilot.bars import (
    ET,
    Bar,
    Session,
    classify_session,
    is_market_open,
    is_weekend,
    minutes_into_session,
    trading_date,
    validate_series,
)
from nqcopilot.contracts import ES, MES, MNQ, NQ, get_contract, is_micro


class TestContractMath:
    def test_point_values(self):
        assert NQ.point_value == pytest.approx(20.0)
        assert MNQ.point_value == pytest.approx(2.0)
        assert ES.point_value == pytest.approx(50.0)
        assert MES.point_value == pytest.approx(5.0)

    def test_micro_is_one_tenth_of_the_mini(self):
        assert MNQ.point_value == pytest.approx(NQ.point_value / 10)

    def test_dollars_round_trip(self):
        assert NQ.points_to_dollars(10.0, 2) == pytest.approx(400.0)
        assert NQ.dollars_to_points(400.0, 2) == pytest.approx(10.0)

    def test_commission_scales_with_quantity(self):
        assert MNQ.commission(5) == pytest.approx(5 * MNQ.round_turn_cost)

    def test_lookup_is_case_insensitive(self):
        assert get_contract("mnq") is MNQ
        with pytest.raises(KeyError):
            get_contract("AAPL")

    def test_micro_detection(self):
        assert is_micro(MNQ) and not is_micro(NQ)


class TestTickRounding:
    def test_snaps_to_quarter_points(self):
        assert NQ.round_to_tick(20_512.13) == pytest.approx(20_512.25)
        assert NQ.round_to_tick(20_512.12) == pytest.approx(20_512.00)

    def test_directional_modes(self):
        assert NQ.round_to_tick(100.1, "up") == pytest.approx(100.25)
        assert NQ.round_to_tick(100.1, "down") == pytest.approx(100.0)

    def test_exact_ticks_are_unchanged(self):
        for price in (100.0, 100.25, 100.5, 100.75):
            for mode in ("nearest", "up", "down"):
                assert NQ.round_to_tick(price, mode) == pytest.approx(price)

    def test_stops_round_to_the_wider_price(self):
        """Rounding a stop tighter would silently raise the risk of a shakeout."""
        assert NQ.round_stop(20_500.10, is_long=True) == pytest.approx(20_500.00)
        assert NQ.round_stop(20_500.10, is_long=False) == pytest.approx(20_500.25)

    def test_targets_round_to_the_nearer_price(self):
        """Rounding a target further out risks missing the fill by one tick."""
        assert NQ.round_target(20_600.10, is_long=True) == pytest.approx(20_600.00)
        assert NQ.round_target(20_600.10, is_long=False) == pytest.approx(20_600.25)

    def test_rejects_unknown_mode(self):
        with pytest.raises(ValueError):
            NQ.round_to_tick(100.0, "sideways")


class TestBarValidation:
    def test_rejects_naive_timestamps(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            Bar(datetime(2026, 7, 27, 9, 30), 100, 101, 99, 100)

    def test_rejects_inverted_high_low(self):
        with pytest.raises(ValueError):
            Bar(datetime(2026, 7, 27, 9, 30, tzinfo=ET), 100, 98, 102, 100)

    def test_rejects_close_outside_the_range(self):
        with pytest.raises(ValueError):
            Bar(datetime(2026, 7, 27, 9, 30, tzinfo=ET), 100, 101, 99, 105)

    def test_rejects_negative_volume(self):
        with pytest.raises(ValueError):
            Bar(datetime(2026, 7, 27, 9, 30, tzinfo=ET), 100, 101, 99, 100, -5)

    def test_accepts_a_valid_bar(self):
        bar = Bar(datetime(2026, 7, 27, 9, 30, tzinfo=ET), 100, 101, 99, 100.5, 500)
        assert bar.hlc3 == pytest.approx((101 + 99 + 100.5) / 3)
        assert bar.range == pytest.approx(2.0)
        assert bar.is_up

    def test_close_position_within_range(self):
        base = datetime(2026, 7, 27, 9, 30, tzinfo=ET)
        assert Bar(base, 100, 110, 100, 110).close_position() == pytest.approx(1.0)
        assert Bar(base, 100, 110, 100, 100).close_position() == pytest.approx(0.0)
        assert Bar(base, 100, 100, 100, 100).close_position() == pytest.approx(0.5)


class TestSeriesValidation:
    def test_rejects_out_of_order_bars(self):
        bars = series_from_closes([100.0, 101.0, 102.0])
        with pytest.raises(ValueError, match="out of order"):
            validate_series([bars[0], bars[2], bars[1]])

    def test_rejects_duplicates(self):
        bars = series_from_closes([100.0, 101.0])
        with pytest.raises(ValueError):
            validate_series([bars[0], bars[0]])

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_series([])

    def test_detects_an_intraday_gap(self):
        base = datetime(2026, 7, 27, 9, 30, tzinfo=ET)
        bars = [
            Bar(base, 100, 101, 99, 100),
            Bar(base + timedelta(minutes=90), 100, 101, 99, 100),
        ]
        with pytest.raises(ValueError, match="data gap"):
            validate_series(bars, max_gap_minutes=10)

    def test_overnight_gap_is_allowed(self):
        bars = [
            Bar(datetime(2026, 7, 27, 15, 55, tzinfo=ET), 100, 101, 99, 100),
            Bar(datetime(2026, 7, 28, 9, 30, tzinfo=ET), 100, 101, 99, 100),
        ]
        validate_series(bars, max_gap_minutes=10)  # must not raise


class TestSessions:
    @pytest.mark.parametrize(
        "hour,minute,expected",
        [
            (9, 35, Session.OPENING_DRIVE),
            (10, 30, Session.MORNING_TREND),
            (12, 0, Session.LUNCH),
            (14, 0, Session.AFTERNOON),
            (15, 15, Session.POWER_HOUR),
            (15, 55, Session.CLOSING),
            (16, 30, Session.POST_CLOSE),
            (17, 30, Session.MAINTENANCE),
            (20, 0, Session.OVERNIGHT),
            (8, 30, Session.PRE_MARKET),
        ],
    )
    def test_classification(self, hour, minute, expected):
        ts = datetime(2026, 7, 27, hour, minute, tzinfo=ET)
        assert classify_session(ts) is expected

    def test_rth_flag(self):
        assert Session.MORNING_TREND.is_rth
        assert not Session.OVERNIGHT.is_rth
        assert not Session.MAINTENANCE.is_rth

    def test_boundaries_are_inclusive_of_the_start(self):
        assert classify_session(datetime(2026, 7, 27, 9, 30, tzinfo=ET)) is Session.OPENING_DRIVE
        assert classify_session(datetime(2026, 7, 27, 9, 29, tzinfo=ET)) is Session.PRE_MARKET


class TestMarketHours:
    def test_saturday_is_closed(self):
        assert is_weekend(datetime(2026, 7, 25, 12, 0, tzinfo=ET))

    def test_friday_evening_is_closed(self):
        assert is_weekend(datetime(2026, 7, 24, 18, 0, tzinfo=ET))
        assert not is_weekend(datetime(2026, 7, 24, 14, 0, tzinfo=ET))

    def test_sunday_reopens_at_six(self):
        assert is_weekend(datetime(2026, 7, 26, 17, 0, tzinfo=ET))
        assert not is_weekend(datetime(2026, 7, 26, 19, 0, tzinfo=ET))

    def test_daily_halt_is_closed(self):
        assert not is_market_open(datetime(2026, 7, 27, 17, 30, tzinfo=ET))
        assert is_market_open(datetime(2026, 7, 27, 18, 30, tzinfo=ET))


class TestTradingDate:
    def test_daytime_maps_to_the_same_date(self):
        ts = datetime(2026, 7, 27, 10, 0, tzinfo=ET)
        assert trading_date(ts) == ts.date()

    def test_evening_rolls_to_the_next_day(self):
        ts = datetime(2026, 7, 27, 19, 0, tzinfo=ET)
        assert trading_date(ts) == datetime(2026, 7, 28).date()

    def test_friday_evening_rolls_to_monday(self):
        ts = datetime(2026, 7, 24, 19, 0, tzinfo=ET)
        assert trading_date(ts) == datetime(2026, 7, 27).date()

    def test_sunday_evening_is_monday(self):
        ts = datetime(2026, 7, 26, 19, 0, tzinfo=ET)
        assert trading_date(ts) == datetime(2026, 7, 27).date()


class TestSessionClock:
    def test_minutes_into_session(self):
        assert minutes_into_session(datetime(2026, 7, 27, 10, 0, tzinfo=ET)) == pytest.approx(30.0)
        assert minutes_into_session(datetime(2026, 7, 27, 9, 0, tzinfo=ET)) == pytest.approx(-30.0)

    def test_handles_a_non_eastern_input_timezone(self):
        from zoneinfo import ZoneInfo

        utc_ts = datetime(2026, 7, 27, 14, 0, tzinfo=ZoneInfo("UTC"))  # 10:00 ET
        assert classify_session(utc_ts) is Session.MORNING_TREND
        assert minutes_into_session(utc_ts) == pytest.approx(30.0)
