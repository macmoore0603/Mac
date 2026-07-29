"""Market context: every indicator the playbook needs, evaluated at one bar.

`MarketContext.build` computes the full indicator stack once and exposes it at a
single evaluation index — by default the last *closed* bar. Decisions are made
on closed bars only. Acting on a forming bar means acting on a value that can
still change before the bar prints, which is the most common way a strategy that
backtested well falls apart live.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import time
from enum import Enum

from .bars import Bar, Session, classify_session, minutes_into_session, trading_date
from .contracts import ContractSpec
from .indicators import (
    ADXResult,
    OpeningRange,
    Pivots,
    PriorDayLevels,
    Series,
    VWAPResult,
    adx,
    atr,
    ema,
    opening_range,
    percentile_rank,
    prior_day_levels,
    resample,
    rsi,
    session_vwap,
    slope_per_bar,
    swing_pivots,
)


class Regime(Enum):
    """Coarse market state. Determines which setups are even allowed.

    Trading a mean-reversion setup in a strong trend, or a breakout in chop, is
    how a decent setup becomes a losing one. The regime gate is the single
    highest-value filter in the system.
    """

    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    CHOP = "chop"
    EXPANSION = "expansion"   # volatility spike; wide stops, reduce size
    QUIET = "quiet"           # compressed range; expect failure of breakouts

    @property
    def is_trending(self) -> bool:
        return self in (Regime.TREND_UP, Regime.TREND_DOWN)


def at(series: Series, i: int) -> float | None:
    """Read a padded series safely at index `i`."""
    if i < 0 or i >= len(series):
        return None
    return series[i]


@dataclass
class IndicatorConfig:
    """Tunable indicator parameters.

    Defaults are conventional rather than optimised. Parameters tuned to a
    specific historical window are the classic way to build something that looks
    excellent in backtest and is worthless forward — if you change these, change
    them for a structural reason, not to improve a backtest number.
    """

    ema_fast: int = 9
    ema_mid: int = 21
    ema_slow: int = 50
    atr_length: int = 14
    rsi_length: int = 14
    adx_length: int = 14
    atr_rank_window: int = 100
    slope_window: int = 10
    pivot_left: int = 3
    pivot_right: int = 3
    vwap_bands: tuple[float, ...] = (1.0, 2.0, 3.0)
    opening_range_start: time = time(9, 30)
    opening_range_end: time = time(10, 0)
    htf_minutes: int = 15
    htf_ema: int = 21
    adx_trend_threshold: float = 23.0
    adx_chop_threshold: float = 18.0
    expansion_rank: float = 0.90
    quiet_rank: float = 0.15


@dataclass
class MarketContext:
    """All computed state for a single decision point."""

    bars: list[Bar]
    spec: ContractSpec
    config: IndicatorConfig
    index: int

    ema_fast: Series
    ema_mid: Series
    ema_slow: Series
    atr: Series
    atr_rank: Series
    rsi: Series
    dmi: ADXResult
    vwap: VWAPResult
    orb: OpeningRange
    pivots: Pivots
    prior_day: PriorDayLevels
    ema_mid_slope: Series
    # Higher-timeframe trend at every bar: +1 bullish, -1 bearish, 0 neutral.
    # Stored as a series so a whole run can share one context (see `at_index`).
    htf_bias_series: list[int]

    @property
    def htf_bias(self) -> int:
        return self.htf_bias_series[self.index]

    def at_index(self, index: int) -> "MarketContext":
        """A view of this context at another bar, sharing the computed series.

        Rebuilding the indicator stack per bar is O(n^2) and makes replaying a
        few months of 5-minute data impractical. Every series here is already
        causal, so moving the evaluation index is sufficient and cannot leak
        future information.
        """
        if not 0 <= index < len(self.bars):
            raise IndexError(f"index {index} out of range for {len(self.bars)} bars")
        return replace(self, index=index)

    @classmethod
    def build(
        cls,
        bars: list[Bar],
        spec: ContractSpec,
        config: IndicatorConfig | None = None,
        index: int | None = None,
    ) -> "MarketContext":
        """Compute the indicator stack. `index` defaults to the last bar."""
        cfg = config or IndicatorConfig()
        if not bars:
            raise ValueError("cannot build context from an empty bar series")
        idx = len(bars) - 1 if index is None else index
        if not 0 <= idx < len(bars):
            raise IndexError(f"index {idx} out of range for {len(bars)} bars")

        closes = [b.close for b in bars]
        atr_series = atr(bars, cfg.atr_length)
        ema_mid_series = ema(closes, cfg.ema_mid)

        return cls(
            bars=bars,
            spec=spec,
            config=cfg,
            index=idx,
            ema_fast=ema(closes, cfg.ema_fast),
            ema_mid=ema_mid_series,
            ema_slow=ema(closes, cfg.ema_slow),
            atr=atr_series,
            atr_rank=percentile_rank(atr_series, cfg.atr_rank_window),
            rsi=rsi(closes, cfg.rsi_length),
            dmi=adx(bars, cfg.adx_length),
            vwap=session_vwap(bars, cfg.vwap_bands),
            orb=opening_range(bars, cfg.opening_range_start, cfg.opening_range_end),
            pivots=swing_pivots(bars, cfg.pivot_left, cfg.pivot_right),
            prior_day=prior_day_levels(bars),
            ema_mid_slope=slope_per_bar(ema_mid_series, cfg.slope_window),
            htf_bias_series=_htf_bias_series(bars, cfg),
        )

    # -- convenience accessors at the evaluation index ---------------------

    @property
    def bar(self) -> Bar:
        return self.bars[self.index]

    @property
    def price(self) -> float:
        return self.bar.close

    @property
    def session(self) -> Session:
        return classify_session(self.bar.ts)

    @property
    def minutes_since_open(self) -> float:
        return minutes_into_session(self.bar.ts)

    @property
    def current_atr(self) -> float | None:
        return at(self.atr, self.index)

    @property
    def current_vwap(self) -> float | None:
        return at(self.vwap.vwap, self.index)

    @property
    def current_adx(self) -> float | None:
        return at(self.dmi.adx, self.index)

    @property
    def current_rsi(self) -> float | None:
        return at(self.rsi, self.index)

    @property
    def is_ready(self) -> bool:
        """True when every indicator the playbook depends on has warmed up."""
        required = (
            at(self.ema_fast, self.index),
            at(self.ema_mid, self.index),
            at(self.ema_slow, self.index),
            at(self.atr, self.index),
            at(self.dmi.adx, self.index),
            at(self.vwap.vwap, self.index),
        )
        return all(v is not None for v in required)

    def warmup_deficit(self) -> int:
        """How many more bars are needed before a decision can be made."""
        cfg = self.config
        needed = max(cfg.ema_slow, cfg.atr_length + cfg.adx_length * 2, cfg.rsi_length + 1)
        return max(0, needed - (self.index + 1))

    def atr_points(self, fallback: float = 20.0) -> float:
        a = self.current_atr
        return a if a is not None else fallback

    def vwap_distance_sigma(self) -> float | None:
        """How many volume-weighted standard deviations price sits from VWAP."""
        vw = self.current_vwap
        sd = at(self.vwap.stdev, self.index)
        if vw is None or sd is None or sd <= 0:
            return None
        return (self.price - vw) / sd

    def ema_stack(self) -> int:
        """+1 when EMAs are stacked bullishly, -1 bearishly, 0 when tangled."""
        f = at(self.ema_fast, self.index)
        m = at(self.ema_mid, self.index)
        s = at(self.ema_slow, self.index)
        if f is None or m is None or s is None:
            return 0
        if f > m > s:
            return 1
        if f < m < s:
            return -1
        return 0

    def regime(self) -> Regime:
        """Classify the current market state.

        Order matters: a volatility spike overrides trend classification,
        because position sizing and stop distance must adapt before direction
        is even considered.
        """
        rank = at(self.atr_rank, self.index)
        adx_v = self.current_adx
        plus = at(self.dmi.plus_di, self.index)
        minus = at(self.dmi.minus_di, self.index)
        cfg = self.config

        if rank is not None and rank >= cfg.expansion_rank:
            return Regime.EXPANSION
        if rank is not None and rank <= cfg.quiet_rank:
            return Regime.QUIET
        if adx_v is None or plus is None or minus is None:
            return Regime.CHOP
        if adx_v >= cfg.adx_trend_threshold:
            stack = self.ema_stack()
            if plus > minus and stack >= 0:
                return Regime.TREND_UP
            if minus > plus and stack <= 0:
                return Regime.TREND_DOWN
        if adx_v < cfg.adx_chop_threshold:
            return Regime.CHOP
        return Regime.CHOP

    def structural_stop(self, is_long: bool, lookback: int = 10) -> float | None:
        """Nearest defensible structural level for a stop.

        Prefers the last confirmed swing point; falls back to the extreme of the
        recent bars when no pivot has been confirmed yet.
        """
        if is_long:
            pivot = at(self.pivots.last_low, self.index)
            start = max(0, self.index - lookback + 1)
            recent = min(b.low for b in self.bars[start : self.index + 1])
            if pivot is None:
                return recent
            return min(pivot, recent)
        pivot = at(self.pivots.last_high, self.index)
        start = max(0, self.index - lookback + 1)
        recent = max(b.high for b in self.bars[start : self.index + 1])
        if pivot is None:
            return recent
        return max(pivot, recent)

    def relative_volume(self, lookback: int = 20) -> float | None:
        """Current bar volume divided by the recent average.

        Returns None when the feed carries no volume, which is common on
        free/delayed data — the playbook then simply skips volume confirmation
        rather than treating zero as low participation.
        """
        start = max(0, self.index - lookback)
        window = [b.volume for b in self.bars[start : self.index]]
        if not window:
            return None
        avg = sum(window) / len(window)
        if avg <= 0:
            return None
        return self.bar.volume / avg


class _StreamingEMA:
    """EMA maintained incrementally, seeded with an SMA like `indicators.ema`."""

    def __init__(self, length: int) -> None:
        self.length = length
        self._k = 2.0 / (length + 1.0)
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.length:
                self.value = sum(self._seed) / self.length
        else:
            self.value = x * self._k + self.value * (1.0 - self._k)
        return self.value


def _htf_bias_series(bars: list[Bar], cfg: IndicatorConfig) -> list[int]:
    """Higher-timeframe trend direction at every bar, from closed HTF bars only.

    Trading against the 15-minute trend on a 5-minute signal is a recognisable
    way to lose slowly, so this feeds the score of every setup.

    A higher-timeframe bar only becomes visible once the next one starts, so the
    bias at bar `i` reflects buckets that had genuinely finished by then. Built
    in a single streaming pass to keep whole-series construction linear.
    """
    n = len(bars)
    out = [0] * n
    if n == 0:
        return out

    trend_ema = _StreamingEMA(cfg.htf_ema)
    recent: list[float] = []       # trailing EMA values, for the slope check
    bias = 0
    bucket_key: tuple | None = None
    bucket_last_close: float | None = None

    for i, bar in enumerate(bars):
        et = bar.et
        key = (et.date(), (et.hour * 60 + et.minute) // cfg.htf_minutes)

        if bucket_key is None:
            bucket_key = key
        elif key != bucket_key:
            # The previous bucket has closed and may now be acted upon.
            value = trend_ema.update(bucket_last_close)
            if value is not None:
                recent.append(value)
                if len(recent) > 5:
                    recent.pop(0)
                bias = _bias_from(bucket_last_close, value, recent)
            bucket_key = key

        bucket_last_close = bar.close
        out[i] = bias

    return out


def _bias_from(last_close: float, ema_value: float, recent: list[float]) -> int:
    """Direction from price vs its EMA, requiring the EMA slope to agree."""
    slope = None
    if len(recent) >= 2:
        length = len(recent)
        xs = list(range(length))
        x_mean = sum(xs) / length
        y_mean = sum(recent) / length
        x_var = sum((x - x_mean) ** 2 for x in xs)
        if x_var:
            slope = sum((xs[j] - x_mean) * (recent[j] - y_mean) for j in range(length)) / x_var

    if last_close > ema_value and (slope is None or slope >= 0):
        return 1
    if last_close < ema_value and (slope is None or slope <= 0):
        return -1
    return 0
