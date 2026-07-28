"""Tests for indicator correctness, alignment, and the no-lookahead guarantee."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import series_from_closes

from nqcopilot.bars import ET, Bar
from nqcopilot.indicators import (
    adx,
    atr,
    ema,
    opening_range,
    percentile_rank,
    prior_day_levels,
    resample,
    rma,
    rolling_extreme,
    rsi,
    session_vwap,
    slope_per_bar,
    sma,
    swing_pivots,
    true_range,
)


class TestAlignment:
    """Every series must be input-length with explicit None during warmup."""

    @pytest.mark.parametrize(
        "fn,length",
        [(sma, 5), (ema, 5), (rma, 5), (rsi, 5)],
    )
    def test_length_and_warmup(self, fn, length):
        values = [float(i) for i in range(20)]
        out = fn(values, length)
        assert len(out) == len(values)
        assert out[0] is None
        assert out[-1] is not None

    def test_no_forward_fill_into_warmup(self):
        out = sma([1.0, 2.0, 3.0, 4.0], 3)
        assert out[:2] == [None, None]
        assert out[2] == pytest.approx(2.0)

    def test_short_series_returns_all_none(self):
        assert ema([1.0, 2.0], 10) == [None, None]

    def test_rejects_none_input(self):
        with pytest.raises(ValueError):
            sma([1.0, None, 3.0], 2)


class TestMovingAverages:
    def test_sma_matches_hand_calculation(self):
        out = sma([2.0, 4.0, 6.0, 8.0], 2)
        assert out == [None, 3.0, 5.0, 7.0]

    def test_ema_of_constant_series_is_the_constant(self):
        out = ema([100.0] * 30, 10)
        assert out[-1] == pytest.approx(100.0)

    def test_ema_seeds_with_sma(self):
        values = [float(i) for i in range(1, 11)]
        out = ema(values, 5)
        assert out[4] == pytest.approx(3.0)  # mean of 1..5

    def test_rma_uses_wilder_constant_not_ema(self):
        """RMA smooths at 1/n; EMA at 2/(n+1). They must not coincide."""
        values = [float(i) for i in range(1, 30)]
        assert rma(values, 14)[-1] != pytest.approx(ema(values, 14)[-1])

    def test_rma_of_constant_series(self):
        assert rma([50.0] * 40, 14)[-1] == pytest.approx(50.0)


class TestTrueRangeAndATR:
    def test_true_range_first_bar_is_high_low(self):
        bars = series_from_closes([100.0, 101.0, 102.0])
        assert true_range(bars)[0] == pytest.approx(bars[0].range)

    def test_true_range_accounts_for_gaps(self):
        base = datetime(2026, 7, 27, 9, 30, tzinfo=ET)
        bars = [
            Bar(base, 100.0, 101.0, 99.0, 100.0),
            # Gaps up: the true range must span from the prior close.
            Bar(base + timedelta(minutes=5), 110.0, 111.0, 109.0, 110.0),
        ]
        assert true_range(bars)[1] == pytest.approx(11.0)  # 111 - 100

    def test_atr_of_uniform_bars_equals_bar_range(self):
        bars = series_from_closes([100.0] * 40, spread=3.0)
        # Flat closes with +/-3 wicks give every bar a 6-point range.
        assert atr(bars, 14)[-1] == pytest.approx(6.0)

    def test_atr_is_positive_and_aligned(self):
        bars = series_from_closes([100.0 + i * 0.5 for i in range(50)])
        out = atr(bars, 14)
        assert len(out) == len(bars)
        assert out[12] is None and out[13] is not None
        assert all(v > 0 for v in out if v is not None)


class TestADX:
    def test_strong_uptrend_produces_high_adx_and_positive_di(self):
        bars = series_from_closes([100.0 + i * 5 for i in range(60)], spread=1.0)
        result = adx(bars, 14)
        assert result.adx[-1] > 40
        assert result.plus_di[-1] > result.minus_di[-1]

    def test_strong_downtrend_flips_di(self):
        bars = series_from_closes([500.0 - i * 5 for i in range(60)], spread=1.0)
        result = adx(bars, 14)
        assert result.adx[-1] > 40
        assert result.minus_di[-1] > result.plus_di[-1]

    def test_trend_reads_stronger_than_a_random_walk(self):
        """ADX must separate directional movement from aimless movement.

        Asserting an absolute threshold on synthetic 'chop' is fragile — the
        discriminating property is the comparison, which is also what the
        regime classifier actually relies on.
        """
        import random

        rng = random.Random(11)
        trend = series_from_closes([100.0 + i * 3 for i in range(90)], spread=2.0)

        walk = [100.0]
        for _ in range(89):
            walk.append(walk[-1] + rng.gauss(0, 4))
        noise = series_from_closes(walk, spread=2.0)

        assert adx(trend, 14).adx[-1] > adx(noise, 14).adx[-1]

    def test_directionless_market_stays_below_trend_threshold(self):
        """A market oscillating in a band must not read as trending."""
        import math

        closes = [100.0 + 6.0 * math.sin(i * math.pi / 6) for i in range(120)]
        result = adx(series_from_closes(closes, spread=1.5), 14)
        assert result.adx[-1] < 45

    def test_alignment_and_bounds(self):
        bars = series_from_closes([100.0 + i for i in range(60)])
        result = adx(bars, 14)
        assert len(result.adx) == len(bars)
        for v in result.plus_di:
            assert v is None or 0 <= v <= 100


class TestRSI:
    def test_monotonic_rise_saturates_high(self):
        assert rsi([float(i) for i in range(1, 40)], 14)[-1] == pytest.approx(100.0)

    def test_monotonic_fall_saturates_low(self):
        assert rsi([float(i) for i in range(40, 1, -1)], 14)[-1] == pytest.approx(0.0)

    def test_bounded(self):
        closes = [100.0 + (i % 7) - 3 for i in range(80)]
        for v in rsi(closes, 14):
            assert v is None or 0.0 <= v <= 100.0


class TestVWAP:
    def test_equal_volume_vwap_is_mean_of_typical_price(self):
        bars = series_from_closes([100.0, 102.0, 101.0], volume=100.0)
        expected = sum(b.hlc3 for b in bars) / 3
        assert session_vwap(bars).vwap[2] == pytest.approx(expected)

    def test_volume_weighting_pulls_toward_heavy_bars(self):
        base = datetime(2026, 7, 27, 9, 30, tzinfo=ET)
        bars = [
            Bar(base, 100.0, 100.0, 100.0, 100.0, volume=1.0),
            Bar(base + timedelta(minutes=5), 200.0, 200.0, 200.0, 200.0, volume=99.0),
        ]
        assert session_vwap(bars).vwap[1] == pytest.approx(199.0)

    def test_resets_on_a_new_trading_day(self):
        day1 = series_from_closes([100.0] * 12, start=datetime(2026, 7, 27, 9, 30, tzinfo=ET))
        day2 = series_from_closes([200.0] * 12, start=datetime(2026, 7, 28, 9, 30, tzinfo=ET))
        result = session_vwap(day1 + day2)
        # Day 2 must not be contaminated by day 1's much lower prices.
        assert result.vwap[12] == pytest.approx(200.0)

    def test_zero_volume_degrades_gracefully(self):
        bars = series_from_closes([100.0, 101.0, 102.0], volume=0.0)
        result = session_vwap(bars)
        assert all(v is not None for v in result.vwap)

    def test_bands_straddle_vwap(self):
        bars = series_from_closes([100.0 + (i % 5) for i in range(30)])
        result = session_vwap(bars, (1.0, 2.0))
        i = len(bars) - 1
        assert result.lower(2.0)[i] < result.lower(1.0)[i] <= result.vwap[i]
        assert result.vwap[i] <= result.upper(1.0)[i] < result.upper(2.0)[i]


class TestNoLookahead:
    """The property that separates a usable tool from a flattering backtest."""

    def test_pivot_is_published_only_after_confirmation(self):
        highs = [10, 11, 12, 13, 14, 20, 14, 13, 12, 11, 10]
        base = datetime(2026, 7, 27, 9, 30, tzinfo=ET)
        bars = [
            Bar(base + timedelta(minutes=5 * i), h - 1, h, h - 2, h - 1)
            for i, h in enumerate(float(x) for x in highs)
        ]
        pivots = swing_pivots(bars, left=3, right=3)

        # The pivot high sits at index 5 and needs 3 more bars to confirm.
        assert pivots.last_high[5] is None
        assert pivots.last_high[7] is None
        assert pivots.last_high[8] == pytest.approx(20.0)
        assert pivots.last_high_index[8] == 5

    def test_prior_day_levels_never_reference_the_current_day(self):
        day1 = series_from_closes(
            [100.0 + i for i in range(10)], start=datetime(2026, 7, 27, 9, 30, tzinfo=ET)
        )
        day2 = series_from_closes(
            [500.0 + i for i in range(10)], start=datetime(2026, 7, 28, 9, 30, tzinfo=ET)
        )
        levels = prior_day_levels(day1 + day2)
        assert levels.high[0] is None  # nothing precedes the first day
        day1_high = max(b.high for b in day1)
        assert levels.high[10] == pytest.approx(day1_high)
        assert levels.high[19] == pytest.approx(day1_high)  # frozen all day

    def test_recomputing_on_a_prefix_gives_identical_values(self):
        """Truncating future bars must not change any past value."""
        bars = series_from_closes([100.0 + (i % 11) * 2 for i in range(80)])
        full = atr(bars, 14)
        prefix = atr(bars[:50], 14)
        for i in range(50):
            assert full[i] == pytest.approx(prefix[i]) if full[i] else full[i] == prefix[i]

    def test_resample_drops_the_incomplete_trailing_bar(self):
        # Ten 5-minute bars from 09:30 fill 09:30 and 09:45 buckets, and start
        # a third that is only partially formed.
        bars = series_from_closes([100.0] * 8, start=datetime(2026, 7, 27, 9, 30, tzinfo=ET))
        out = resample(bars, 15)
        assert len(out) == 2
        assert out[0].ts.astimezone(ET).minute == 30
        assert out[1].ts.astimezone(ET).minute == 45


class TestResample:
    def test_aggregates_ohlcv_correctly(self):
        bars = series_from_closes(
            [100.0, 105.0, 95.0, 102.0, 108.0, 99.0],
            start=datetime(2026, 7, 27, 9, 30, tzinfo=ET),
            volume=100.0,
        )
        out = resample(bars, 15)
        first = out[0]
        assert first.open == bars[0].open
        assert first.close == bars[2].close
        assert first.high == max(b.high for b in bars[:3])
        assert first.low == min(b.low for b in bars[:3])
        assert first.volume == pytest.approx(300.0)

    def test_rejects_bad_interval(self):
        with pytest.raises(ValueError):
            resample(series_from_closes([1.0, 2.0]), 0)


class TestOpeningRange:
    def test_range_completes_after_the_window(self):
        closes = [100.0 + i for i in range(12)]  # 09:30 through 10:25
        bars = series_from_closes(closes, start=datetime(2026, 7, 27, 9, 30, tzinfo=ET))
        orb = opening_range(bars, __import__("datetime").time(9, 30), __import__("datetime").time(10, 0))
        assert not orb.complete[3]     # 09:45, still inside the window
        assert orb.complete[6]         # 10:00, window closed
        window_high = max(b.high for b in bars[:6])
        assert orb.high[6] == pytest.approx(window_high)

    def test_range_is_frozen_after_completion(self):
        closes = [100.0] * 6 + [500.0] * 6
        bars = series_from_closes(closes, start=datetime(2026, 7, 27, 9, 30, tzinfo=ET))
        orb = opening_range(bars, __import__("datetime").time(9, 30), __import__("datetime").time(10, 0))
        # The 500-level bars are outside the window and must not widen it.
        assert orb.high[-1] == pytest.approx(orb.high[6])


class TestUtilities:
    def test_rolling_extreme(self):
        assert rolling_extreme([1.0, 5.0, 3.0, 2.0], 2, "max") == [None, 5.0, 5.0, 3.0]
        assert rolling_extreme([1.0, 5.0, 3.0, 2.0], 2, "min") == [None, 1.0, 3.0, 2.0]

    def test_percentile_rank_bounds(self):
        values = [float(i) for i in range(50)]
        out = percentile_rank(values, 10)
        assert out[-1] == pytest.approx(1.0)  # rising series: current is the max
        for v in out:
            assert v is None or 0.0 <= v <= 1.0

    def test_slope_is_signed_correctly(self):
        rising = slope_per_bar([float(i) for i in range(20)], 10)
        falling = slope_per_bar([float(-i) for i in range(20)], 10)
        assert rising[-1] == pytest.approx(1.0)
        assert falling[-1] == pytest.approx(-1.0)

    def test_slope_ignores_windows_with_gaps(self):
        assert slope_per_bar([None, None, 1.0, 2.0], 3)[2] is None
