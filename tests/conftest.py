"""Shared test fixtures and bar-construction helpers."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nqcopilot.apex import APEX_50K_INTRADAY, AccountState
from nqcopilot.bars import ET, Bar


def make_bar(
    ts: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1000.0,
) -> Bar:
    return Bar(ts=ts, open=open_, high=high, low=low, close=close, volume=volume)


def series_from_closes(
    closes: list[float],
    start: datetime | None = None,
    interval_minutes: int = 5,
    spread: float = 2.0,
    volume: float = 1000.0,
) -> list[Bar]:
    """Build a bar series from closes, with a symmetric high/low around each.

    Opens chain from the previous close so the series behaves like a real one.
    """
    origin = start or datetime(2026, 7, 27, 9, 30, tzinfo=ET)
    bars: list[Bar] = []
    prev_close = closes[0]
    for i, close in enumerate(closes):
        open_ = prev_close
        high = max(open_, close) + spread
        low = min(open_, close) - spread
        bars.append(
            Bar(
                ts=origin + timedelta(minutes=interval_minutes * i),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
        prev_close = close
    return bars


@pytest.fixture
def fresh_state() -> AccountState:
    """A brand-new Apex $50K intraday account."""
    return AccountState.fresh(APEX_50K_INTRADAY, today=datetime(2026, 7, 27).date())


@pytest.fixture
def rth_open() -> datetime:
    """A Monday at 10:15 ET — inside the morning trend window."""
    return datetime(2026, 7, 27, 10, 15, tzinfo=ET)
