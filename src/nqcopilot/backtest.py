"""Bar-by-bar replay of the copilot against historical data.

Backtests lie by default. Every modelling choice here is deliberately the
pessimistic one, because a harness that flatters the strategy is worse than no
harness at all — it produces confidence you have not earned.

The choices, stated plainly:

* **Entry fills on the next bar's open, plus slippage.** The signal is generated
  from a closed bar, so its close has already happened and is not available to
  trade. Filling at the signal bar's close is the single most common way a
  backtest invents profit that does not exist.
* **Ambiguous bars resolve against you.** When one bar's range contains both the
  stop and a target, there is no way to know which came first without tick data.
  This assumes the stop. Real results will differ; they will not differ in the
  flattering direction.
* **Gaps fill at the open.** If price gaps through the stop, the fill is the
  open, not the stop price.
* **The threshold ratchets on each bar's favourable extreme**, not its close, so
  the intraday drawdown mechanic is modelled at its most punishing.
* **Commission is charged per contract on every exit**, including partial
  scale-outs.

What this still cannot model: real slippage on a fast tape, partial fills, order
rejects, whether your platform was even connected. Treat the output as an upper
bound on a strategy's quality, never as an expectation.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime

from .apex import AccountState, RiskEngine, RiskLimits, consistency_status
from .bars import Bar, Session, classify_session, trading_date
from .contracts import ContractSpec
from .market import IndicatorConfig, MarketContext
from .playbook import PlaybookConfig, evaluate_context
from .signals import Setup


@dataclass(frozen=True)
class BacktestConfig:
    """Execution assumptions for the replay."""

    slippage_ticks: float = 1.0
    warmup_bars: int = 80
    scale_out_at_t1: bool = True
    breakeven_at_r: float = 1.0
    flatten_at_session_end: bool = True
    stop_on_breach: bool = True
    # These are momentum setups. A thesis that has not resolved within a couple
    # of hours has stopped being the trade that was entered, and holding it ties
    # up drawdown room while the threshold keeps ratcheting on every new high.
    max_hold_bars: int = 24


@dataclass
class CompletedTrade:
    """One round trip, from entry fill to fully flat."""

    setup_name: str
    direction: int
    quantity: int
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float          # size-weighted average across partial exits
    gross_pnl: float
    commission: float
    net_pnl: float
    r_multiple: float          # net, relative to planned risk
    exit_reason: str
    mae_points: float          # maximum adverse excursion
    mfe_points: float          # maximum favourable excursion
    planned_risk: float

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0

    @property
    def side(self) -> str:
        return "LONG" if self.direction > 0 else "SHORT"


@dataclass
class _OpenPosition:
    """Internal working state for a live position."""

    setup: Setup
    quantity: int
    remaining: int
    entry_price: float
    entry_ts: datetime
    stop: float
    planned_risk: float
    realised: float = 0.0
    commission: float = 0.0
    scaled: bool = False
    bars_held: int = 0
    mae_points: float = 0.0
    mfe_points: float = 0.0
    exit_value: float = 0.0    # sum of price * quantity over exits
    exit_qty: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def direction(self) -> int:
        return self.setup.direction

    def unrealised(self, price: float, spec: ContractSpec) -> float:
        move = (price - self.entry_price) * self.direction
        return spec.points_to_dollars(move, self.remaining)


@dataclass
class Metrics:
    """Summary statistics for a replay."""

    trades: int
    wins: int
    losses: int
    win_rate: float
    net_pnl: float
    gross_profit: float
    gross_loss: float
    profit_factor: float
    expectancy: float          # dollars per trade
    expectancy_r: float        # R per trade
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    max_consecutive_losses: int
    max_drawdown: float
    total_commission: float
    trading_days: int
    trades_per_day: float
    best_day: float
    worst_day: float

    def format(self) -> str:
        pf = "inf" if math.isinf(self.profit_factor) else f"{self.profit_factor:.2f}"
        return "\n".join(
            [
                f"Trades            {self.trades}  ({self.wins}W / {self.losses}L, "
                f"{self.win_rate * 100:.1f}% win rate)",
                f"Net P&L           ${self.net_pnl:,.2f}  (commission ${self.total_commission:,.2f})",
                f"Profit factor     {pf}",
                f"Expectancy        ${self.expectancy:,.2f} per trade  ({self.expectancy_r:+.3f}R)",
                f"Avg win / loss    ${self.avg_win:,.2f} / ${self.avg_loss:,.2f}",
                f"Largest win/loss  ${self.largest_win:,.2f} / ${self.largest_loss:,.2f}",
                f"Max drawdown      ${self.max_drawdown:,.2f}",
                f"Worst streak      {self.max_consecutive_losses} consecutive losses",
                f"Activity          {self.trading_days} days, {self.trades_per_day:.1f} trades/day",
                f"Best / worst day  ${self.best_day:,.2f} / ${self.worst_day:,.2f}",
            ]
        )


@dataclass
class BacktestResult:
    """Everything the replay produced."""

    trades: list[CompletedTrade]
    equity_curve: list[tuple[datetime, float]]
    threshold_curve: list[tuple[datetime, float]]
    state: AccountState
    breached: bool
    breach_ts: datetime | None
    bars_processed: int
    config: BacktestConfig

    @property
    def metrics(self) -> Metrics:
        return compute_metrics(self)

    def daily_pnl(self) -> dict[date, float]:
        out: dict[date, float] = {}
        for trade in self.trades:
            day = trading_date(trade.exit_ts)
            out[day] = out.get(day, 0.0) + trade.net_pnl
        return out


def compute_metrics(result: BacktestResult) -> Metrics:
    """Derive summary statistics from a completed replay."""
    trades = result.trades
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]

    gross_profit = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))
    net = sum(t.net_pnl for t in trades)

    # Longest run of losers, which is what actually breaks discipline.
    streak = worst_streak = 0
    for trade in trades:
        if trade.net_pnl < 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        elif trade.net_pnl > 0:
            streak = 0

    peak = -math.inf
    max_dd = 0.0
    for _, equity in result.equity_curve:
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    by_day = result.daily_pnl()
    day_values = list(by_day.values())

    return Metrics(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(trades) if trades else 0.0,
        net_pnl=net,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else math.inf,
        expectancy=net / len(trades) if trades else 0.0,
        expectancy_r=statistics.fmean([t.r_multiple for t in trades]) if trades else 0.0,
        avg_win=statistics.fmean([t.net_pnl for t in wins]) if wins else 0.0,
        avg_loss=statistics.fmean([t.net_pnl for t in losses]) if losses else 0.0,
        largest_win=max((t.net_pnl for t in wins), default=0.0),
        largest_loss=min((t.net_pnl for t in losses), default=0.0),
        max_consecutive_losses=worst_streak,
        max_drawdown=max_dd,
        total_commission=sum(t.commission for t in trades),
        trading_days=len(by_day),
        trades_per_day=len(trades) / len(by_day) if by_day else 0.0,
        best_day=max(day_values, default=0.0),
        worst_day=min(day_values, default=0.0),
    )


def run_backtest(
    bars: list[Bar],
    spec: ContractSpec,
    state: AccountState,
    *,
    limits: RiskLimits | None = None,
    playbook: PlaybookConfig | None = None,
    indicators: IndicatorConfig | None = None,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Replay the copilot bar by bar over `bars`, mutating `state` as it goes.

    The account state evolves exactly as it would live: the threshold trails,
    daily counters roll, and the risk engine gates entries using the state as it
    stood at that moment. That is the point — this tests the whole system, not
    just the signal layer.
    """
    cfg = config or BacktestConfig()
    engine = RiskEngine(state, limits or RiskLimits())

    # Built once. Every series is causal, so stepping the index is safe.
    ctx = MarketContext.build(bars, spec, indicators)
    slip = cfg.slippage_ticks * spec.tick_size

    trades: list[CompletedTrade] = []
    equity_curve: list[tuple[datetime, float]] = []
    threshold_curve: list[tuple[datetime, float]] = []

    position: _OpenPosition | None = None
    pending: tuple[Setup, int] | None = None
    breached = False
    breach_ts: datetime | None = None
    processed = 0

    start = max(cfg.warmup_bars, 1)
    if state.session_date is None and bars:
        state.session_date = trading_date(bars[start].ts) if start < len(bars) else None

    for i in range(start, len(bars)):
        bar = bars[i]
        processed += 1
        day = trading_date(bar.ts)

        # --- session roll -------------------------------------------------
        if state.session_date is not None and day != state.session_date:
            if position is not None:
                position, trade = _close_all(
                    position, bar.open, bar.ts, "session_end", spec, state
                )
                trades.append(trade)
            pending = None
            state.end_session(day)

        # --- fill a pending entry at this bar's open ----------------------
        if pending is not None and position is None:
            setup, quantity = pending
            pending = None
            fill = bar.open + slip * setup.direction  # slippage is always adverse
            fill = spec.round_to_tick(fill)
            planned_risk = abs(fill - setup.stop)
            # If the open gapped past the stop, the trade is already invalid.
            still_valid = (
                (fill > setup.stop) if setup.is_long else (fill < setup.stop)
            )
            if still_valid and planned_risk > 0:
                position = _OpenPosition(
                    setup=setup,
                    quantity=quantity,
                    remaining=quantity,
                    entry_price=fill,
                    entry_ts=bar.ts,
                    stop=setup.stop,
                    planned_risk=spec.points_to_dollars(planned_risk, quantity),
                )

        # --- manage an open position --------------------------------------
        if position is not None:
            position, closed = _manage(position, bar, spec, state, cfg)
            if closed is not None:
                trades.append(closed)
                position = None

        # --- mark to market ------------------------------------------------
        if position is not None:
            state.mark(position.unrealised(bar.close, spec))
        else:
            state.mark(0.0)

        equity_curve.append((bar.ts, state.equity))
        threshold_curve.append((bar.ts, state.threshold))

        if state.is_breached:
            breached = True
            breach_ts = bar.ts
            if cfg.stop_on_breach:
                break

        # --- look for a new signal, only when genuinely flat ---------------
        if position is None and pending is None:
            directive = evaluate_context(
                ctx.at_index(i), state, risk=engine, config=playbook
            )
            if directive.is_actionable and directive.sizing is not None:
                pending = (directive.setup, directive.sizing.quantity)

    # Anything still open at the end is closed at the last price, so the result
    # never contains an unresolved position flattering the equity curve.
    if position is not None and bars:
        last = bars[min(processed + start - 1, len(bars) - 1)]
        position, trade = _close_all(position, last.close, last.ts, "end_of_data", spec, state)
        trades.append(trade)

    return BacktestResult(
        trades=trades,
        equity_curve=equity_curve,
        threshold_curve=threshold_curve,
        state=state,
        breached=breached,
        breach_ts=breach_ts,
        bars_processed=processed,
        config=cfg,
    )


