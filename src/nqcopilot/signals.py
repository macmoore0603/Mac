"""Trade setup detection.

Each detector recognises one specific, named pattern and is gated to the regime
where that pattern has a rationale. A breakout detector that fires in chop, or a
fade that fires in a strong trend, converts a reasonable idea into a losing one —
so the gating here matters more than the trigger logic.

Every setup returns a complete trade plan (entry, stop, two targets) or nothing
at all. There is deliberately no "weak signal" output: a setup that cannot
specify where it is wrong is not a setup.

None of this predicts the future. These are conditional patterns with modest
historical edge, and a well-formed setup with a high score still loses often.
The score orders candidates; it is not a probability of profit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .bars import Session
from .contracts import ContractSpec
from .market import MarketContext, Regime, at


@dataclass(frozen=True)
class Setup:
    """A complete, executable trade plan."""

    name: str
    direction: int              # +1 long, -1 short
    entry: float
    stop: float
    target1: float
    target2: float
    score: float                # 0-100, for ranking candidates only
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_long(self) -> bool:
        return self.direction > 0

    @property
    def side(self) -> str:
        return "LONG" if self.is_long else "SHORT"

    @property
    def stop_points(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def target1_points(self) -> float:
        return abs(self.target1 - self.entry)

    @property
    def target2_points(self) -> float:
        return abs(self.target2 - self.entry)

    @property
    def r_multiple_t1(self) -> float:
        return self.target1_points / self.stop_points if self.stop_points else 0.0

    @property
    def r_multiple_t2(self) -> float:
        return self.target2_points / self.stop_points if self.stop_points else 0.0

    def net_r_t1(self, spec: ContractSpec, quantity: int = 1) -> float:
        """Reward:risk at target 1 *after* commission.

        Gross R:R flatters tight-stop scalps. On MNQ a 10-point stop risks $20
        plus $1.34 of round-turn cost — nearly 7% of the risk before the trade
        starts. Sizing and go/no-go decisions use this, not gross R.
        """
        if quantity <= 0:
            quantity = 1
        cost = spec.commission(quantity)
        gross_win = spec.points_to_dollars(self.target1_points, quantity)
        gross_loss = spec.points_to_dollars(self.stop_points, quantity)
        net_win = gross_win - cost
        net_loss = gross_loss + cost
        return net_win / net_loss if net_loss > 0 else 0.0


def _build_setup(
    name: str,
    direction: int,
    entry: float,
    stop: float,
    target1: float,
    target2: float,
    score: float,
    spec: ContractSpec,
    reasons: list[str],
    warnings: list[str] | None = None,
) -> Setup | None:
    """Assemble a setup, snapping to ticks and rejecting invalid geometry.

    Returns None if the plan is nonsensical (stop on the wrong side, zero-width
    stop, target behind entry). Silently emitting a broken plan would be worse
    than emitting nothing.
    """
    is_long = direction > 0
    entry = spec.round_to_tick(entry)
    stop = spec.round_stop(stop, is_long)
    target1 = spec.round_target(target1, is_long)
    target2 = spec.round_target(target2, is_long)

    if is_long:
        if not (stop < entry < target1 <= target2):
            return None
    else:
        if not (stop > entry > target1 >= target2):
            return None
    if abs(entry - stop) < spec.tick_size:
        return None

    return Setup(
        name=name,
        direction=direction,
        entry=entry,
        stop=stop,
        target1=target1,
        target2=target2,
        score=max(0.0, min(100.0, score)),
        reasons=reasons,
        warnings=warnings or [],
    )


def _confluence(ctx: MarketContext, direction: int) -> tuple[float, list[str], list[str]]:
    """Score how well the broader context supports a trade in `direction`.

    Returns (points, reasons, warnings). Contributions are capped so no single
    factor can carry a setup on its own.
    """
    points = 0.0
    reasons: list[str] = []
    warnings: list[str] = []
    is_long = direction > 0

    if ctx.htf_bias == direction:
        points += 15
        reasons.append(f"{ctx.config.htf_minutes}m trend agrees")
    elif ctx.htf_bias == -direction:
        points -= 12
        warnings.append(f"Fighting the {ctx.config.htf_minutes}m trend")

    stack = ctx.ema_stack()
    if stack == direction:
        points += 10
        reasons.append("EMAs stacked in trade direction")
    elif stack == -direction:
        points -= 8
        warnings.append("EMAs stacked against the trade")

    vwap = ctx.current_vwap
    if vwap is not None:
        on_side = (ctx.price > vwap) if is_long else (ctx.price < vwap)
        if on_side:
            points += 10
            reasons.append("On the favourable side of session VWAP")
        else:
            points -= 6
            warnings.append("On the wrong side of session VWAP")

    adx_v = ctx.current_adx
    if adx_v is not None:
        points += max(0.0, min(15.0, (adx_v - 15.0) * 0.9))
        if adx_v >= 25:
            reasons.append(f"ADX {adx_v:.0f}: directional")
        elif adx_v < 18:
            warnings.append(f"ADX {adx_v:.0f}: weak directional conviction")

    rvol = ctx.relative_volume()
    if rvol is not None:
        if rvol >= 1.3:
            points += 10
            reasons.append(f"Volume {rvol:.1f}x average")
        elif rvol < 0.7:
            points -= 8
            warnings.append(f"Volume only {rvol:.1f}x average")

    session = ctx.session
    if session in (Session.OPENING_DRIVE, Session.MORNING_TREND):
        points += 10
        reasons.append(f"{session.value.replace('_', ' ').title()}: prime hours")
    elif session is Session.LUNCH:
        points -= 15
        warnings.append("Lunch session: poor follow-through")
    elif session is Session.POWER_HOUR:
        points += 5
        reasons.append("Power hour")

    rsi_v = ctx.current_rsi
    if rsi_v is not None:
        if is_long and rsi_v > 78:
            points -= 10
            warnings.append(f"RSI {rsi_v:.0f}: extended, poor entry location")
        elif not is_long and rsi_v < 22:
            points -= 10
            warnings.append(f"RSI {rsi_v:.0f}: extended, poor entry location")

    return points, reasons, warnings


def _stop_from_structure(ctx: MarketContext, is_long: bool) -> float:
    """Structural stop padded by a fraction of ATR to sit outside the noise."""
    atr_v = ctx.atr_points()
    level = ctx.structural_stop(is_long)
    pad = 0.25 * atr_v
    if level is None:
        return ctx.price - pad * 4 if is_long else ctx.price + pad * 4
    return level - pad if is_long else level + pad


def detect_orb_breakout(ctx: MarketContext) -> Setup | None:
    """Opening-range breakout: the day's first accepted move out of the open range.

    Requires the range to be *complete* (the window has closed) and the breakout
    bar to be the first close outside it. A range that is unusually wide or
    narrow relative to ATR is rejected: too wide leaves no room before the next
    reference level, too narrow means the level carries no information.
    """
    i = ctx.index
    if not ctx.orb.complete[i]:
        return None
    hi, lo = at(ctx.orb.high, i), at(ctx.orb.low, i)
    if hi is None or lo is None or i < 1:
        return None

    # Breakouts decay quickly after the first couple of hours.
    if not 30 <= ctx.minutes_since_open <= 180:
        return None

    atr_v = ctx.atr_points()
    width = hi - lo
    if width > 3.0 * atr_v or width < 0.4 * atr_v:
        return None

    prev = ctx.bars[i - 1]
    bar = ctx.bar
    regime = ctx.regime()
    if regime in (Regime.CHOP, Regime.QUIET):
        return None

    if bar.close > hi and prev.close <= hi:
        direction = 1
        level = hi
    elif bar.close < lo and prev.close >= lo:
        direction = -1
        level = lo
    else:
        return None

    is_long = direction > 0
    # A breakout bar that closes back inside its own range is a failed push.
    if is_long and bar.close_position() < 0.55:
        return None
    if not is_long and bar.close_position() > 0.45:
        return None

    points, reasons, warnings = _confluence(ctx, direction)
    points += 25
    reasons.insert(0, f"First close {'above' if is_long else 'below'} the {width:.0f}pt opening range")

    entry = bar.close
    structural = _stop_from_structure(ctx, is_long)
    # Behind the broken level is the natural invalidation point.
    level_stop = level - 0.3 * atr_v if is_long else level + 0.3 * atr_v
    stop = min(structural, level_stop) if is_long else max(structural, level_stop)

    risk = abs(entry - stop)
    t1 = entry + risk * 1.5 * direction
    t2 = entry + max(risk * 2.5, width) * direction

    return _build_setup(
        "Opening Range Breakout", direction, entry, stop, t1, t2, points,
        ctx.spec, reasons, warnings,
    )


def detect_trend_pullback(ctx: MarketContext) -> Setup | None:
    """Pullback continuation: buy a dip inside an established uptrend (or mirror).

    Structurally the highest-expectancy intraday pattern on NQ, because it enters
    with the dominant flow at a location where the stop is close. Requires an
    actual pullback to a reference (EMA or VWAP) followed by a resumption bar.
    """
    i = ctx.index
    regime = ctx.regime()
    if regime is Regime.TREND_UP:
        direction = 1
    elif regime is Regime.TREND_DOWN:
        direction = -1
    else:
        return None

    is_long = direction > 0
    atr_v = ctx.atr_points()
    ema_mid = at(ctx.ema_mid, i)
    vwap = ctx.current_vwap
    if ema_mid is None:
        return None

    refs = [r for r in (ema_mid, vwap) if r is not None]
    lookback = ctx.bars[max(0, i - 3) : i + 1]
    touched = False
    for b in lookback:
        for ref in refs:
            if is_long and b.low <= ref + 0.35 * atr_v:
                touched = True
            if not is_long and b.high >= ref - 0.35 * atr_v:
                touched = True
    if not touched:
        return None

    bar = ctx.bar
    # The resumption bar must actually resume: correct direction, closing strong.
    if is_long and not (bar.is_up and bar.close_position() >= 0.6 and bar.close > ema_mid):
        return None
    if not is_long and not (
        not bar.is_up and bar.close_position() <= 0.4 and bar.close < ema_mid
    ):
        return None

    points, reasons, warnings = _confluence(ctx, direction)
    points += 22
    reasons.insert(0, f"Pullback to the {ctx.config.ema_mid} EMA held, momentum resuming")

    entry = bar.close
    stop = _stop_from_structure(ctx, is_long)
    risk = abs(entry - stop)
    t1 = entry + risk * 1.5 * direction
    t2 = entry + risk * 3.0 * direction

    return _build_setup(
        "Trend Pullback", direction, entry, stop, t1, t2, points,
        ctx.spec, reasons, warnings,
    )


def detect_vwap_fade(ctx: MarketContext) -> Setup | None:
    """Mean reversion to VWAP from a stretched band, in range conditions only.

    Gated hard to CHOP/QUIET regimes. Fading a 2-sigma extension during a genuine
    trend day is the fastest way to lose an account: in a trend, 2 sigma is not
    an extreme, it is the trend.
    """
    i = ctx.index
    regime = ctx.regime()
    if regime not in (Regime.CHOP, Regime.QUIET):
        return None

    sigma = ctx.vwap_distance_sigma()
    vwap = ctx.current_vwap
    if sigma is None or vwap is None or abs(sigma) < 2.0:
        return None

    bar = ctx.bar
    rsi_v = ctx.current_rsi
    direction = -1 if sigma > 0 else 1
    is_long = direction > 0

    # Require visible rejection, not merely an extended price.
    if not is_long:
        rejected = bar.upper_wick > bar.body * 0.8 or not bar.is_up
        overbought = rsi_v is None or rsi_v > 62
        if not (rejected and overbought):
            return None
    else:
        rejected = bar.lower_wick > bar.body * 0.8 or bar.is_up
        oversold = rsi_v is None or rsi_v < 38
        if not (rejected and oversold):
            return None

    atr_v = ctx.atr_points()
    points, reasons, warnings = _confluence(ctx, direction)
    points += 12
    reasons.insert(0, f"Price {abs(sigma):.1f}σ from VWAP in a range regime, showing rejection")
    warnings.append("Counter-trend by construction: take target 1, do not hold for a runner")

    entry = bar.close
    extreme = bar.high if not is_long else bar.low
    stop = extreme + 0.5 * atr_v if not is_long else extreme - 0.5 * atr_v
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    # VWAP is the objective; a second target beyond it is wishful in a range.
    t1 = entry + min(abs(entry - vwap) * 0.6, risk * 1.5) * direction
    t2 = vwap

    return _build_setup(
        "VWAP Fade", direction, entry, stop, t1, t2, points,
        ctx.spec, reasons, warnings,
    )


def detect_failed_breakout(ctx: MarketContext) -> Setup | None:
    """Reversal after price rejects a prior-day extreme.

    Prior-day high/low are where resting liquidity sits. A push through that
    fails to hold traps the breakout crowd, and their stops fuel the reversal.
    """
    i = ctx.index
    if i < 3:
        return None
    pdh = at(ctx.prior_day.high, i)
    pdl = at(ctx.prior_day.low, i)
    bar = ctx.bar
    atr_v = ctx.atr_points()
    window = ctx.bars[max(0, i - 3) : i + 1]

    direction = 0
    level = 0.0
    if pdh is not None and any(b.high > pdh for b in window) and bar.close < pdh:
        direction = -1
        level = max(b.high for b in window)
    elif pdl is not None and any(b.low < pdl for b in window) and bar.close > pdl:
        direction = 1
        level = min(b.low for b in window)
    if direction == 0:
        return None

    is_long = direction > 0
    # The rejection must be genuine, not a marginal poke through.
    reference = pdl if is_long else pdh
    if abs(level - reference) < 0.15 * atr_v:
        return None
    if is_long and not bar.is_up:
        return None
    if not is_long and bar.is_up:
        return None

    points, reasons, warnings = _confluence(ctx, direction)
    points += 18
    reasons.insert(
        0,
        f"Failed break of prior-day {'high' if not is_long else 'low'} "
        f"({reference:,.2f}); breakout traders trapped",
    )

    entry = bar.close
    stop = level + 0.3 * atr_v if not is_long else level - 0.3 * atr_v
    risk = abs(entry - stop)
    vwap = ctx.current_vwap
    t1 = entry + risk * 1.5 * direction
    t2 = vwap if vwap is not None and (vwap - entry) * direction > risk * 2 else entry + risk * 2.5 * direction

    return _build_setup(
        "Failed Breakout Reversal", direction, entry, stop, t1, t2, points,
        ctx.spec, reasons, warnings,
    )


def detect_vwap_reclaim(ctx: MarketContext) -> Setup | None:
    """Session VWAP reclaimed after time spent on the other side.

    A clean shift in the session's balance point, and one of the more reliable
    signals that the morning's direction has changed.
    """
    i = ctx.index
    if i < 6:
        return None
    vwap_now = ctx.current_vwap
    if vwap_now is None:
        return None
    if ctx.regime() is Regime.EXPANSION:
        return None

    prior = ctx.bars[max(0, i - 6) : i]
    prior_vwaps = [at(ctx.vwap.vwap, j) for j in range(max(0, i - 6), i)]
    if any(v is None for v in prior_vwaps):
        return None

    below = sum(1 for b, v in zip(prior, prior_vwaps) if b.close < v)
    above = sum(1 for b, v in zip(prior, prior_vwaps) if b.close > v)
    bar = ctx.bar

    if below >= 4 and bar.close > vwap_now and bar.is_up:
        direction = 1
    elif above >= 4 and bar.close < vwap_now and not bar.is_up:
        direction = -1
    else:
        return None

    is_long = direction > 0
    atr_v = ctx.atr_points()
    points, reasons, warnings = _confluence(ctx, direction)
    points += 15
    reasons.insert(0, "Session VWAP reclaimed after sustained trade on the other side")

    entry = bar.close
    structural = _stop_from_structure(ctx, is_long)
    vwap_stop = vwap_now - 0.4 * atr_v if is_long else vwap_now + 0.4 * atr_v
    stop = min(structural, vwap_stop) if is_long else max(structural, vwap_stop)
    risk = abs(entry - stop)
    t1 = entry + risk * 1.5 * direction
    t2 = entry + risk * 2.5 * direction

    return _build_setup(
        "VWAP Reclaim", direction, entry, stop, t1, t2, points,
        ctx.spec, reasons, warnings,
    )


DETECTORS = (
    detect_trend_pullback,
    detect_orb_breakout,
    detect_vwap_reclaim,
    detect_failed_breakout,
    detect_vwap_fade,
)


def detect_all(ctx: MarketContext) -> list[Setup]:
    """Run every detector and return candidates ranked by score."""
    found: list[Setup] = []
    for detector in DETECTORS:
        setup = detector(ctx)
        if setup is not None:
            found.append(setup)
    found.sort(key=lambda s: s.score, reverse=True)
    return found
