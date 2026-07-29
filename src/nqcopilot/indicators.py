"""Technical indicators, implemented to match TradingView/Wilder conventions.

Two invariants hold throughout this module, and the tests enforce both:

1. **Alignment.** Every function returns a list the same length as its input.
   Warmup positions are `None`, never zero or a forward-filled value. A caller
   that reads index `i` gets either a real value or an explicit "not yet".

2. **No lookahead.** The value at index `i` is computable from bars `0..i` only.
   Swing pivots are the subtle case: a pivot high at bar `i` cannot be confirmed
   until `right` more bars have printed, so it is published at `i + right`.
   Backtests that ignore this produce results you can never reproduce live.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, time

from .bars import Bar, trading_date

Series = list[float | None]


def _require_no_none(values: Sequence[float], name: str) -> None:
    if any(v is None for v in values):
        raise ValueError(f"{name} must not contain None; align inputs first")


def _apply_to_tail(values: Sequence[float | None], fn, *args, **kwargs) -> Series:
    """Run a dense-input function over the non-None tail of a padded series.

    Indicator chains (ATR of TR, ADX of DX) stack warmups. This keeps the
    padding bookkeeping in one place instead of in every caller.
    """
    out: Series = [None] * len(values)
    first = next((i for i, v in enumerate(values) if v is not None), None)
    if first is None:
        return out
    tail = values[first:]
    if any(v is None for v in tail):
        raise ValueError("series has interior gaps; cannot smooth across them")
    result = fn([float(v) for v in tail], *args, **kwargs)
    for i, v in enumerate(result):
        out[first + i] = v
    return out


def sma(values: Sequence[float], length: int) -> Series:
    """Simple moving average."""
    if length <= 0:
        raise ValueError("length must be positive")
    _require_no_none(values, "sma input")
    out: Series = [None] * len(values)
    if len(values) < length:
        return out
    running = sum(values[:length])
    out[length - 1] = running / length
    for i in range(length, len(values)):
        running += values[i] - values[i - length]
        out[i] = running / length
    return out


def ema(values: Sequence[float], length: int) -> Series:
    """Exponential moving average, seeded with an SMA (TradingView behaviour)."""
    if length <= 0:
        raise ValueError("length must be positive")
    _require_no_none(values, "ema input")
    out: Series = [None] * len(values)
    if len(values) < length:
        return out
    prev = sum(values[:length]) / length
    out[length - 1] = prev
    k = 2.0 / (length + 1.0)
    for i in range(length, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def rma(values: Sequence[float], length: int) -> Series:
    """Wilder's smoothing (a.k.a. RMA/SMMA), used by ATR, RSI and ADX.

    Distinct from EMA: the smoothing constant is 1/n, not 2/(n+1).
    """
    if length <= 0:
        raise ValueError("length must be positive")
    _require_no_none(values, "rma input")
    out: Series = [None] * len(values)
    if len(values) < length:
        return out
    prev = sum(values[:length]) / length
    out[length - 1] = prev
    for i in range(length, len(values)):
        prev = (prev * (length - 1) + values[i]) / length
        out[i] = prev
    return out


def true_range(bars: Sequence[Bar]) -> Series:
    """True range. Index 0 is the bare high-low, having no prior close."""
    out: Series = [None] * len(bars)
    if not bars:
        return out
    out[0] = bars[0].range
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        out[i] = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - prev_close),
            abs(bars[i].low - prev_close),
        )
    return out


def atr(bars: Sequence[Bar], length: int = 14) -> Series:
    """Average true range (Wilder)."""
    return _apply_to_tail(true_range(bars), rma, length)


def rsi(values: Sequence[float], length: int = 14) -> Series:
    """Relative strength index (Wilder)."""
    _require_no_none(values, "rsi input")
    out: Series = [None] * len(values)
    if len(values) <= length:
        return out
    gains = [0.0] * len(values)
    losses = [0.0] * len(values)
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)
    # Drop index 0: it has no change and would bias the first average.
    avg_gain = rma(gains[1:], length)
    avg_loss = rma(losses[1:], length)
    for i, (g, losses_i) in enumerate(zip(avg_gain, avg_loss)):
        if g is None or losses_i is None:
            continue
        if losses_i == 0:
            out[i + 1] = 100.0
        else:
            rs = g / losses_i
            out[i + 1] = 100.0 - (100.0 / (1.0 + rs))
    return out


@dataclass
class ADXResult:
    """Directional movement system output."""

    plus_di: Series
    minus_di: Series
    adx: Series


def adx(bars: Sequence[Bar], length: int = 14) -> ADXResult:
    """Wilder's ADX and directional indicators.

    ADX measures trend *strength* without direction; DI+ / DI- carry direction.
    The playbook uses ADX to decide whether to trade continuation or fade.
    """
    n = len(bars)
    empty: Series = [None] * n
    if n < 2:
        return ADXResult(list(empty), list(empty), list(empty))

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    trs: list[float] = []
    for i in range(1, n):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        prev_close = bars[i - 1].close
        trs.append(
            max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - prev_close),
                abs(bars[i].low - prev_close),
            )
        )

    sm_plus = rma(plus_dm, length)
    sm_minus = rma(minus_dm, length)
    sm_tr = rma(trs, length)

    plus_di: Series = [None] * n
    minus_di: Series = [None] * n
    dx_dense: list[float] = []
    dx_offset: int | None = None

    for i in range(len(sm_tr)):
        tr_v, p_v, m_v = sm_tr[i], sm_plus[i], sm_minus[i]
        if tr_v is None or p_v is None or m_v is None or tr_v == 0:
            continue
        p = 100.0 * p_v / tr_v
        m = 100.0 * m_v / tr_v
        plus_di[i + 1] = p
        minus_di[i + 1] = m
        total = p + m
        dx = 0.0 if total == 0 else 100.0 * abs(p - m) / total
        if dx_offset is None:
            dx_offset = i + 1
        dx_dense.append(dx)

    adx_series: Series = [None] * n
    if dx_offset is not None and len(dx_dense) >= length:
        smoothed = rma(dx_dense, length)
        for i, v in enumerate(smoothed):
            if v is not None:
                adx_series[dx_offset + i] = v

    return ADXResult(plus_di, minus_di, adx_series)


@dataclass
class VWAPResult:
    """Session-anchored VWAP with standard-deviation bands."""

    vwap: Series
    stdev: Series
    bands: dict[float, tuple[Series, Series]] = field(default_factory=dict)

    def upper(self, mult: float) -> Series:
        return self.bands[mult][0]

    def lower(self, mult: float) -> Series:
        return self.bands[mult][1]


def session_vwap(
    bars: Sequence[Bar], band_mults: Sequence[float] = (1.0, 2.0, 3.0)
) -> VWAPResult:
    """Volume-weighted average price, reset each trading day, with bands.

    Bands use the volume-weighted standard deviation of typical price about the
    running VWAP, which is what TradingView's VWAP indicator plots. On zero-
    volume bars (thin overnight data, or feeds that omit volume) the calculation
    degrades to an unweighted running average rather than dividing by zero.
    """
    n = len(bars)
    vwap: Series = [None] * n
    stdev: Series = [None] * n
    bands: dict[float, tuple[Series, Series]] = {
        m: ([None] * n, [None] * n) for m in band_mults
    }

    current_day: date | None = None
    sum_pv = sum_v = sum_p2v = 0.0
    count = 0
    sum_p = sum_p2 = 0.0

    for i, bar in enumerate(bars):
        day = trading_date(bar.ts)
        if day != current_day:
            current_day = day
            sum_pv = sum_v = sum_p2v = 0.0
            count = 0
            sum_p = sum_p2 = 0.0

        price = bar.hlc3
        vol = bar.volume
        sum_pv += price * vol
        sum_v += vol
        sum_p2v += price * price * vol
        count += 1
        sum_p += price
        sum_p2 += price * price

        if sum_v > 0:
            mean = sum_pv / sum_v
            variance = sum_p2v / sum_v - mean * mean
        else:
            mean = sum_p / count
            variance = sum_p2 / count - mean * mean

        sd = math.sqrt(max(variance, 0.0))
        vwap[i] = mean
        stdev[i] = sd
        for mult in band_mults:
            bands[mult][0][i] = mean + mult * sd
            bands[mult][1][i] = mean - mult * sd

    return VWAPResult(vwap=vwap, stdev=stdev, bands=bands)


@dataclass
class OpeningRange:
    """The high/low established in a fixed window after the cash open."""

    high: Series
    low: Series
    complete: list[bool]

    def width(self, i: int) -> float | None:
        hi, lo = self.high[i], self.low[i]
        if hi is None or lo is None:
            return None
        return hi - lo


def opening_range(
    bars: Sequence[Bar], start: time, end: time
) -> OpeningRange:
    """Track the opening range per trading day.

    While inside the window the values update live and `complete` is False; a
    breakout is only actionable once `complete` is True. This distinction is the
    difference between trading the opening range and trading a bar that happens
    to be making it.
    """
    n = len(bars)
    highs: Series = [None] * n
    lows: Series = [None] * n
    complete = [False] * n

    current_day: date | None = None
    day_high: float | None = None
    day_low: float | None = None
    done = False

    for i, bar in enumerate(bars):
        day = trading_date(bar.ts)
        if day != current_day:
            current_day = day
            day_high = day_low = None
            done = False

        t = bar.et.time()
        if start <= t < end:
            day_high = bar.high if day_high is None else max(day_high, bar.high)
            day_low = bar.low if day_low is None else min(day_low, bar.low)
        elif t >= end and day_high is not None:
            done = True

        highs[i] = day_high
        lows[i] = day_low
        complete[i] = done

    return OpeningRange(high=highs, low=lows, complete=complete)


@dataclass
class Pivots:
    """Most recently *confirmed* swing points, as known at each bar."""

    last_high: Series
    last_low: Series
    last_high_index: list[int | None]
    last_low_index: list[int | None]


def swing_pivots(bars: Sequence[Bar], left: int = 3, right: int = 3) -> Pivots:
    """Fractal swing highs and lows, published only once confirmed.

    A pivot high at bar `p` requires `left` lower highs before it and `right`
    lower highs after it, so it is not knowable until bar `p + right`. The
    returned series reflect that delay, which makes them safe to use as
    structural stop levels in a live decision.
    """
    n = len(bars)
    last_high: Series = [None] * n
    last_low: Series = [None] * n
    last_high_index: list[int | None] = [None] * n
    last_low_index: list[int | None] = [None] * n

    pending_high: float | None = None
    pending_high_idx: int | None = None
    pending_low: float | None = None
    pending_low_idx: int | None = None

    for i in range(n):
        # At bar i we can finally judge the candidate that sits `right` bars back.
        p = i - right
        if p - left >= 0:
            candidate = bars[p]
            is_high = all(
                bars[j].high <= candidate.high
                for j in range(p - left, p + right + 1)
                if j != p
            )
            is_low = all(
                bars[j].low >= candidate.low
                for j in range(p - left, p + right + 1)
                if j != p
            )
            if is_high:
                pending_high = candidate.high
                pending_high_idx = p
            if is_low:
                pending_low = candidate.low
                pending_low_idx = p

        last_high[i] = pending_high
        last_low[i] = pending_low
        last_high_index[i] = pending_high_idx
        last_low_index[i] = pending_low_idx

    return Pivots(last_high, last_low, last_high_index, last_low_index)


def rolling_extreme(values: Sequence[float], length: int, mode: str = "max") -> Series:
    """Rolling max or min over a trailing window of `length` bars."""
    _require_no_none(values, "rolling_extreme input")
    if mode not in ("max", "min"):
        raise ValueError("mode must be 'max' or 'min'")
    fn = max if mode == "max" else min
    out: Series = [None] * len(values)
    for i in range(length - 1, len(values)):
        out[i] = fn(values[i - length + 1 : i + 1])
    return out


def percentile_rank(values: Sequence[float | None], window: int) -> Series:
    """Fraction of the trailing `window` values at or below the current one.

    Used to ask "is current volatility high or low *for this market*", which is
    far more robust across regimes than any absolute ATR threshold.
    """
    out: Series = [None] * len(values)
    for i, current in enumerate(values):
        if current is None or i < window:
            continue
        history = [v for v in values[i - window : i] if v is not None]
        if not history:
            continue
        below = sum(1 for v in history if v <= current)
        out[i] = below / len(history)
    return out


def slope_per_bar(values: Sequence[float | None], length: int) -> Series:
    """Least-squares slope over a trailing window, in price units per bar.

    Preferred over a simple end-to-end difference because a single spike at
    either endpoint cannot dominate the result.
    """
    out: Series = [None] * len(values)
    if length < 2:
        raise ValueError("length must be at least 2")
    xs = list(range(length))
    x_mean = sum(xs) / length
    x_var = sum((x - x_mean) ** 2 for x in xs)
    for i in range(length - 1, len(values)):
        window = values[i - length + 1 : i + 1]
        if any(v is None for v in window):
            continue
        ys = [float(v) for v in window]
        y_mean = sum(ys) / length
        cov = sum((xs[j] - x_mean) * (ys[j] - y_mean) for j in range(length))
        out[i] = cov / x_var if x_var else 0.0
    return out


@dataclass
class PriorDayLevels:
    """Previous trading day's high, low and close, as known at each bar."""

    high: Series
    low: Series
    close: Series