def _manage(
    position: _OpenPosition,
    bar: Bar,
    spec: ContractSpec,
    state: AccountState,
    cfg: BacktestConfig,
) -> tuple[_OpenPosition | None, CompletedTrade | None]:
    """Advance an open position through one bar."""
    setup = position.setup
    is_long = setup.is_long
    slip = cfg.slippage_ticks * spec.tick_size
    position.bars_held += 1

    # Excursions, measured from the bar's extremes.
    adverse = (position.entry_price - bar.low) if is_long else (bar.high - position.entry_price)
    favourable = (bar.high - position.entry_price) if is_long else (position.entry_price - bar.low)
    position.mae_points = max(position.mae_points, adverse)
    position.mfe_points = max(position.mfe_points, favourable)

    # Ratchet the threshold on the favourable extreme, not the close. This is
    # the punishing reading of the intraday rule and the correct one to plan by.
    peak_price = bar.high if is_long else bar.low
    state.mark(position.unrealised(peak_price, spec))

    gapped = (bar.open <= position.stop) if is_long else (bar.open >= position.stop)
    stop_hit = (bar.low <= position.stop) if is_long else (bar.high >= position.stop)
    t1_hit = (bar.high >= setup.target1) if is_long else (bar.low <= setup.target1)
    t2_hit = (bar.high >= setup.target2) if is_long else (bar.low <= setup.target2)

    # A gap through the stop fills at the open, which is worse than the stop.
    if gapped:
        return _close_all(position, bar.open, bar.ts, "gap_through_stop", spec, state)

    # Ambiguity resolves against us: if both the stop and a target are inside
    # this bar's range, assume the stop came first. Without tick data there is
    # no honest way to claim otherwise.
    if stop_hit:
        fill = position.stop - slip if is_long else position.stop + slip
        reason = "breakeven_stop" if position.scaled else "stop"
        return _close_all(position, fill, bar.ts, reason, spec, state)

    # Scale out at target 1 and protect the remainder.
    if t1_hit and not position.scaled and cfg.scale_out_at_t1 and position.remaining >= 2:
        half = position.remaining // 2
        _partial_exit(position, setup.target1, half, spec)
        position.scaled = True
        position.stop = position.entry_price  # breakeven on the runner

    if t2_hit:
        return _close_all(position, setup.target2, bar.ts, "target2", spec, state)

    if t1_hit and not position.scaled:
        # A single contract cannot scale, so target 1 is the whole exit.
        return _close_all(position, setup.target1, bar.ts, "target1", spec, state)

    # Flatten into the close rather than carrying risk past the deadline.
    if cfg.flatten_at_session_end and classify_session(bar.ts) is Session.CLOSING:
        return _close_all(position, bar.close, bar.ts, "session_close", spec, state)

    # Time stop: the setup's premise has expired.
    if cfg.max_hold_bars and position.bars_held >= cfg.max_hold_bars:
        fill = bar.close - slip if is_long else bar.close + slip
        return _close_all(position, fill, bar.ts, "time_stop", spec, state)

    return position, None


