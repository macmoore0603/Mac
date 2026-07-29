"""Command-line interface: renders the decision card.

Design goal for the output: everything needed to act, and the honest reason
behind it, visible without scrolling. A trading tool that buries *why* trains
you to click without thinking.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time as _time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from .apex import (
    PRESETS,
    ScalingLadder,
    AccountState,
    RiskEngine,
    RiskLimits,
    TrailingMode,
    check_threshold_consistency,
    payout_status,
)
from .backtest import BacktestConfig, format_report, run_backtest
from .bars import ET, Bar, classify_session, validate_series
from .calendar import CalendarError, EconomicEvent, fetch_economic_calendar
from .contracts import get_contract
from .data import DataError, fetch_live, generate_demo_bars, load_csv
from .market import IndicatorConfig
from .playbook import Action, Directive, PlaybookConfig, evaluate
from .webhook import BarStore, WebhookConfig, WebhookContext, WebhookServer

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GREY = "\033[90m"

WIDTH = 78

_ACTION_COLOUR = {
    Action.GO_LONG: GREEN,
    Action.GO_SHORT: RED,
    Action.WAIT: YELLOW,
    Action.STAND_DOWN: GREY,
    Action.FLATTEN: RED,
}


class Palette:
    """ANSI colours, disabled when output is piped or --no-color is passed."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return f"{''.join(codes)}{text}{RESET}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nqcopilot",
        description="Nasdaq futures intraday copilot with Apex rule enforcement.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  nqcopilot --demo\n"
            "  nqcopilot --csv nq_5m.csv --symbol MNQ --balance 51200 --threshold 48700\n"
            "  nqcopilot --live --symbol MNQ --state ~/.nqcopilot.json --watch 60\n"
            "  nqcopilot --state ~/.nqcopilot.json --record-trade -125.50\n"
        ),
    )

    source = parser.add_argument_group("data source")
    source.add_argument("--csv", type=Path, help="Bars exported from your platform.")
    source.add_argument("--live", action="store_true", help="Fetch delayed bars over HTTP.")
    source.add_argument("--demo", action="store_true", help="Use deterministic synthetic bars.")
    source.add_argument("--interval", default="5m", help="Bar interval for --live (default: 5m).")
    source.add_argument("--lookback", default="5d", help="History window for --live (default: 5d).")

    account = parser.add_argument_group("account")
    account.add_argument(
        "--symbol", default="MNQ", help="Contract to trade (default: MNQ)."
    )
    account.add_argument(
        "--profile",
        default="apex50k-pa",
        choices=sorted(PRESETS),
        help="Account rule preset (default: apex50k-pa).",
    )
    account.add_argument(
        "--tiers",
        type=Path,
        help="JSON scaling ladder: [{\"balance\":50000,\"contracts\":2,"
        "\"daily_loss\":1000}, ...]. Supersedes --max-contracts and "
        "--firm-daily-loss, and tracks tier changes from your end-of-day balance.",
    )
    account.add_argument(
        "--firm-daily-loss",
        type=float,
        help="Your PA tier's daily loss limit. This is a firm rule that ends the "
        "account, unlike --daily-loss which is your own discipline.",
    )
    account.add_argument(
        "--record-payout",
        type=float,
        metavar="AMOUNT",
        help="Book a withdrawal into --state and exit. Resets the consistency window.",
    )
    account.add_argument("--balance", type=float, help="Current closed balance.")
    account.add_argument("--threshold", type=float, help="Current trailing threshold.")
    account.add_argument("--open-pnl", type=float, default=0.0, help="Open position P&L.")
    account.add_argument("--in-position", action="store_true", help="A position is open.")
    account.add_argument("--state", type=Path, help="JSON file to persist account state.")
    account.add_argument(
        "--record-trade",
        type=float,
        metavar="PNL",
        help="Book a closed trade into --state and exit.",
    )
    account.add_argument(
        "--reset-day", action="store_true", help="Roll --state to a new session and exit."
    )

    risk = parser.add_argument_group("risk limits")
    risk.add_argument("--risk-per-trade", type=float, default=250.0)
    risk.add_argument("--daily-loss", type=float, default=600.0)
    risk.add_argument("--daily-target", type=float, default=900.0)
    risk.add_argument("--max-trades", type=int, default=4)
    risk.add_argument("--max-contracts", type=int, help="Override the account contract cap.")
    risk.add_argument("--round-turn", type=float, help="Override round-turn commission.")
    risk.add_argument(
        "--allow-lunch", action="store_true", help="Permit entries in the lunch session."
    )
    risk.add_argument(
        "--news",
        action="append",
        default=[],
        metavar="HH:MM",
        help="Blackout around a release, ET. Repeatable.",
    )
    risk.add_argument(
        "--news-auto",
        action="store_true",
        help="Fetch today's high-impact releases and blackout around them. "
        "Needs RAPIDAPI_KEY (RapidAPI 'Trading View' by apidojo).",
    )
    risk.add_argument(
        "--news-countries",
        default="US",
        help="Country codes for --news-auto (default: US).",
    )
    risk.add_argument(
        "--news-importance",
        type=int,
        default=1,
        choices=[-1, 0, 1],
        help="Minimum importance for --news-auto: -1 low, 0 medium, 1 high (default: 1).",
    )

    replay = parser.add_argument_group("replay")
    replay.add_argument(
        "--backtest",
        action="store_true",
        help="Replay the copilot over the loaded bars instead of reading the last one.",
    )
    replay.add_argument(
        "--slippage-ticks",
        type=float,
        default=1.0,
        help="Adverse slippage applied to entries and stops (default: 1 tick).",
    )
    replay.add_argument(
        "--trades", action="store_true", help="List every trade from the replay."
    )

    serve = parser.add_argument_group("live feed from TradingView")
    serve.add_argument(
        "--serve",
        action="store_true",
        help="Receive bars from TradingView alert webhooks and decide on each close.",
    )
    serve.add_argument("--port", type=int, default=8787, help="Webhook port (default: 8787).")
    serve.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1 — use a tunnel to expose it).",
    )
    serve.add_argument(
        "--secret",
        help="Shared secret the alert must send. Falls back to NQCOPILOT_WEBHOOK_SECRET.",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--json", action="store_true", help="Emit JSON instead of a card.")
    output.add_argument("--no-color", action="store_true")
    output.add_argument("--min-score", type=float, default=55.0)
    output.add_argument(
        "--watch",
        type=int,
        metavar="SECONDS",
        help="Re-evaluate on an interval in ONE long-lived process. Use this "
        "rather than a shell `while true` loop: relaunching a binary makes the "
        "OS re-ask for permissions every iteration.",
    )
    output.add_argument(
        "--clear",
        action="store_true",
        help="Clear the terminal between --watch refreshes. Off by default so "
        "scrollback, logs and screen recordings stay readable.",
    )
    output.add_argument("--verbose", action="store_true", help="Show rejected candidates.")

    return parser


def load_bars(args: argparse.Namespace) -> list[Bar]:
    if args.csv:
        bars = load_csv(args.csv)
    elif args.live:
        bars = fetch_live(args.symbol, args.interval, args.lookback)
    elif args.demo:
        bars = generate_demo_bars()
    elif args.serve:
        # Serving without a seed is legal: the series builds from the feed.
        return []
    else:
        raise DataError("choose a data source: --csv PATH, --live, or --demo")

    validate_series(bars)
    return bars


def load_state(args: argparse.Namespace) -> AccountState:
    """Build account state from --state, explicit flags, or the profile defaults."""
    profile = PRESETS[args.profile]
    # Tier-dependent limits are not knowable from the profile alone.
    overrides = {}
    if getattr(args, "tiers", None) is not None:
        try:
            rows = json.loads(args.tiers.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise DataError(f"cannot read {args.tiers}: {exc}") from exc
        try:
            overrides["scaling"] = ScalingLadder.from_rows(rows)
        except (ValueError, TypeError) as exc:
            raise DataError(f"{args.tiers}: {exc}") from exc
    if getattr(args, "firm_daily_loss", None) is not None:
        overrides["firm_daily_loss_limit"] = args.firm_daily_loss
    if getattr(args, "max_contracts", None) is not None:
        overrides["max_contracts"] = args.max_contracts
    if overrides:
        profile = replace(profile, **overrides)
    today = datetime.now(ET).date()

    stored: dict = {}
    if args.state and args.state.exists():
        try:
            stored = json.loads(args.state.read_text())
        except json.JSONDecodeError as exc:
            raise DataError(f"{args.state} is not valid JSON: {exc}") from exc

    balance = args.balance
    if balance is None:
        balance = stored.get("closed_balance", profile.starting_balance)
    threshold = args.threshold
    if threshold is None:
        threshold = stored.get("threshold", profile.initial_threshold)

    daily = {
        datetime.fromisoformat(k).date(): v
        for k, v in (stored.get("daily_pnl") or {}).items()
    }

    warning = check_threshold_consistency(profile, float(balance), float(threshold))
    if warning:
        # A threshold the drawdown cannot produce almost always means the wrong
        # profile is selected, which would report room the account lacks.
        print(f"  ! Account numbers disagree: {warning}", file=sys.stderr)

    state = AccountState.resume(
        profile,
        closed_balance=float(balance),
        threshold=float(threshold),
        today=today,
        daily_pnl=daily,
    )
    stored_payout = stored.get("last_payout_date")
    if stored_payout:
        state.last_payout_date = datetime.fromisoformat(stored_payout).date()
    state.payouts_taken = int(stored.get("payouts_taken", 0))
    if stored.get("tier_reference_balance") is not None:
        state.tier_reference_balance = float(stored["tier_reference_balance"])
    state.day_start_balance = float(stored.get("day_start_balance", state.closed_balance))
    state.trades_today = int(stored.get("trades_today", 0))
    state.consecutive_losses = int(stored.get("consecutive_losses", 0))

    # A stored session from a previous day must not carry counters into today.
    stored_date = stored.get("session_date")
    if stored_date and datetime.fromisoformat(stored_date).date() != today:
        state.end_session(today)

    state.mark(args.open_pnl)
    return state


def save_state(path: Path, state: AccountState) -> None:
    payload = {
        "profile": state.profile.name,
        "closed_balance": state.closed_balance,
        "threshold": state.threshold,
        "peak_equity": state.peak_equity,
        "day_start_balance": state.day_start_balance,
        "trades_today": state.trades_today,
        "consecutive_losses": state.consecutive_losses,
        "session_date": state.session_date.isoformat() if state.session_date else None,
        "daily_pnl": {k.isoformat(): v for k, v in state.daily_pnl.items()},
        "last_payout_date": (
            state.last_payout_date.isoformat() if state.last_payout_date else None
        ),
        "payouts_taken": state.payouts_taken,
        "tier_reference_balance": state.tier_reference_balance,
        "updated": datetime.now(ET).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def collect_news(
    args: argparse.Namespace,
) -> tuple[list[datetime], list[EconomicEvent], str | None]:
    """Assemble blackout times from manual flags and, optionally, the calendar.

    Returns (times, events, warning). A calendar fetch that fails yields a
    warning rather than an exception: the blackout is a refinement, and refusing
    to produce any read because an HTTP call timed out helps nobody. The warning
    is deliberately loud — a silently missing gate is the dangerous outcome.
    """
    today = datetime.now(ET).date()
    times: list[datetime] = []
    for raw in args.news:
        try:
            hh, mm = (int(p) for p in raw.split(":"))
            times.append(
                datetime.combine(
                    today, datetime.min.time().replace(hour=hh, minute=mm), tzinfo=ET
                )
            )
        except (ValueError, TypeError) as exc:
            raise DataError(f"bad --news value {raw!r}, expected HH:MM") from exc

    events: list[EconomicEvent] = []
    warning: str | None = None
    if args.news_auto:
        try:
            events = fetch_economic_calendar(
                day=today,
                countries=args.news_countries,
                min_importance=args.news_importance,
            )
            times.extend(event.ts for event in events)
        except CalendarError as exc:
            warning = (
                f"news calendar unavailable ({exc}). Blackout windows are NOT "
                f"active — check the schedule yourself before trading."
            )

    return times, events, warning


def build_risk(
    args: argparse.Namespace, state: AccountState, news_times: list[datetime] | None = None
) -> RiskEngine:
    limits = RiskLimits(
        max_risk_per_trade=args.risk_per_trade,
        daily_loss_limit=args.daily_loss,
        daily_profit_lock=args.daily_target if args.daily_target > 0 else None,
        max_trades_per_day=args.max_trades,
        max_contracts_override=args.max_contracts,
        block_lunch=not args.allow_lunch,
    )
    return RiskEngine(state, limits, news_times or [])


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _rule(char: str = "─") -> str:
    return char * WIDTH


def _wrap(text: str, indent: int = 2, width: int = WIDTH) -> list[str]:
    """Wrap text to the card width, preserving a hanging indent."""
    import textwrap

    return textwrap.wrap(text, width=width - indent) or [""]


def render(directive: Directive, state: AccountState, spec, c: Palette, verbose: bool) -> str:
    out: list[str] = []
    colour = _ACTION_COLOUR.get(directive.action, "")
    et = directive.timestamp.astimezone(ET)
    session = classify_session(directive.timestamp)

    out.append(c(_rule("═"), DIM))
    out.append(
        c(f" NQ COPILOT ", BOLD)
        + c(f"│ {spec.symbol} │ {et:%a %d %b %H:%M ET} │ {session.value.replace('_', ' ')}", DIM)
    )
    out.append(c(_rule("═"), DIM))
    out.append("")

    # The instruction, unmissable.
    out.append(f"  {c(directive.action.value, BOLD, colour)}")
    for line in _wrap(directive.headline, indent=4):
        out.append(f"    {line}")
    out.append("")

    setup, sizing = directive.setup, directive.sizing
    if directive.is_actionable and setup and sizing:
        out.append(c("  ORDER", BOLD))
        risk_dollars = spec.points_to_dollars(setup.stop_points, sizing.quantity)
        t1_dollars = spec.points_to_dollars(setup.target1_points, sizing.quantity)
        t2_dollars = spec.points_to_dollars(setup.target2_points, sizing.quantity)
        out.append(
            f"    {'Entry':<10} {setup.entry:>12,.2f}   "
            f"{sizing.quantity} × {spec.symbol}"
        )
        out.append(
            f"    {'Stop':<10} {setup.stop:>12,.2f}   "
            + c(f"−${risk_dollars:,.2f}  ({setup.stop_points:.2f} pts)", RED)
        )
        out.append(
            f"    {'Target 1':<10} {setup.target1:>12,.2f}   "
            + c(f"+${t1_dollars:,.2f}  ({setup.r_multiple_t1:.2f}R)", GREEN)
        )
        out.append(
            f"    {'Target 2':<10} {setup.target2:>12,.2f}   "
            + c(f"+${t2_dollars:,.2f}  ({setup.r_multiple_t2:.2f}R)", GREEN)
        )
        out.append("")

    # Market read.
    out.append(c("  MARKET", BOLD))
    out.append(
        f"    Price {directive.price:,.2f}   Regime "
        + c(directive.regime.value.replace("_", " ").upper(), CYAN)
    )
    out.append("")

    if directive.reasons:
        out.append(c("  WHY", BOLD))
        for reason in directive.reasons:
            for j, line in enumerate(_wrap(reason, indent=6)):
                out.append(f"    {'✓' if j == 0 else ' '} {line}")
        out.append("")

    if directive.warnings:
        out.append(c("  CAUTION", BOLD))
        for warning in directive.warnings:
            for j, line in enumerate(_wrap(warning, indent=6)):
                out.append(c(f"    {'!' if j == 0 else ' '} {line}", YELLOW))
        out.append("")

    blockers = [b for b in directive.blockers if b.is_hard]
    if blockers:
        out.append(c("  BLOCKED BY", BOLD))
        for blocker in blockers:
            for j, line in enumerate(_wrap(blocker.message, indent=6)):
                out.append(c(f"    {'✗' if j == 0 else ' '} {line}", RED))
        out.append("")

    if directive.notes:
        out.append(c("  ACCOUNT", BOLD))
        for note in directive.notes:
            for j, line in enumerate(_wrap(note, indent=6)):
                out.append(f"    {'·' if j == 0 else ' '} {line}")
        out.append("")

    payout = directive.consistency
    if payout is not None and (payout.required_total or payout.required_days):
        out.append(c("  PAYOUT", BOLD))
        for line in _wrap(payout.message, indent=6):
            out.append(f"      {line}")
        out.append(
            f"      Days {payout.qualifying_days}/{payout.required_days}  "
            f"withdrawable ${payout.withdrawable:,.2f}  "
            f"best day ${payout.best_day:,.2f} of ${payout.total_profit:,.2f}"
        )
        extra = payout.max_additional_today
        if extra is not None and extra > 0:
            for line in _wrap(
                f"Banking more than ${extra:,.2f} further today would make today "
                f"too large a share of the total and delay your payout.",
                indent=6,
            ):
                out.append(c(f"      {line}", DIM))
        out.append("")

    if verbose and len(directive.candidates) > 1:
        out.append(c("  OTHER CANDIDATES", BOLD))
        for cand in directive.candidates[1:]:
            out.append(
                c(f"      {cand.name} {cand.side} — {cand.score:.0f}/100", DIM)
            )
        out.append("")

    out.append(c(_rule(), DIM))
    out.append(
        c(
            "  Signals are probabilistic and will be wrong regularly. "
            "Rule gates are not.",
            DIM,
        )
    )
    out.append(c(_rule(), DIM))
    return "\n".join(out)


def directive_to_dict(directive: Directive, state: AccountState) -> dict:
    setup = directive.setup
    sizing = directive.sizing
    return {
        "timestamp": directive.timestamp.isoformat(),
        "action": directive.action.name,
        "headline": directive.headline,
        "regime": directive.regime.value,
        "price": directive.price,
        "setup": (
            {
                "name": setup.name,
                "side": setup.side,
                "entry": setup.entry,
                "stop": setup.stop,
                "target1": setup.target1,
                "target2": setup.target2,
                "stop_points": setup.stop_points,
                "r_multiple_t1": setup.r_multiple_t1,
                "score": setup.score,
            }
            if setup
            else None
        ),
        "sizing": asdict(sizing) if sizing else None,
        "blockers": [
            {"code": b.code, "message": b.message, "severity": b.severity.value}
            for b in directive.blockers
        ],
        "reasons": directive.reasons,
        "warnings": directive.warnings,
        "notes": directive.notes,
        "account": {
            "equity": state.equity,
            "threshold": state.threshold,
            "room": state.room,
            "day_pnl": state.day_pnl,
            "trades_today": state.trades_today,
            "threshold_locked": state.threshold_is_locked,
        },
    }


def run_replay(  # noqa: PLR0913 - mirrors run_once's dependencies
    args: argparse.Namespace,
    spec,
    state: AccountState,
    bars: list[Bar],
    engine: RiskEngine,
    c: Palette,
) -> int:
    """Replay the copilot over the loaded bars and print a summary."""
    result = run_backtest(
        bars,
        spec,
        state,
        limits=engine.limits,
        playbook=PlaybookConfig(min_score=args.min_score),
        config=BacktestConfig(slippage_ticks=args.slippage_ticks),
    )

    if args.json:
        metrics = result.metrics
        print(
            json.dumps(
                {
                    "metrics": asdict(metrics),
                    "breached": result.breached,
                    "breach_ts": result.breach_ts.isoformat() if result.breach_ts else None,
                    "final_balance": result.state.closed_balance,
                    "final_threshold": result.state.threshold,
                    "trades": [
                        {
                            "setup": t.setup_name,
                            "side": t.side,
                            "quantity": t.quantity,
                            "entry_ts": t.entry_ts.isoformat(),
                            "entry": t.entry_price,
                            "exit_ts": t.exit_ts.isoformat(),
                            "exit": t.exit_price,
                            "net_pnl": t.net_pnl,
                            "r": t.r_multiple,
                            "reason": t.exit_reason,
                        }
                        for t in result.trades
                    ],
                },
                indent=2,
            )
        )
        return 0

    print(format_report(result, spec))

    if args.trades and result.trades:
        print("\n Trades")
        print(
            f"   {'Entry':<17}{'Setup':<26}{'Side':<7}{'Qty':>4}"
            f"{'Net':>11}{'R':>7}  Exit"
        )
        for t in result.trades:
            colour = GREEN if t.net_pnl > 0 else RED
            print(
                f"   {t.entry_ts:%m-%d %H:%M}    {t.setup_name:<26}{t.side:<7}{t.quantity:>4}"
                + c(f"{t.net_pnl:>11,.2f}", colour)
                + f"{t.r_multiple:>7.2f}  {t.exit_reason}"
            )

    return 0


def run_server(
    args: argparse.Namespace,
    spec,
    state: AccountState,
    bars: list[Bar],
    engine: RiskEngine,
    c: Palette,
) -> int:
    """Serve the TradingView webhook endpoint and decide on every bar close."""
    secret = args.secret or os.environ.get("NQCOPILOT_WEBHOOK_SECRET")

    def on_directive(directive, meta) -> None:
        symbol = meta.get("symbol") or spec.symbol
        interval = meta.get("interval") or ""
        banner = f"── {symbol} {interval} bar received ──"
        print("\n" + c(banner, DIM))
        print(render(directive, state, spec, c, args.verbose))
        # Cross-check the chart's own read against the engine's.
        chart_action = (meta.get("tv_action") or "").replace(" ", "_")
        if chart_action and chart_action != directive.action.name:
            print(
                c(
                    f"  note: chart said {meta.get('tv_action')}, engine says "
                    f"{directive.action.value}. Differences usually mean the chart "
                    f"and the engine disagree on account state — trust the engine, "
                    f"it knows your threshold.",
                    YELLOW,
                )
            )
        if args.state:
            save_state(args.state, state)

    context = WebhookContext(
        config=WebhookConfig(host=args.bind, port=args.port, secret=secret),
        spec=spec,
        state=state,
        store=BarStore(bars),
        engine=engine,
        playbook=PlaybookConfig(min_score=args.min_score),
        indicators=IndicatorConfig(),
        on_directive=on_directive,
    )

    server = WebhookServer(context)
    print(c("  NQ COPILOT — live feed", BOLD))
    print(f"    Listening on {c(server.url, CYAN)}")
    print(f"    Seeded with {len(bars)} bar(s) for {spec.symbol}")
    if not secret:
        print(
            c(
                "    WARNING: no shared secret set. Anyone who can reach this port "
                "can inject fake bars. Use --secret.",
                YELLOW,
                BOLD,
            )
        )
    if args.bind in ("127.0.0.1", "localhost"):
        print(
            c(
                "    Bound to localhost — TradingView cannot reach it directly. "
                "Expose it with a tunnel (cloudflared/ngrok) and use that URL.",
                DIM,
            )
        )
    if len(bars) < 80:
        print(
            c(
                f"    Only {len(bars)} bars seeded; the engine needs ~80 before it "
                f"will read anything. Seed with --csv for an immediate read.",
                DIM,
            )
        )
    print(c("    Ctrl-C to stop.\n", DIM))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


MIN_WATCH_SECONDS = 5


def run_watch(args: argparse.Namespace, c: Palette) -> int:
    """Re-evaluate on an interval inside a single long-lived process.

    Deliberately one process rather than a relaunching shell loop. Beyond being
    wasteful, repeatedly relaunching a binary makes the operating system treat
    each iteration as a new program: macOS re-asks for permissions per launch,
    so a `while true; do …; sleep 5; done` wrapper produces a permission dialog
    every five seconds. Keeping one process alive asks once.

    A transient failure — a rate-limited quote endpoint, a momentary DNS blip —
    must not end the session either. Those are reported and retried rather than
    killing a monitor you are relying on mid-session.
    """
    interval = max(args.watch, MIN_WATCH_SECONDS)
    if interval != args.watch:
        print(
            c(
                f"  Watch interval raised to {interval}s: polling faster adds load "
                f"without adding information on {args.interval} bars.",
                DIM,
            ),
            file=sys.stderr,
        )

    consecutive_errors = 0
    while True:
        if args.clear:
            print("\033[2J\033[H", end="")
        try:
            run_once(args, c)
            consecutive_errors = 0
        except DataError as exc:
            consecutive_errors += 1
            print(
                c(f"  ! {exc} (retry {consecutive_errors} in {interval}s)", YELLOW),
                file=sys.stderr,
            )
        except KeyboardInterrupt:
            return 130
        _time.sleep(interval)


def run_once(args: argparse.Namespace, c: Palette) -> int:
    spec = get_contract(args.symbol)
    if args.round_turn is not None:
        spec = type(spec)(
            symbol=spec.symbol,
            name=spec.name,
            tick_size=spec.tick_size,
            tick_value=spec.tick_value,
            round_turn_cost=args.round_turn,
        )

    state = load_state(args)
    bars = load_bars(args)
    news_times, events, news_warning = collect_news(args)
    engine = build_risk(args, state, news_times)

    if news_warning:
        print(c(f"  ! {news_warning}", YELLOW, BOLD), file=sys.stderr)

    if args.serve:
        return run_server(args, spec, state, bars, engine, c)

    if args.backtest:
        return run_replay(args, spec, state, bars, engine, c)

    directive = evaluate(
        bars,
        spec,
        state,
        risk=engine,
        config=PlaybookConfig(min_score=args.min_score),
        indicators=IndicatorConfig(),
        in_position=args.in_position,
    )

    if args.json:
        print(json.dumps(directive_to_dict(directive, state), indent=2))
    else:
        print(render(directive, state, spec, c, args.verbose))
        if events:
            print(c("\n  SCHEDULED RELEASES TODAY", BOLD))
            now = datetime.now(ET)
            for event in events:
                marker = "·" if event.ts < now else "→"
                print(f"    {marker} {event}")
        if state.profile.verify_note:
            print(c(f"\n  Rule assumptions — {state.profile.verify_note}", DIM))

    if args.state:
        save_state(args.state, state)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    c = Palette(enabled=not args.no_color and sys.stdout.isatty())

    try:
        # State-mutating shortcuts run without needing market data.
        if args.record_trade is not None:
            if not args.state:
                parser.error("--record-trade requires --state")
            state = load_state(args)
            state.close_trade(args.record_trade)
            save_state(args.state, state)
            verb = "profit" if args.record_trade >= 0 else "loss"
            print(
                f"Recorded {verb} of ${abs(args.record_trade):,.2f}. "
                f"Balance ${state.closed_balance:,.2f}, threshold ${state.threshold:,.2f}, "
                f"room ${state.room:,.2f}, {state.trades_today} trade(s) today."
            )
            return 0

        if args.record_payout is not None:
            if not args.state:
                parser.error("--record-payout requires --state")
            state = load_state(args)
            status = payout_status(state)
            if not status.eligible:
                print(c(f"  ! {status.message}", YELLOW), file=sys.stderr)
            state.record_payout(args.record_payout)
            save_state(args.state, state)
            print(
                f"Recorded payout of ${args.record_payout:,.2f}. "
                f"Balance ${state.closed_balance:,.2f}, threshold "
                f"${state.threshold:,.2f}, room ${state.room:,.2f}. "
                f"Payout {state.payouts_taken}"
                + (f" of {state.profile.payout_lifetime_cap}" if state.profile.payout_lifetime_cap else "")
                + ". Consistency window reset."
            )
            return 0

        if args.reset_day:
            if not args.state:
                parser.error("--reset-day requires --state")
            state = load_state(args)
            state.end_session(datetime.now(ET).date())
            save_state(args.state, state)
            print(
                f"New session. Balance ${state.closed_balance:,.2f}, "
                f"threshold ${state.threshold:,.2f}, room ${state.room:,.2f}."
            )
            return 0

        if args.watch:
            return run_watch(args, c)

        return run_once(args, c)

    except DataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
