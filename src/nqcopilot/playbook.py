"""The decision layer: turn market context and account state into one instruction.

The contract of this module is simple and strict: **risk has veto power over
signal**. A perfect setup during a hard blocker produces STAND_DOWN. There is no
code path that lets a high score override a rule gate, and the tests assert it.

That asymmetry is the whole design. Setup quality is a probabilistic opinion and
will be wrong often. Rule compliance is arithmetic and can be exactly right.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .apex import (
    AccountState,
    Blocker,
    ConsistencyStatus,
    RiskEngine,
    Severity,
    Sizing,
    TrailingMode,
    consistency_status,
)
from .bars import Bar
from .contracts import ContractSpec
from .market import IndicatorConfig, MarketContext, Regime
from .signals import Setup, detect_all


class Action(Enum):
    """The instruction. Exactly one is issued per evaluation."""

    GO_LONG = "GO LONG"
    GO_SHORT = "GO SHORT"
    WAIT = "WAIT"              # conditions allowed, no qualifying setup
    STAND_DOWN = "STAND DOWN"  # a rule forbids trading
    FLATTEN = "FLATTEN"        # close what you have, now

    @property
    def is_entry(self) -> bool:
        return self in (Action.GO_LONG, Action.GO_SHORT)


@dataclass(frozen=True)
class PlaybookConfig:
    """Thresholds a setup must clear before it becomes an instruction."""

    min_score: float = 55.0
    min_net_r: float = 1.2
    scale_out_at_t1: bool = True
    move_to_breakeven_at_r: float = 1.0


@dataclass
class Directive:
    """The complete answer to 'what do I do right now'."""

    timestamp: datetime
    action: Action
    headline: str
    regime: Regime
    price: float
    setup: Setup | None = None
    sizing: Sizing | None = None
    blockers: list[Blocker] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    consistency: ConsistencyStatus | None = None
    candidates: list[Setup] = field(default_factory=list)

    @property
    def hard_blockers(self) -> list[Blocker]:
        return [b for b in self.blockers if b.is_hard]

    @property
    def is_actionable(self) -> bool:
        return self.action.is_entry and self.sizing is not None and self.sizing.quantity > 0


def evaluate(
    bars: list[Bar],
    spec: ContractSpec,
    state: AccountState,
    *,
    risk: RiskEngine | None = None,
    config: PlaybookConfig | None = None,
    indicators: IndicatorConfig | None = None,
    in_position: bool = False,
    now: datetime | None = None,
) -> Directive:
    """Produce the single instruction for the current bar.

    Args:
        bars: Chronological bars, most recent last. The last bar must be closed.
        spec: Contract being traded.
        state: Current account state, marked to market.
        risk: Risk engine; one is constructed from `state` if omitted.
        config: Setup-quality thresholds.
        indicators: Indicator parameters.
        in_position: Whether a position is currently open. Changes the question
            from "should I enter" to "should I stay".
        now: Decision time; defaults to the last bar's timestamp.
    """
    ctx = MarketContext.build(bars, spec, indicators)
    return evaluate_context(
        ctx, state, risk=risk, config=config, in_position=in_position, now=now
    )


def evaluate_context(
    ctx: MarketContext,
    state: AccountState,
    *,
    risk: RiskEngine | None = None,
    config: PlaybookConfig | None = None,
    in_position: bool = False,
    now: datetime | None = None,
) -> Directive:
    """Produce the instruction for a prebuilt context at its evaluation index.

    Separated from `evaluate` so a replay can build the indicator stack once and
    step the index, rather than recomputing every series on every bar.
    """
    cfg = config or PlaybookConfig()
    engine = risk or RiskEngine(state)
    spec = ctx.spec
    decision_time = now or ctx.bar.ts
    regime = ctx.regime()

    notes = _account_notes(state)
    budget_note = _risk_budget_note(state, engine)
    if budget_note:
        notes.append(budget_note)
    consistency = consistency_status(state)

    # 1. Warmup. Without a full indicator stack, every downstream read is noise.
    if not ctx.is_ready:
        deficit = ctx.warmup_deficit()
        return Directive(
            timestamp=decision_time,
            action=Action.WAIT,
            headline=f"Warming up — need ~{deficit} more bars before any read is valid",
            regime=regime,
            price=ctx.price,
            notes=notes,
            consistency=consistency,
        )

    # 2. Rule gates. These are evaluated before any setup is even considered, so
    #    a hard blocker cannot be argued with by a good-looking chart.
    blockers = engine.check(decision_time, in_position=in_position)
    hard = [b for b in blockers if b.is_hard]

    if hard:
        if in_position:
            return Directive(
                timestamp=decision_time,
                action=Action.FLATTEN,
                headline=f"Close the position: {hard[0].message}",
                regime=regime,
                price=ctx.price,
                blockers=blockers,
                notes=notes,
                consistency=consistency,
            )
        return Directive(
            timestamp=decision_time,
            action=Action.STAND_DOWN,
            headline=hard[0].message,
            regime=regime,
            price=ctx.price,
            blockers=blockers,
            notes=notes,
            consistency=consistency,
        )

    soft_warnings = [b.message for b in blockers if not b.is_hard]

    # 3. Managing an existing position takes priority over finding a new one.
    if in_position:
        return _manage_open_position(ctx, state, decision_time, regime, blockers, notes, consistency)

    # 4. Setup detection.
    candidates = detect_all(ctx)
    if not candidates:
        return Directive(
            timestamp=decision_time,
            action=Action.WAIT,
            headline=f"No qualifying setup — {_regime_phrase(regime)}",
            regime=regime,
            price=ctx.price,
            blockers=blockers,
            warnings=soft_warnings,
            notes=notes,
            consistency=consistency,
        )

    best = candidates[0]

    if best.score < cfg.min_score:
        return Directive(
            timestamp=decision_time,
            action=Action.WAIT,
            headline=(
                f"{best.name} present but scores {best.score:.0f}/100, "
                f"below the {cfg.min_score:.0f} threshold"
            ),
            regime=regime,
            price=ctx.price,
            blockers=blockers,
            reasons=best.reasons,
            warnings=soft_warnings + best.warnings,
            notes=notes,
            consistency=consistency,
            candidates=candidates,
        )

    # 5. Stop sanity, then sizing.
    stop_blocker = engine.validate_stop(best.stop_points)
    if stop_blocker is not None:
        return Directive(
            timestamp=decision_time,
            action=Action.STAND_DOWN,
            headline=stop_blocker.message,
            regime=regime,
            price=ctx.price,
            blockers=blockers + [stop_blocker],
            warnings=soft_warnings,
            notes=notes,
            consistency=consistency,
            candidates=candidates,
        )

    sizing = engine.size_position(spec, best.stop_points)

    if sizing.quantity < 1:
        return Directive(
            timestamp=decision_time,
            action=Action.STAND_DOWN,
            headline=(
                f"{best.name} qualifies, but a {best.stop_points:.2f}pt stop costs "
                f"${sizing.risk_per_contract:,.2f} per contract and your budget is "
                f"${sizing.risk_budget:,.2f} ({_factor_phrase(sizing.limiting_factor)}). "
                "Size would round to zero — skip it."
            ),
            regime=regime,
            price=ctx.price,
            setup=best,
            sizing=sizing,
            blockers=blockers,
            warnings=soft_warnings,
            notes=notes,
            consistency=consistency,
            candidates=candidates,
        )

    net_r = best.net_r_t1(spec, sizing.quantity)
    if net_r < cfg.min_net_r:
        return Directive(
            timestamp=decision_time,
            action=Action.WAIT,
            headline=(
                f"{best.name} nets only {net_r:.2f}R to target 1 after commission "
                f"(minimum {cfg.min_net_r:.2f}R) — not worth the risk"
            ),
            regime=regime,
            price=ctx.price,
            setup=best,
            sizing=sizing,
            blockers=blockers,
            reasons=best.reasons,
            warnings=soft_warnings + best.warnings,
            notes=notes,
            consistency=consistency,
            candidates=candidates,
        )

    # 6. Cleared. Build the execution instruction.
    action = Action.GO_LONG if best.is_long else Action.GO_SHORT
    headline = (
        f"{action.value} {sizing.quantity} {spec.symbol} @ {best.entry:,.2f} — "
        f"{best.name} ({best.score:.0f}/100)"
    )

    trade_notes = _execution_notes(ctx, best, sizing, spec, state, cfg, net_r)

    return Directive(
        timestamp=decision_time,
        action=action,
        headline=headline,
        regime=regime,
        price=ctx.price,
        setup=best,
        sizing=sizing,
        blockers=blockers,
        reasons=best.reasons,
        warnings=soft_warnings + best.warnings,
        notes=notes + trade_notes,
        consistency=consistency,
        candidates=candidates,
    )


def _manage_open_position(
    ctx: MarketContext,
    state: AccountState,
    decision_time: datetime,
    regime: Regime,
    blockers: list[Blocker],
    notes: list[str],
    consistency: ConsistencyStatus,
) -> Directive:
    """Guidance while a position is open and no hard rule has tripped."""
    warnings = [b.message for b in blockers if not b.is_hard]
    guidance: list[str] = []

    if state.open_pnl > 0 and state.profile.trailing_mode is TrailingMode.INTRADAY:
        guidance.append(
            f"Open profit of ${state.open_pnl:,.2f} has already ratcheted your "
            f"threshold. Giving it back costs that room permanently — trail your stop."
        )
    elif state.open_pnl < 0:
        guidance.append(
            f"Position is ${abs(state.open_pnl):,.2f} offside. Your stop is the plan; "
            "do not widen it."
        )

    if regime is Regime.EXPANSION:
        warnings.append("Volatility has expanded since entry — consider reducing size.")

    return Directive(
        timestamp=decision_time,
        action=Action.WAIT,
        headline="Position open — manage it, no new entries",
        regime=regime,
        price=ctx.price,
        blockers=blockers,
        warnings=warnings,
        notes=notes + guidance,
        consistency=consistency,
    )


def _execution_notes(
    ctx: MarketContext,
    setup: Setup,
    sizing: Sizing,
    spec: ContractSpec,
    state: AccountState,
    cfg: PlaybookConfig,
    net_r: float,
) -> list[str]:
    """Concrete order-management instructions for an accepted trade."""
    notes: list[str] = []
    qty = sizing.quantity

    notes.append(
        f"Risk ${sizing.risk_dollars:,.2f} all-in (stop loss plus commission) of your "
        f"${state.room:,.2f} room — {sizing.risk_dollars / state.room * 100:.1f}%, "
        f"capped by {_factor_phrase(sizing.limiting_factor)}."
    )
    notes.append(
        f"Net {net_r:.2f}R to target 1 after ${spec.commission(qty):,.2f} commission."
    )

    if cfg.scale_out_at_t1 and qty >= 2:
        half = qty // 2
        notes.append(
            f"Scale: take {half} off at {setup.target1:,.2f} "
            f"(+${spec.points_to_dollars(setup.target1_points, half):,.2f}), "
            f"trail the remaining {qty - half} toward {setup.target2:,.2f}."
        )
    elif qty == 1:
        notes.append(
            f"Single contract: no scaling available. Exit at target 1 "
            f"({setup.target1:,.2f}) unless momentum is clearly extending."
        )

    be_price = setup.entry + (setup.stop_points * cfg.move_to_breakeven_at_r) * setup.direction
    notes.append(
        f"Move the stop to breakeven once price reaches {spec.round_to_tick(be_price):,.2f} "
        f"({cfg.move_to_breakeven_at_r:.1f}R)."
    )

    # The intraday-threshold trap, quantified for this specific trade.
    peak_profit = spec.points_to_dollars(setup.target2_points, qty)
    cost = state.room_cost_of_running_profit(peak_profit)
    if cost > 0:
        notes.append(
            f"If this runs to target 2 (+${peak_profit:,.2f} open) your threshold "
            f"rises ${cost:,.2f}. That room is permanent — do not let a winner "
            f"round-trip to breakeven."
        )

    return notes


def _risk_budget_note(state: AccountState, engine: RiskEngine) -> str | None:
    """State how many losing days the current settings can absorb.

    Apex enforces no daily loss limit on most accounts, so this one is entirely
    self-imposed — which makes it easy to set a number that feels careful and
    is not. Expressing it as days-to-breach is the check that makes a limit
    concrete, and it is the calculation most traders never do.
    """
    limits = engine.limits
    drawdown = state.profile.drawdown_amount
    if limits.daily_loss_limit <= 0 or drawdown <= 0:
        return None

    days = limits.days_to_breach(drawdown)
    verdict = "thin" if days < 4 else "workable" if days < 6 else "durable"
    return (
        f"Self-imposed daily stop ${limits.daily_loss_limit:,.2f} — "
        f"{days:.1f} full stop-out days would exhaust a ${drawdown:,.0f} "
        f"drawdown ({verdict}). Apex enforces none, so this one is yours to keep."
    )


def _account_notes(state: AccountState) -> list[str]:
    """Standing facts about the account that colour every decision."""
    notes = [
        f"Equity ${state.equity:,.2f} | threshold ${state.threshold:,.2f} | "
        f"room ${state.room:,.2f}",
    ]
    tier = state.current_tier
    if tier is not None:
        notes.append(
            f"Tier: {tier.max_contracts} mini(s) / {tier.max_contracts * 10} micros, "
            f"firm daily loss ${tier.daily_loss_limit:,.2f} — set by your "
            f"${state.tier_reference_balance:,.2f} closing balance. Intraday profit "
            f"does not raise it until the session ends."
        )
    to_lock = state.profit_to_lock_threshold()
    if to_lock is None:
        pass
    elif to_lock <= 0:
        notes.append(
            f"Threshold is LOCKED at ${state.threshold:,.2f} — it no longer trails you up."
        )
    else:
        notes.append(
            f"${to_lock:,.2f} more peak equity locks the threshold at "
            f"${state.profile.threshold_lock:,.2f} permanently."
        )
    if state.day_pnl != 0:
        notes.append(f"Today's realised P&L: ${state.day_pnl:,.2f} over {state.trades_today} trade(s).")
    return notes


def _regime_phrase(regime: Regime) -> str:
    return {
        Regime.TREND_UP: "uptrend intact, waiting for a pullback to enter",
        Regime.TREND_DOWN: "downtrend intact, waiting for a bounce to enter",
        Regime.CHOP: "choppy and directionless, the worst conditions to force a trade",
        Regime.EXPANSION: "volatility spike; stops must widen and size must shrink",
        Regime.QUIET: "compressed range; breakouts here usually fail",
    }[regime]


def _factor_phrase(factor: str) -> str:
    return {
        "max_risk_per_trade": "your per-trade risk cap",
        "pct_of_room": "the cap on percentage of drawdown room",
        "threshold_buffer": "proximity to your trailing threshold",
        "daily_loss_limit": "remaining daily loss budget",
        "contract_cap": "the account's contract limit",
    }.get(factor, factor)