def _partial_exit(
    position: _OpenPosition, price: float, quantity: int, spec: ContractSpec
) -> None:
    """Book a scale-out without closing the position."""
    if quantity <= 0:
        return
    move = (price - position.entry_price) * position.direction
    position.realised += spec.points_to_dollars(move, quantity)
    position.commission += spec.commission(quantity)
    position.remaining -= quantity
    position.exit_value += price * quantity
    position.exit_qty += quantity


def _close_all(
    position: _OpenPosition,
    price: float,
    ts: datetime,
    reason: str,
    spec: ContractSpec,
    state: AccountState,
) -> tuple[None, CompletedTrade]:
    """Close the remainder and book the completed trade into the account."""
    _partial_exit(position, price, position.remaining, spec)

    gross = position.realised
    commission = position.commission
    net = gross - commission
    state.close_trade(net)

    avg_exit = position.exit_value / position.exit_qty if position.exit_qty else price
    r_multiple = net / position.planned_risk if position.planned_risk > 0 else 0.0

    trade = CompletedTrade(
        setup_name=position.setup.name,
        direction=position.direction,
        quantity=position.quantity,
        entry_ts=position.entry_ts,
        entry_price=position.entry_price,
        exit_ts=ts,
        exit_price=avg_exit,
        gross_pnl=gross,
        commission=commission,
        net_pnl=net,
        r_multiple=r_multiple,
        exit_reason=reason,
        mae_points=position.mae_points,
        mfe_points=position.mfe_points,
        planned_risk=position.planned_risk,
    )
    return None, trade


