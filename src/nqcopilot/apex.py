"""Apex account model: trailing threshold, position sizing, and hard rule gates.

This module is the part of the copilot that is allowed to be absolute. Signal
generation is probabilistic and will be wrong regularly; the arithmetic here is
not. If a guard in this module says no, the playbook cannot override it.

The single most important behaviour modelled here is that on an **intraday**
trailing-threshold account the threshold follows peak *equity*, including
unrealised open profit. A trade that runs +$800 in your favour and returns to
breakeven costs you $800 of drawdown room permanently, without booking a cent.
That mechanic ends more Apex accounts than bad entries do, so it is modelled
explicitly and surfaced in every decision (`room_cost_of_running_profit`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum

from .bars import APEX_FLATTEN_BY, ET, Session, classify_session, is_market_open
from .contracts import MICRO_PARENT, MICROS_PER_MINI, ContractSpec, is_micro


class TrailingMode(Enum):
    """How the drawdown threshold follows the account."""

    INTRADAY = "intraday"  # trails peak equity tick-by-tick, unrealised included
    END_OF_DAY = "eod"     # trails the closing balance only
    STATIC = "static"      # fixed floor, never moves


@dataclass(frozen=True)
class AccountProfile:
    """Static parameters of a funded account.

    Every field is configurable because prop-firm rules change without notice
    and differ between account variants (evaluation vs PA, intraday vs EOD,
    Rithmic vs Tradovate). The bundled presets are a starting point, not an
    authority — `verify_note` is printed by the CLI for exactly that reason.
    """

    name: str
    starting_balance: float
    drawdown_amount: float
    trailing_mode: TrailingMode
    max_contracts: int              # in minis; micros allowed at 10x
    threshold_lock: float | None = None   # threshold freezes at this level
    profit_target: float | None = None
    consistency_pct: float | None = None  # e.g. 0.30 for the 30% rule
    flatten_by: time = APEX_FLATTEN_BY
    allow_overnight: bool = False
    verify_note: str = ""

    @property
    def initial_threshold(self) -> float:
        return self.starting_balance - self.drawdown_amount

    def contract_cap(self, spec: ContractSpec) -> int:
        """Maximum quantity of `spec` this account may hold."""
        if is_micro(spec):
            return self.max_contracts * MICROS_PER_MINI
        return self.max_contracts


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------
# These reflect the widely-published parameters for Apex's $50K intraday
# trailing-threshold account. Published third-party summaries disagree on the
# lock level in particular, and Apex has shipped several account variants, so
# treat these as defaults to confirm rather than facts to rely on.

APEX_50K_INTRADAY = AccountProfile(
    name="Apex $50K (intraday trailing)",
    starting_balance=50_000.0,
    drawdown_amount=2_500.0,
    trailing_mode=TrailingMode.INTRADAY,
    max_contracts=10,
    threshold_lock=50_100.0,
    profit_target=3_000.0,
    consistency_pct=0.30,
    verify_note=(
        "Confirm against your Apex dashboard: drawdown $2,500, threshold starts "
        "$47,500, freezes at $50,100, cap 10 minis / 100 micros, 30% consistency."
    ),
)

APEX_50K_EOD = AccountProfile(
    name="Apex $50K (end-of-day trailing)",
    starting_balance=50_000.0,
    drawdown_amount=2_000.0,
    trailing_mode=TrailingMode.END_OF_DAY,
    max_contracts=10,
    threshold_lock=52_100.0,
    profit_target=3_000.0,
    consistency_pct=0.30,
    verify_note="Confirm against your Apex dashboard: EOD trailing, $2,000 drawdown.",
)

PRESETS: dict[str, AccountProfile] = {
    "apex50k-intraday": APEX_50K_INTRADAY,
    "apex50k-eod": APEX_50K_EOD,
}


@dataclass(frozen=True)
class RiskLimits:
    """Self-imposed limits. Apex sets almost none of these; survival requires them.

    Apex imposes no daily loss limit, which is precisely why one is essential:
    without it a single bad session can consume a threshold that took weeks to
    build. Defaults are deliberately conservative for a $50K account.
    """

    max_risk_per_trade: float = 250.0
    max_risk_pct_of_room: float = 0.10   # never stake >10% of remaining room
    daily_loss_limit: float = 600.0
    daily_profit_lock: float | None = 900.0  # bank the day after this much
    max_trades_per_day: int = 4
    max_consecutive_losses: int = 2
    min_room_to_trade: float = 400.0
    threshold_safety_buffer: float = 150.0   # never plan into the threshold itself
    max_contracts_override: int | None = None
    min_stop_points: float = 8.0    # NQ noise floor; tighter stops are coin flips
    max_stop_points: float = 60.0   # beyond this the setup is not intraday
    block_lunch: bool = True
    block_first_minutes: float = 5.0    # let the opening auction settle
    news_blackout_minutes: float = 15.0


class Severity(Enum):
    HARD = "hard"   # trading is forbidden; the playbook cannot override
    SOFT = "soft"   # advisory


@dataclass(frozen=True)
class Blocker:
    """A reason not to trade."""

    code: str
    message: str
    severity: Severity = Severity.HARD

    @property
    def is_hard(self) -> bool:
        return self.severity is Severity.HARD


@dataclass
class AccountState:
    """Live account state, including the trailing threshold.

    `closed_balance` is realised only. `open_pnl` is the current mark-to-market
    of any open position and must be updated as price moves, because on an
    intraday account it feeds the threshold.
    """

    profile: AccountProfile
    closed_balance: float
    peak_equity: float
    threshold: float
    open_pnl: float = 0.0
    session_date: date | None = None
    day_start_balance: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    daily_pnl: dict[date, float] = field(default_factory=dict)

    @classmethod
    def fresh(cls, profile: AccountProfile, today: date | None = None) -> "AccountState":
        """A brand-new account at its starting balance."""
        return cls(
            profile=profile,
            closed_balance=profile.starting_balance,
            peak_equity=profile.starting_balance,
            threshold=profile.initial_threshold,
            session_date=today,
            day_start_balance=profile.starting_balance,
        )

    @classmethod
    def resume(
        cls,
        profile: AccountProfile,
        *,
        closed_balance: float,
        threshold: float,
        today: date | None = None,
        daily_pnl: dict[date, float] | None = None,
    ) -> "AccountState":
        """Rebuild state from the two numbers Apex shows you: balance and threshold.

        Peak equity is inferred from the threshold, which is exact while the
        threshold is still trailing. Once the threshold has locked, the inferred
        peak is a lower bound — harmless, because a locked threshold no longer
        moves.
        """
        inferred_peak = max(closed_balance, threshold + profile.drawdown_amount)
        return cls(
            profile=profile,
            closed_balance=closed_balance,
            peak_equity=inferred_peak,
            threshold=threshold,
            session_date=today,
            day_start_balance=closed_balance,
            daily_pnl=dict(daily_pnl or {}),
        )

    # -- core arithmetic ---------------------------------------------------

    @property
    def equity(self) -> float:
        """Balance including open position P&L."""
        return self.closed_balance + self.open_pnl

    @property
    def room(self) -> float:
        """Dollars of loss available before the account breaches."""
        return self.equity - self.threshold

    @property
    def usable_room(self) -> float:
        """Room minus the safety buffer is what may actually be planned against."""
        return self.room

    @property
    def is_breached(self) -> bool:
        return self.equity <= self.threshold

    @property
    def threshold_is_locked(self) -> bool:
        lock = self.profile.threshold_lock
        return lock is not None and self.threshold >= lock - 1e-9

    @property
    def day_pnl(self) -> float:
        """Realised P&L for the current session."""
        return self.closed_balance - self.day_start_balance

    def _recompute_threshold(self) -> None:
        """Advance the threshold. It never moves down, and stops at the lock."""
        raw = self.peak_equity - self.profile.drawdown_amount
        lock = self.profile.threshold_lock
        if lock is not None:
            raw = min(raw, lock)
        self.threshold = max(self.threshold, raw)

    def mark(self, open_pnl: float) -> None:
        """Update open position P&L and let the threshold trail if applicable."""
        self.open_pnl = open_pnl
        if self.profile.trailing_mode is TrailingMode.INTRADAY:
            self.peak_equity = max(self.peak_equity, self.equity)
            self._recompute_threshold()

    def close_trade(self, realised_pnl: float) -> None:
        """Book a completed trade."""
        self.closed_balance += realised_pnl
        self.open_pnl = 0.0
        self.trades_today += 1
        if realised_pnl < 0:
            self.consecutive_losses += 1
        elif realised_pnl > 0:
            self.consecutive_losses = 0
        if self.session_date is not None:
            self.daily_pnl[self.session_date] = (
                self.daily_pnl.get(self.session_date, 0.0) + realised_pnl
            )
        if self.profile.trailing_mode is TrailingMode.INTRADAY:
            self.peak_equity = max(self.peak_equity, self.equity)
            self._recompute_threshold()

    def end_session(self, next_date: date | None = None) -> None:
        """Close the books on a session and roll counters."""
        if self.profile.trailing_mode is TrailingMode.END_OF_DAY:
            self.peak_equity = max(self.peak_equity, self.closed_balance)
            self._recompute_threshold()
        self.session_date = next_date
        self.day_start_balance = self.closed_balance
        self.trades_today = 0
        self.consecutive_losses = 0
        self.open_pnl = 0.0

    # -- forward-looking helpers ------------------------------------------

    def room_cost_of_running_profit(self, peak_open_profit: float) -> float:
        """Permanent room lost by letting an open trade peak at `peak_open_profit`.

        On an intraday account, unrealised profit ratchets the threshold up. If
        the trade then gives it all back, that room is gone for good. Returns 0
        for EOD and static accounts, where open profit does not move the
        threshold.
        """
        if self.profile.trailing_mode is not TrailingMode.INTRADAY:
            return 0.0
        if peak_open_profit <= 0:
            return 0.0
        hypothetical_peak = max(self.peak_equity, self.equity + peak_open_profit)
        raw = hypothetical_peak - self.profile.drawdown_amount
        lock = self.profile.threshold_lock
        if lock is not None:
            raw = min(raw, lock)
        new_threshold = max(self.threshold, raw)
        return max(0.0, new_threshold - self.threshold)

    def profit_to_lock_threshold(self) -> float | None:
        """Additional profit needed before the threshold stops trailing.

        Past this point the account can no longer be lost from a drawdown that
        follows you up, which is the real graduation moment on an Apex account.
        """
        lock = self.profile.threshold_lock
        if lock is None:
            return None
        if self.threshold_is_locked:
            return 0.0
        required_peak = lock + self.profile.drawdown_amount
        return max(0.0, required_peak - self.peak_equity)


@dataclass(frozen=True)
class Sizing:
    """Result of a position-size calculation."""

    quantity: int
    risk_dollars: float          # worst-case loss at the stop, incl. commission
    risk_per_contract: float
    risk_budget: float
    limiting_factor: str
    stop_points: float

    @property
    def is_tradeable(self) -> bool:
        return self.quantity > 0


class RiskEngine:
    """Enforces account rules and sizes positions.

    Deliberately has no opinion about market direction. It answers two
    questions: *may I trade at all right now*, and *how many contracts is the
    largest defensible size*.
    """

    def __init__(
        self,
        state: AccountState,
        limits: RiskLimits | None = None,
        news_times: list[datetime] | None = None,
    ) -> None:
        self.state = state
        self.limits = limits or RiskLimits()
        self.news_times = news_times or []

    # -- gates -------------------------------------------------------------

    def check(self, now: datetime, *, in_position: bool = False) -> list[Blocker]:
        """All reasons not to open a new position at `now`, worst first."""
        s, lim = self.state, self.limits
        blockers: list[Blocker] = []
        et_now = now.astimezone(ET)
        session = classify_session(now)

        if s.is_breached:
            blockers.append(
                Blocker(
                    "account_breached",
                    f"Account breached: equity ${s.equity:,.2f} is at or below "
                    f"threshold ${s.threshold:,.2f}.",
                )
            )

        if not is_market_open(now):
            blockers.append(
                Blocker("market_closed", f"Market closed ({session.value}).")
            )

        # Apex requires flat before the daily cutoff; stop opening well before it.
        flatten_at = datetime.combine(et_now.date(), s.profile.flatten_by, tzinfo=ET)
        minutes_to_flatten = (flatten_at - et_now).total_seconds() / 60.0
        if session.is_rth or session is Session.POST_CLOSE:
            if minutes_to_flatten <= 0:
                blockers.append(
                    Blocker(
                        "past_flatten",
                        f"Past the {s.profile.flatten_by:%H:%M} ET flatten deadline.",
                    )
                )
            elif minutes_to_flatten < 20 and not in_position:
                blockers.append(
                    Blocker(
                        "near_flatten",
                        f"Only {minutes_to_flatten:.0f} min to the flatten deadline; "
                        "not enough runway for a new position.",
                    )
                )

        if session is Session.CLOSING and not in_position:
            blockers.append(
                Blocker(
                    "closing_window",
                    "In the 15:50 ET close-out window; manage, do not initiate.",
                )
            )

        if not s.profile.allow_overnight and session in (
            Session.OVERNIGHT,
            Session.PRE_MARKET,
        ):
            blockers.append(
                Blocker(
                    "overnight_disabled",
                    "Overnight/pre-market trading is disabled for this profile.",
                    Severity.SOFT if in_position else Severity.HARD,
                )
            )

        if lim.block_lunch and session is Session.LUNCH:
            blockers.append(
                Blocker(
                    "lunch_chop",
                    "11:30-13:30 ET lunch session: thin liquidity, low follow-through.",
                    Severity.SOFT,
                )
            )

        if session is Session.OPENING_DRIVE:
            from .bars import minutes_into_session

            elapsed = minutes_into_session(now)
            if 0 <= elapsed < lim.block_first_minutes:
                blockers.append(
                    Blocker(
                        "opening_auction",
                        f"First {lim.block_first_minutes:.0f} min after the open; "
                        "spreads are wide and the range is undefined.",
                    )
                )

        for event in self.news_times:
            delta = abs((now - event).total_seconds()) / 60.0
            if delta <= lim.news_blackout_minutes:
                blockers.append(
                    Blocker(
                        "news_blackout",
                        f"Within {lim.news_blackout_minutes:.0f} min of a scheduled "
                        f"release ({event.astimezone(ET):%H:%M ET}).",
                    )
                )
                break

        if s.room < lim.min_room_to_trade:
            blockers.append(
                Blocker(
                    "insufficient_room",
                    f"Only ${s.room:,.2f} of drawdown room left, below the "
                    f"${lim.min_room_to_trade:,.2f} minimum.",
                )
            )

        if s.day_pnl <= -lim.daily_loss_limit:
            blockers.append(
                Blocker(
                    "daily_loss_limit",
                    f"Daily loss limit hit: ${s.day_pnl:,.2f} vs "
                    f"-${lim.daily_loss_limit:,.2f}. Done for the day.",
                )
            )

        if lim.daily_profit_lock is not None and s.day_pnl >= lim.daily_profit_lock:
            blockers.append(
                Blocker(
                    "daily_profit_lock",
                    f"Daily profit target banked (${s.day_pnl:,.2f}). "
                    "Protect it; stop trading.",
                )
            )

        if s.trades_today >= lim.max_trades_per_day:
            blockers.append(
                Blocker(
                    "trade_count",
                    f"{s.trades_today} trades already taken today "
                    f"(limit {lim.max_trades_per_day}).",
                )
            )

        if s.consecutive_losses >= lim.max_consecutive_losses:
            blockers.append(
                Blocker(
                    "loss_streak",
                    f"{s.consecutive_losses} losses in a row. Step away; you are "
                    "reading the market wrong today.",
                )
            )

        blockers.sort(key=lambda b: 0 if b.is_hard else 1)
        return blockers

    # -- sizing ------------------------------------------------------------

    def size_position(self, spec: ContractSpec, stop_points: float) -> Sizing:
        """Largest defensible position for a stop `stop_points` away.

        The budget is the minimum of every applicable constraint, so whichever
        rule binds first wins. Commission is included in per-contract risk: on
        micros with a tight stop it is a meaningful fraction of the loss.
        """
        s, lim = self.state, self.limits

        if stop_points <= 0:
            raise ValueError("stop_points must be positive")

        risk_per_contract = spec.points_to_dollars(stop_points) + spec.round_turn_cost

        # Every candidate budget, labelled so the caller can explain the cap.
        room_budget = max(0.0, s.room - lim.threshold_safety_buffer)
        daily_budget = max(0.0, lim.daily_loss_limit + min(0.0, s.day_pnl))
        candidates = [
            ("max_risk_per_trade", lim.max_risk_per_trade),
            ("pct_of_room", s.room * lim.max_risk_pct_of_room),
            ("threshold_buffer", room_budget),
            ("daily_loss_limit", daily_budget),
        ]
        limiting_factor, risk_budget = min(candidates, key=lambda kv: kv[1])

        quantity = int(math.floor(risk_budget / risk_per_contract)) if risk_per_contract > 0 else 0

        cap = s.profile.contract_cap(spec)
        if lim.max_contracts_override is not None:
            override = lim.max_contracts_override
            if is_micro(spec):
                override *= MICROS_PER_MINI
            cap = min(cap, override)
        if quantity > cap:
            quantity = cap
            limiting_factor = "contract_cap"

        quantity = max(0, quantity)
        return Sizing(
            quantity=quantity,
            risk_dollars=quantity * risk_per_contract,
            risk_per_contract=risk_per_contract,
            risk_budget=risk_budget,
            limiting_factor=limiting_factor,
            stop_points=stop_points,
        )

    def validate_stop(self, stop_points: float) -> Blocker | None:
        """Reject stops that are too tight to survive noise or too wide to be intraday."""
        lim = self.limits
        if stop_points < lim.min_stop_points:
            return Blocker(
                "stop_too_tight",
                f"Stop of {stop_points:.2f} pts is inside NQ's noise band "
                f"(minimum {lim.min_stop_points:.2f}).",
            )
        if stop_points > lim.max_stop_points:
            return Blocker(
                "stop_too_wide",
                f"Stop of {stop_points:.2f} pts exceeds the {lim.max_stop_points:.2f} "
                "pt intraday maximum; the setup is not defined tightly enough.",
            )
        return None


@dataclass(frozen=True)
class ConsistencyStatus:
    """Where you stand against the payout consistency rule."""

    best_day: float
    total_profit: float
    required_total: float | None
    compliant: bool
    max_additional_today: float | None
    message: str


def consistency_status(
    state: AccountState, today_profit: float | None = None
) -> ConsistencyStatus:
    """Evaluate the 30%-style consistency rule for payout eligibility.

    The rule caps any single day at a fraction of total profit at payout time.
    Its practical bite is counter-intuitive: a huge green day does not
    disqualify you, it raises the *total* profit you must accumulate before you
    can withdraw. This reports both numbers and, when a day is running hot, how
    much more can be banked today without pushing the bar higher.
    """
    pct = state.profile.consistency_pct
    realised_today = state.day_pnl if today_profit is None else today_profit

    days = dict(state.daily_pnl)
    if state.session_date is not None:
        days[state.session_date] = realised_today

    total_profit = state.closed_balance - state.profile.starting_balance
    positive_days = [v for v in days.values() if v > 0]
    best_day = max(positive_days) if positive_days else 0.0

    if pct is None:
        return ConsistencyStatus(
            best_day, total_profit, None, True, None,
            "No consistency rule configured for this profile.",
        )

    required_total = best_day / pct if best_day > 0 else 0.0
    compliant = total_profit >= required_total

    # Banking more today only hurts while today *is* the best day and the
    # account is not already over the required total.
    max_additional: float | None = None
    if realised_today > 0:
        # Today may bank up to x more while keeping (today+x) <= pct*(total+x).
        headroom = (pct * total_profit - realised_today) / (1 - pct) if pct < 1 else None
        max_additional = max(0.0, headroom) if headroom is not None else None

    if compliant:
        message = (
            f"Consistency OK: best day ${best_day:,.2f} is "
            f"{(best_day / total_profit * 100) if total_profit > 0 else 0:.0f}% of "
            f"${total_profit:,.2f} total."
        )
    else:
        shortfall = required_total - total_profit
        message = (
            f"Consistency: best day ${best_day:,.2f} requires ${required_total:,.2f} "
            f"total profit before payout; ${shortfall:,.2f} more needed."
        )

    return ConsistencyStatus(
        best_day=best_day,
        total_profit=total_profit,
        required_total=required_total,
        compliant=compliant,
        max_additional_today=max_additional,
        message=message,
    )
