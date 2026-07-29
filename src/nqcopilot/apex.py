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
from collections.abc import Sequence
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
class ScalingTier:
    """One rung of a Performance Account's scaling ladder."""

    min_balance: float       # end-of-day balance at or above this
    max_contracts: int       # in minis
    daily_loss_limit: float


@dataclass(frozen=True)
class ScalingLadder:
    """Contract and daily-loss limits that move with the account balance.

    Two properties of Apex's ladder matter and are modelled here:

    * The tier is set from the **end-of-day** balance and applies to the *next*
      session. Being up intraday does not raise today's contract allowance, and
      assuming otherwise is a rule breach rather than an optimistic estimate.
    * It moves **down** as well as up. A losing day can cut both your size and
      your daily loss limit for the following session.

    Thresholds are not bundled as a preset because they vary by account size and
    change over time. Supply your own from your dashboard; `--tiers` loads them
    from JSON.
    """

    tiers: tuple[ScalingTier, ...]

    def __post_init__(self) -> None:
        if not self.tiers:
            raise ValueError("a ladder needs at least one tier")
        ordered = sorted(self.tiers, key=lambda t: t.min_balance)
        object.__setattr__(self, "tiers", tuple(ordered))

    @classmethod
    def from_rows(cls, rows: Sequence[dict]) -> "ScalingLadder":
        """Build from `[{"balance": …, "contracts": …, "daily_loss": …}, …]`."""
        tiers = []
        for row in rows:
            try:
                tiers.append(
                    ScalingTier(
                        min_balance=float(row["balance"]),
                        max_contracts=int(row["contracts"]),
                        daily_loss_limit=float(row["daily_loss"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"bad scaling tier {row!r}: {exc}") from exc
        return cls(tuple(tiers))

    def for_balance(self, balance: float) -> ScalingTier:
        """The tier a given end-of-day balance qualifies for.

        Below the lowest rung the lowest rung still applies: an account under
        its opening balance is not exempt from limits, it is simply at the
        bottom of the ladder.
        """
        current = self.tiers[0]
        for tier in self.tiers:
            if balance >= tier.min_balance:
                current = tier
            else:
                break
        return current


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
    consistency_pct: float | None = None  # e.g. 0.50 for the 50% rule
    flatten_by: time = APEX_FLATTEN_BY
    allow_overnight: bool = False
    verify_note: str = ""

    # Firm-imposed daily loss limit. Apex enforces none during evaluation, but
    # Performance Accounts carry a tier-based one. Set it from your tier: this
    # is a hard rule that can end the account, unlike the self-imposed limit in
    # RiskLimits which merely protects you.
    firm_daily_loss_limit: float | None = None

    # Optional scaling ladder. When present it supersedes max_contracts and
    # firm_daily_loss_limit, derived from the end-of-day balance.
    scaling: "ScalingLadder | None" = None

    # Payout rules. Zero/None disables the corresponding check.
    payout_min_days: int = 0            # qualifying trading days required
    payout_min_daily_profit: float = 0.0  # what makes a day "qualifying"
    payout_minimum: float = 0.0         # smallest withdrawal allowed
    payout_lifetime_cap: int | None = None  # total payouts per account

    @property
    def initial_threshold(self) -> float:
        return self.starting_balance - self.drawdown_amount

    @property
    def safety_net(self) -> float | None:
        """Balance above which profits become withdrawable.

        Apex sets this at the level where the threshold stops trailing, so it
        coincides with `threshold_lock`.
        """
        return self.threshold_lock

    def contract_cap(self, spec: ContractSpec, tier_balance: float | None = None) -> int:
        """Maximum quantity of `spec` this account may hold.

        `tier_balance` is the end-of-day balance that set the current tier, not
        the live balance — intraday profit does not raise today's allowance.
        """
        minis = self.max_contracts
        if self.scaling is not None and tier_balance is not None:
            minis = self.scaling.for_balance(tier_balance).max_contracts
        if is_micro(spec):
            return minis * MICROS_PER_MINI
        return minis

    def daily_loss_limit_for(self, tier_balance: float | None = None) -> float | None:
        """The firm's daily loss limit, from the ladder when one is configured."""
        if self.scaling is not None and tier_balance is not None:
            return self.scaling.for_balance(tier_balance).daily_loss_limit
        return self.firm_daily_loss_limit


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------
# Parameters below follow Apex's published intraday trailing-drawdown rules:
#
#   Evaluation — $3,000 target, $2,000 real-time intraday trailing drawdown
#   following peak balance including open profit, no daily loss limit, no
#   minimum trading days.
#
#   Performance Account — same real-time intraday tracking, threshold stops
#   trailing permanently at starting balance + $100 ($50,100 on a 50K), which
#   is also the safety net above which profits are withdrawable. Payouts need
#   5 qualifying trading days, obey a 50% consistency rule since the last
#   payout, have a $500 minimum, and are capped at 6 per account.
#
# Contract and daily-loss limits on a PA are tier-based and are NOT encoded
# here, because they depend on where you are in the scaling ladder. Set
# `max_contracts` and `firm_daily_loss_limit` from your own tier.

APEX_50K_PA = AccountProfile(
    name="Apex $50K PA (intraday trailing)",
    starting_balance=50_000.0,
    drawdown_amount=2_000.0,
    trailing_mode=TrailingMode.INTRADAY,
    max_contracts=10,
    threshold_lock=50_100.0,          # starting balance + $100
    profit_target=3_000.0,
    consistency_pct=0.50,             # no day may be 50% or more of the total
    payout_min_days=5,
    payout_min_daily_profit=50.0,
    payout_minimum=500.0,
    payout_lifetime_cap=6,
    verify_note=(
        "Drawdown $2,000 (threshold starts $48,000, locks at $50,100 = start + $100). "
        "Contract cap and daily loss limit are TIER-BASED and are NOT set here — "
        "supply your ladder with --tiers, or a single tier with --max-contracts and "
        "--firm-daily-loss. Confirm the $50 minimum qualifying day for your size."
    ),
)

APEX_50K_EVAL = AccountProfile(
    name="Apex $50K Evaluation (intraday trailing)",
    starting_balance=50_000.0,
    drawdown_amount=2_000.0,
    trailing_mode=TrailingMode.INTRADAY,
    max_contracts=10,
    # Evaluation locks at "starting balance plus an offset" whose value differs
    # by platform. Left unset deliberately: an unset lock means the threshold is
    # assumed to trail forever, which understates your room rather than
    # overstating it. Set it once you have confirmed the number.
    threshold_lock=None,
    profit_target=3_000.0,
    consistency_pct=None,     # no consistency requirement during evaluation
    firm_daily_loss_limit=None,  # none enforced during evaluation
    verify_note=(
        "Evaluation: $3,000 target, $2,000 intraday trailing drawdown, no daily "
        "loss limit, no minimum trading days. Threshold lock left unset (assumes "
        "it never stops trailing) — conservative until you confirm the offset."
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
    consistency_pct=0.50,
    payout_min_days=5,
    payout_min_daily_profit=50.0,
    payout_minimum=500.0,
    payout_lifetime_cap=6,
    verify_note="Confirm against your dashboard: EOD trailing, $2,000 drawdown.",
)

PRESETS: dict[str, AccountProfile] = {
    "apex50k-pa": APEX_50K_PA,
    "apex50k-eval": APEX_50K_EVAL,
    "apex50k-eod": APEX_50K_EOD,
    # Retained so existing state files and scripts keep working.
    "apex50k-intraday": APEX_50K_PA,
}

# Backwards-compatible alias.
APEX_50K_INTRADAY = APEX_50K_PA


class RiskStyle(Enum):
    """How much of the drawdown allowance a session may consume."""

    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


# Fractions of the account's *drawdown allowance*, not of its balance. A $50K
# account with $2,000 of drawdown can survive far less than the balance implies,
# and limits set against the balance flatter that difference away.
#
# The number that matters is how many losing days a style can absorb before the
# account is gone. At CONSERVATIVE that is roughly six full stop-out days; at
# AGGRESSIVE it is closer to three, which on a trailing threshold that also
# ratchets against you is a thin margin.
_STYLE_FRACTIONS: dict[RiskStyle, dict[str, float]] = {
    RiskStyle.CONSERVATIVE: {
        "per_trade": 0.05, "pct_of_room": 0.05, "daily_loss": 0.15,
        "profit_lock": 0.20, "min_room": 0.25, "buffer": 0.10,
    },
    RiskStyle.BALANCED: {
        "per_trade": 0.08, "pct_of_room": 0.08, "daily_loss": 0.25,
        "profit_lock": 0.30, "min_room": 0.20, "buffer": 0.08,
    },
    RiskStyle.AGGRESSIVE: {
        "per_trade": 0.125, "pct_of_room": 0.12, "daily_loss": 0.35,
        "profit_lock": 0.45, "min_room": 0.15, "buffer": 0.05,
    },
}

_STYLE_COUNTS: dict[RiskStyle, tuple[int, int]] = {
    # (max trades per day, max consecutive losses)
    RiskStyle.CONSERVATIVE: (3, 2),
    RiskStyle.BALANCED: (4, 3),
    RiskStyle.AGGRESSIVE: (6, 3),
}


@dataclass(frozen=True)
class RiskLimits:
    """Self-imposed limits. Apex sets almost none of these; survival requires them.

    Apex enforces no daily loss limit during evaluation and, on many accounts,
    none at all. That is precisely why one is essential: without it a single bad
    session can consume a threshold that took weeks to build.

    The defaults here are calibrated for a $2,000 drawdown. Prefer
    `RiskLimits.for_drawdown`, which scales every dollar figure to the account's
    actual allowance — a fixed $600 daily cap is prudent on a $10,000 buffer and
    reckless on a $2,000 one, and only the ratio makes that visible.
    """

    max_risk_per_trade: float = 100.0
    max_risk_pct_of_room: float = 0.05   # never stake >5% of remaining room
    daily_loss_limit: float = 300.0
    daily_profit_lock: float | None = 400.0  # bank the day after this much
    max_trades_per_day: int = 3
    max_consecutive_losses: int = 2
    min_room_to_trade: float = 500.0
    threshold_safety_buffer: float = 200.0   # never plan into the threshold itself
    max_contracts_override: int | None = None
    min_stop_points: float = 8.0    # NQ noise floor; tighter stops are coin flips
    max_stop_points: float = 60.0   # beyond this the setup is not intraday
    block_lunch: bool = True
    block_first_minutes: float = 5.0    # let the opening auction settle
    news_blackout_minutes: float = 15.0

    @classmethod
    def for_drawdown(
        cls,
        drawdown: float,
        style: RiskStyle = RiskStyle.CONSERVATIVE,
        **overrides,
    ) -> "RiskLimits":
        """Scale every dollar limit to the account's drawdown allowance.

        Args:
            drawdown: The account's total drawdown allowance (e.g. $2,000).
            style: How much of it a session may consume.
            overrides: Any field to pin explicitly, bypassing the calculation.
        """
        if drawdown <= 0:
            raise ValueError("drawdown must be positive")
        fractions = _STYLE_FRACTIONS[style]
        trades, streak = _STYLE_COUNTS[style]

        derived = {
            "max_risk_per_trade": round(drawdown * fractions["per_trade"], 2),
            "max_risk_pct_of_room": fractions["pct_of_room"],
            "daily_loss_limit": round(drawdown * fractions["daily_loss"], 2),
            "daily_profit_lock": round(drawdown * fractions["profit_lock"], 2),
            "min_room_to_trade": round(drawdown * fractions["min_room"], 2),
            "threshold_safety_buffer": round(drawdown * fractions["buffer"], 2),
            "max_trades_per_day": trades,
            "max_consecutive_losses": streak,
        }
        derived.update(overrides)
        return cls(**derived)

    def days_to_breach(self, drawdown: float) -> float:
        """Losing days at the full daily limit before the account is gone.

        The single most useful sanity check on a risk setting, and the one most
        traders never compute. Below about four, one bad week ends the account.
        """
        if self.daily_loss_limit <= 0:
            return float("inf")
        return drawdown / self.daily_loss_limit


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
    # Payout history. The consistency rule and the qualifying-day count are both
    # measured since the last payout, so this window matters.
    last_payout_date: date | None = None
    payouts_taken: int = 0
    # End-of-day balance that set today's tier. Deliberately NOT the live
    # balance: intraday profit does not raise today's contract allowance.
    tier_reference_balance: float | None = None

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
            tier_reference_balance=profile.starting_balance,
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
            tier_reference_balance=closed_balance,
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
        # The tier for the next session is set by this session's closing
        # balance — including downward moves after a losing day.
        self.tier_reference_balance = self.closed_balance
        self.session_date = next_date
        self.day_start_balance = self.closed_balance
        self.trades_today = 0
        self.consecutive_losses = 0
        self.open_pnl = 0.0

    @property
    def current_tier(self) -> "ScalingTier | None":
        """Today's tier, from the balance at the end of the previous session."""
        ladder = self.profile.scaling
        if ladder is None or self.tier_reference_balance is None:
            return None
        return ladder.for_balance(self.tier_reference_balance)

    def contract_cap(self, spec: ContractSpec) -> int:
        return self.profile.contract_cap(spec, self.tier_reference_balance)

    def firm_daily_loss_limit(self) -> float | None:
        return self.profile.daily_loss_limit_for(self.tier_reference_balance)

    def record_payout(self, amount: float, on_date: date | None = None) -> None:
        """Book a withdrawal, resetting the consistency and qualifying-day window.

        The balance drops by the amount withdrawn. The threshold does not move:
        it only ever follows peak equity upward, so a withdrawal reduces your
        room by exactly what you took out.
        """
        if amount <= 0:
            raise ValueError("payout amount must be positive")
        self.closed_balance -= amount
        self.payouts_taken += 1
        self.last_payout_date = on_date or self.session_date
        self.day_start_balance = self.closed_balance

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

        # The firm's own daily loss limit ends the account, unlike the
        # self-imposed one below which merely ends the day. Checked first and
        # reported distinctly so the difference is never ambiguous.
        firm_limit = s.firm_daily_loss_limit()
        if firm_limit is not None and s.day_pnl <= -firm_limit:
            blockers.append(
                Blocker(
                    "firm_daily_loss_limit",
                    f"FIRM daily loss limit breached: ${s.day_pnl:,.2f} vs "
                    f"-${firm_limit:,.2f}. This is an account rule, not a preference.",
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
        firm_limit = s.firm_daily_loss_limit()
        if firm_limit is not None:
            candidates.append(
                ("firm_daily_loss_limit", max(0.0, firm_limit + min(0.0, s.day_pnl)))
            )
        limiting_factor, risk_budget = min(candidates, key=lambda kv: kv[1])

        quantity = int(math.floor(risk_budget / risk_per_contract)) if risk_per_contract > 0 else 0

        cap = s.contract_cap(spec)
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


def check_threshold_consistency(
    profile: AccountProfile, closed_balance: float, threshold: float
) -> str | None:
    """Detect a threshold that contradicts the profile's drawdown amount.

    The threshold can never sit further below the balance than the drawdown
    allows, so a lower one means the numbers came from a different account type
    than the selected profile. That is worth shouting about: a profile with too
    large a drawdown would report room the account does not have.
    """
    implied_minimum = closed_balance - profile.drawdown_amount
    if profile.threshold_lock is not None:
        implied_minimum = min(implied_minimum, profile.threshold_lock)

    if threshold < implied_minimum - 0.01:
        return (
            f"threshold ${threshold:,.2f} sits ${implied_minimum - threshold:,.2f} below "
            f"the lowest value a ${profile.drawdown_amount:,.0f} drawdown allows at a "
            f"${closed_balance:,.2f} balance (${implied_minimum:,.2f}). Either the "
            f"balance/threshold pair is stale, or '{profile.name}' is the wrong profile "
            f"for this account. The engine will use ${implied_minimum:,.2f}."
        )
    return None


@dataclass(frozen=True)
class PayoutStatus:
    """Where you stand against every payout requirement."""

    # Consistency rule
    best_day: float
    total_profit: float          # accumulated since the last payout
    required_total: float | None
    compliant: bool
    max_additional_today: float | None
    # Qualifying-days rule
    qualifying_days: int
    required_days: int
    days_ok: bool
    # Amount rules
    withdrawable: float
    minimum_payout: float
    amount_ok: bool
    # Lifetime cap
    payouts_taken: int
    lifetime_cap: int | None
    cap_ok: bool
    # Overall
    eligible: bool
    blockers: list[str]
    message: str


def payout_status(state: AccountState, today_profit: float | None = None) -> PayoutStatus:
    """Evaluate every payout requirement at once.

    Four independent gates, all of which must pass:

    1. **Consistency.** No single day may be `consistency_pct` *or more* of the
       total profit since your last payout. Counter-intuitively, a huge green
       day does not disqualify you — it raises the total you must accumulate
       before you can withdraw.
    2. **Qualifying days.** A minimum number of separate trading days each
       clearing a minimum profit.
    3. **Amount.** Only profit above the safety net is withdrawable, and the
       request must clear the minimum.
    4. **Lifetime cap.** A finite number of payouts per account.

    Everything is measured *since the last payout*, which is what the rule
    actually says — so accuracy depends on booking trades with `record_trade`
    and payouts with `record_payout`.
    """
    profile = state.profile
    realised_today = state.day_pnl if today_profit is None else today_profit

    # Only days after the last payout count toward consistency and day counts.
    days = {
        day: value
        for day, value in state.daily_pnl.items()
        if state.last_payout_date is None or day > state.last_payout_date
    }
    if state.session_date is not None and (
        state.last_payout_date is None or state.session_date > state.last_payout_date
    ):
        if today_profit is not None:
            days[state.session_date] = today_profit
        else:
            # `close_trade` already records the session's P&L, and `day_pnl` is
            # derived from `day_start_balance`, which `end_session` resets. Only
            # fall back to it when nothing has been recorded for today, or a
            # mid-day session reset would erase the day from the payout history.
            days.setdefault(state.session_date, realised_today)

    if state.last_payout_date is None:
        total_profit = state.closed_balance - profile.starting_balance
    else:
        total_profit = sum(days.values())

    positive = [v for v in days.values() if v > 0]
    best_day = max(positive) if positive else 0.0

    blockers: list[str] = []

    # --- 1. consistency --------------------------------------------------
    pct = profile.consistency_pct
    if pct is None:
        required_total: float | None = None
        compliant = True
        max_additional: float | None = None
    else:
        required_total = best_day / pct if best_day > 0 else 0.0
        # The rule fails at "pct or more", so compliance is a strict inequality.
        compliant = best_day <= 0 or best_day < pct * total_profit
        max_additional = None
        if realised_today > 0 and pct < 1:
            # Bank at most x more while keeping today + x < pct * (total + x).
            max_additional = max(0.0, (pct * total_profit - realised_today) / (1 - pct))
        if not compliant:
            blockers.append(
                f"best day ${best_day:,.2f} needs ${required_total:,.2f} total "
                f"(${required_total - total_profit:,.2f} more)"
            )

    # --- 2. qualifying days ----------------------------------------------
    required_days = profile.payout_min_days
    qualifying_days = sum(
        1 for value in days.values() if value >= profile.payout_min_daily_profit and value > 0
    )
    days_ok = qualifying_days >= required_days
    if not days_ok:
        blockers.append(
            f"{qualifying_days}/{required_days} qualifying days "
            f"(≥${profile.payout_min_daily_profit:,.0f} profit each)"
        )

    # --- 3. amount --------------------------------------------------------
    safety_net = profile.safety_net
    withdrawable = (
        max(0.0, state.closed_balance - safety_net) if safety_net is not None else total_profit
    )
    minimum = profile.payout_minimum
    amount_ok = withdrawable >= minimum if minimum > 0 else withdrawable > 0
    if not amount_ok:
        blockers.append(
            f"${withdrawable:,.2f} above the safety net, need ${minimum:,.2f}"
        )

    # --- 4. lifetime cap --------------------------------------------------
    cap = profile.payout_lifetime_cap
    cap_ok = cap is None or state.payouts_taken < cap
    if not cap_ok:
        blockers.append(f"all {cap} lifetime payouts already taken")

    eligible = compliant and days_ok and amount_ok and cap_ok

    if pct is None and required_days == 0:
        message = "No payout requirements configured for this profile."
    elif eligible:
        message = (
            f"Payout eligible: ${withdrawable:,.2f} withdrawable, "
            f"{qualifying_days} qualifying days, best day "
            f"{(best_day / total_profit * 100) if total_profit > 0 else 0:.0f}% of total."
        )
    else:
        message = "Payout blocked — " + "; ".join(blockers) + "."

    return PayoutStatus(
        best_day=best_day,
        total_profit=total_profit,
        required_total=required_total,
        compliant=compliant,
        max_additional_today=max_additional,
        qualifying_days=qualifying_days,
        required_days=required_days,
        days_ok=days_ok,
        withdrawable=withdrawable,
        minimum_payout=minimum,
        amount_ok=amount_ok,
        payouts_taken=state.payouts_taken,
        lifetime_cap=cap,
        cap_ok=cap_ok,
        eligible=eligible,
        blockers=blockers,
        message=message,
    )


# Retained name for callers that only care about the consistency portion.
consistency_status = payout_status
ConsistencyStatus = PayoutStatus