def format_report(result: BacktestResult, spec: ContractSpec) -> str:
    """Render a replay summary."""
    lines: list[str] = []
    state = result.state
    metrics = result.metrics

    lines.append("=" * 78)
    lines.append(f" REPLAY — {spec.symbol} — {state.profile.name}")
    lines.append("=" * 78)

    if result.equity_curve:
        first_ts = result.equity_curve[0][0]
        last_ts = result.equity_curve[-1][0]
        lines.append(f" {first_ts:%Y-%m-%d %H:%M} → {last_ts:%Y-%m-%d %H:%M} "
                     f"({result.bars_processed} bars)")
    lines.append("")

    if metrics.trades == 0:
        lines.append("  No trades were taken. Either the filters never cleared or the")
        lines.append("  sample is too short. This is information, not a failure.")
        lines.append("=" * 78)
        return "\n".join(lines)

    lines.append(metrics.format())
    lines.append("")

    lines.append(f" Account   balance ${state.closed_balance:,.2f}  "
                 f"threshold ${state.threshold:,.2f}  room ${state.room:,.2f}")
    if result.breached:
        lines.append(f" BREACHED at {result.breach_ts:%Y-%m-%d %H:%M} — the account would be gone.")
    elif state.threshold_is_locked:
        lines.append(" Threshold LOCKED — the trailing drawdown can no longer take this account.")
    else:
        to_lock = state.profit_to_lock_threshold()
        if to_lock:
            lines.append(f" ${to_lock:,.2f} more peak equity would lock the threshold.")

    status = consistency_status(state)
    if status.required_total:
        lines.append(f" Payout    {status.message}")
    lines.append("")

    # Per-setup breakdown: which patterns actually carried the result.
    by_setup: dict[str, list[CompletedTrade]] = {}
    for trade in result.trades:
        by_setup.setdefault(trade.setup_name, []).append(trade)

    lines.append(" By setup")
    for name, group in sorted(by_setup.items(), key=lambda kv: -sum(t.net_pnl for t in kv[1])):
        net = sum(t.net_pnl for t in group)
        wins = sum(1 for t in group if t.net_pnl > 0)
        lines.append(
            f"   {name:<26} {len(group):>3} trades  {wins:>3}W  ${net:>10,.2f}"
        )
    lines.append("")

    # Exit reasons expose whether the plan is being followed or overrun.
    by_reason: dict[str, int] = {}
    for trade in result.trades:
        by_reason[trade.exit_reason] = by_reason.get(trade.exit_reason, 0) + 1
    lines.append(" Exits     " + ", ".join(f"{k}: {v}" for k, v in sorted(by_reason.items())))
    lines.append("")

    lines.append("-" * 78)
    lines.append(" Modelled pessimistically: next-bar-open fills with slippage, ambiguous")
    lines.append(" bars resolved as losses, gaps filled at the open, commission on every")
    lines.append(" exit. Real results will be worse than this, not better.")
    lines.append("=" * 78)
    return "\n".join(lines)
