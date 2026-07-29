"""Price bars, trading-session arithmetic, and the CME/Apex clock.

Everything in this package is timezone-aware and anchored to America/New_York,
because every rule that matters (RTH open, Apex's 4:59pm flatten, the 5-6pm
maintenance halt, economic releases) is defined in Eastern time and shifts with
US daylight saving.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Bar OHLC consistency is checked with a small tolerance: real feeds occasionally
# emit a close a hair outside the high/low from rounding on aggregation.
_OHLC_EPS = 1e-6


@dataclass(frozen=True)
class Bar:
    """A single OHLCV price bar.

    `ts` is the bar's OPEN time and must be timezone-aware. Using open time
    consistently matters: a 5-minute bar stamped 09:30 covers 09:30:00-09:34:59,
    so it is complete and tradeable at 09:35.
    """

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError(f"bar timestamp must be timezone-aware: {self.ts!r}")
        if self.high < self.low - _OHLC_EPS:
            raise ValueError(f"bar high {self.high} below low {self.low} at {self.ts}")
        top = max(self.open, self.close)
        bottom = min(self.open, self.close)
        if self.high < top - _OHLC_EPS:
            raise ValueError(f"bar high {self.high} below body top {top} at {self.ts}")
        if self.low > bottom + _OHLC_EPS:
            raise ValueError(f"bar low {self.low} above body bottom {bottom} at {self.ts}")
        if self.volume < 0:
            raise ValueError(f"negative volume {self.volume} at {self.ts}")

    @property
    def et(self) -> datetime:
        """Timestamp converted to Eastern time."""
        return self.ts.astimezone(ET)

    @property
    def hlc3(self) -> float:
        """Typical price. Used as the VWAP price input, matching TradingView."""
        return (self.high + self.low + self.close) / 3.0

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_up(self) -> bool:
        return self.close > self.open

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    def close_position(self) -> float:
        """Where the close sits within the bar range, 0.0 (low) to 1.0 (high).

        Returns 0.5 for a zero-range bar.
        """
        if self.range <= 0:
            return 0.5
        return (self.close - self.low) / self.range


class Session(Enum):
    """Named intraday windows, in Eastern time.

    The boundaries encode how NQ actually behaves through the day, and the
    playbook weights setups differently in each one.
    """

    OVERNIGHT = "overnight"          # 18:00 - 09:30, Globex
    PRE_MARKET = "pre_market"        # 08:00 - 09:30, cash-open positioning
    OPENING_DRIVE = "opening_drive"  # 09:30 - 10:00, highest volatility
    MORNING_TREND = "morning_trend"  # 10:00 - 11:30, best trend continuation
    LUNCH = "lunch"                  # 11:30 - 13:30, liquidity trough
    AFTERNOON = "afternoon"          # 13:30 - 15:00, trend resumption
    POWER_HOUR = "power_hour"        # 15:00 - 15:50
    CLOSING = "closing"              # 15:50 - 16:00, flatten window
    POST_CLOSE = "post_close"        # 16:00 - 17:00
    MAINTENANCE = "maintenance"      # 17:00 - 18:00, CME halt

    @property
    def is_rth(self) -> bool:
        return self in _RTH_SESSIONS


_RTH_SESSIONS = frozenset(
    {
        Session.OPENING_DRIVE,
        Session.MORNING_TREND,
        Session.LUNCH,
        Session.AFTERNOON,
        Session.POWER_HOUR,
        Session.CLOSING,
    }
)

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)

# CME equity index futures halt 17:00-18:00 ET; Apex additionally requires flat
# positions by 16:59 ET. Both are enforced as hard gates in the risk layer.
CME_HALT_START = time(17, 0)
CME_HALT_END = time(18, 0)
APEX_FLATTEN_BY = time(16, 59)

_BOUNDARIES: tuple[tuple[time, Session], ...] = (
    (time(0, 0), Session.OVERNIGHT),
    (time(8, 0), Session.PRE_MARKET),
    (time(9, 30), Session.OPENING_DRIVE),
    (time(10, 0), Session.MORNING_TREND),
    (time(11, 30), Session.LUNCH),
    (time(13, 30), Session.AFTERNOON),
    (time(15, 0), Session.POWER_HOUR),
    (time(15, 50), Session.CLOSING),
    (time(16, 0), Session.POST_CLOSE),
    (time(17, 0), Session.MAINTENANCE),
    (time(18, 0), Session.OVERNIGHT),
)


def classify_session(ts: datetime) -> Session:
    """Return the named session window containing `ts`."""
    t = ts.astimezone(ET).time()
    current = Session.OVERNIGHT
    for start, session in _BOUNDARIES:
        if t >= start:
            current = session
        else:
            break
    return current


def is_weekend(ts: datetime) -> bool:
    """True when the market is closed for the weekend.

    The CME week runs Sunday 18:00 ET to Friday 17:00 ET.
    """
    et = ts.astimezone(ET)
    weekday = et.weekday()  # Monday == 0
    if weekday == 5:  # Saturday
        return True
    if weekday == 6:  # Sunday, closed until the 18:00 reopen
        return et.time() < CME_HALT_END
    if weekday == 4 and et.time() >= CME_HALT_START:  # Friday after the close
        return True
    return False


def is_market_open(ts: datetime) -> bool:
    """True when NQ is tradeable: not the weekend and not the daily halt."""
    if is_weekend(ts):
        return False
    return classify_session(ts) is not Session.MAINTENANCE


def trading_date(ts: datetime) -> date:
    """The trading day a timestamp belongs to.

    The Globex session opening Sunday 18:00 ET belongs to Monday's trading date,
    so anything at or after 18:00 rolls to the next weekday. This is the anchor
    for session VWAP and for daily P&L accounting.
    """
    et = ts.astimezone(ET)
    d = et.date()
    if et.time() >= CME_HALT_END:
        d = d + timedelta(days=1)
    # Roll a Saturday result (from Friday evening) forward to Monday.
    while d.weekday() >= 5:
        d = d + timedelta(days=1)
    return d


def et_datetime(d: date, t: time) -> datetime:
    """Build a timezone-aware Eastern datetime."""
    return datetime.combine(d, t, tzinfo=ET)


def minutes_into_session(ts: datetime, session_start: time = RTH_OPEN) -> float:
    """Minutes elapsed since `session_start` on the same calendar day.

    Negative before the start. Used to gate setups that are only valid a certain
    distance into the cash session.
    """
    et = ts.astimezone(ET)
    start = datetime.combine(et.date(), session_start, tzinfo=ET)
    return (et - start).total_seconds() / 60.0


def minutes_until(ts: datetime, target: time) -> float:
    """Minutes from `ts` until `target` on the same calendar day."""
    et = ts.astimezone(ET)
    end = datetime.combine(et.date(), target, tzinfo=ET)
    return (end - et).total_seconds() / 60.0


def bars_in_window(
    bars: list[Bar], start: time, end: time, on_date: date | None = None
) -> list[Bar]:
    """Select bars whose open time falls in [start, end) on a trading date."""
    out = []
    for bar in bars:
        et = bar.et
        if on_date is not None and trading_date(bar.ts) != on_date:
            continue
        if start <= et.time() < end:
            out.append(bar)
    return out


def validate_series(bars: list[Bar], *, max_gap_minutes: float | None = None) -> None:
    """Reject a bar series that would produce silently wrong indicator values.

    Checks strict chronological ordering and, optionally, that no gap exceeds
    `max_gap_minutes` (a gap means missing data, which corrupts every rolling
    average computed across it). Overnight and weekend gaps are exempt.
    """
    if not bars:
        raise ValueError("empty bar series")
    for i in range(1, len(bars)):
        prev, cur = bars[i - 1], bars[i]
        if cur.ts <= prev.ts:
            raise ValueError(
                f"bars out of order or duplicated at index {i}: {prev.ts} -> {cur.ts}"
            )
        if max_gap_minutes is None:
            continue
        gap = (cur.ts - prev.ts).total_seconds() / 60.0
        if gap <= max_gap_minutes:
            continue
        # A gap spanning the halt or the weekend is expected, not corruption.
        if is_weekend(cur.ts) or trading_date(prev.ts) != trading_date(cur.ts):
            continue
        if classify_session(prev.ts) is Session.MAINTENANCE:
            continue
        raise ValueError(
            f"data gap of {gap:.0f} minutes at index {i} ({prev.ts} -> {cur.ts}); "
            "indicators computed across a gap are not trustworthy"
        )