def prior_day_levels(bars: Sequence[Bar]) -> PriorDayLevels:
    """Prior-day high/low/close, the reference levels NQ reacts to most.

    Values only change at a day boundary, so there is no lookahead: during a
    given day these describe a day that has already finished.
    """
    n = len(bars)
    highs: Series = [None] * n
    lows: Series = [None] * n
    closes: Series = [None] * n

    current_day: date | None = None
    running_high = running_low = running_close = None
    prev = (None, None, None)

    for i, bar in enumerate(bars):
        day = trading_date(bar.ts)
        if day != current_day:
            if current_day is not None:
                prev = (running_high, running_low, running_close)
            current_day = day
            running_high, running_low = bar.high, bar.low
        else:
            running_high = max(running_high, bar.high)
            running_low = min(running_low, bar.low)
        running_close = bar.close
        highs[i], lows[i], closes[i] = prev

    return PriorDayLevels(highs, lows, closes)


def resample(bars: Sequence[Bar], minutes: int) -> list[Bar]:
    """Aggregate bars into a higher timeframe, on wall-clock boundaries.

    Only fully-closed higher-timeframe bars are returned. A partial trailing
    bucket is dropped, so the last element is always a complete bar — the caller
    can trust it the same way a chart would.
    """
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    if not bars:
        return []

    out: list[Bar] = []
    bucket: list[Bar] = []
    bucket_key: tuple[date, int] | None = None

    def flush() -> None:
        if not bucket:
            return
        out.append(
            Bar(
                ts=bucket[0].ts,
                open=bucket[0].open,
                high=max(b.high for b in bucket),
                low=min(b.low for b in bucket),
                close=bucket[-1].close,
                volume=sum(b.volume for b in bucket),
            )
        )

    for bar in bars:
        et = bar.et
        slot = (et.hour * 60 + et.minute) // minutes
        key = (et.date(), slot)
        if bucket_key is None:
            bucket_key = key
        elif key != bucket_key:
            flush()
            bucket = []
            bucket_key = key
        bucket.append(bar)

    # The final bucket is intentionally discarded unless it is provably full.
    if bucket and bucket_key is not None:
        span = (bucket[-1].ts - bucket[0].ts).total_seconds() / 60.0
        base = (bucket[1].ts - bucket[0].ts).total_seconds() / 60.0 if len(bucket) > 1 else None
        if base and span + base >= minutes:
            flush()

    return out
