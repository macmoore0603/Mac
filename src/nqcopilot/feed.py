"""Turn a stream of quotes into bars the engine can read.

The engine consumes `list[Bar]`. Plenty of sources give you a *price* instead —
a broker API, a websocket, a polled JSON endpoint, a scraped quote. This module
is the adapter between the two: feed it ticks, get completed bars.

Deliberately source-agnostic. `HttpQuoteSource` covers "some URL returns JSON
containing a price", which fits most broker REST APIs and most quote endpoints,
and anything else just needs to call `BarAggregator.add_tick`.

Two things worth knowing before relying on this:

* **A quote is not a bar.** Bars built from polled quotes only approximate real
  OHLCV — you see the price at each poll, not every trade, so highs and lows are
  understated and volume is usually unavailable. Fine for tracking the current
  session; not equivalent to exchange bars, and not what you want under a
  backtest.
* **Warmup is real.** The engine needs ~80 bars. Built from scratch on a
  5-minute interval that is nearly seven hours. Seed with `--csv` history and
  let the feed extend it, rather than starting cold.

Prefer a licensed feed from the broker you already have over scraping a public
page: it is real-time rather than delayed, it will not break when a page
changes, and it does not put you on the wrong side of anyone's terms.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .bars import ET, Bar


class FeedError(RuntimeError):
    """Raised when a quote cannot be fetched or understood."""


@dataclass
class Tick:
    """A single observed price."""

    ts: datetime
    price: float
    volume: float = 0.0


@dataclass
class BarAggregator:
    """Accumulate ticks into fixed-interval OHLCV bars.

    Bars are emitted only once their interval has fully elapsed, so a completed
    bar is never revised afterwards. That matters because the engine decides on
    closed bars: handing it a bar that can still change would reintroduce
    exactly the lookahead the indicator layer is careful to avoid.
    """

    interval_minutes: int = 5
    _open: float | None = field(default=None, init=False)
    _high: float | None = field(default=None, init=False)
    _low: float | None = field(default=None, init=False)
    _close: float | None = field(default=None, init=False)
    _volume: float = field(default=0.0, init=False)
    _bucket_start: datetime | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")

    def bucket_for(self, ts: datetime) -> datetime:
        """Start of the interval containing `ts`, on wall-clock boundaries."""
        et = ts.astimezone(ET)
        floored = et.minute - (et.minute % self.interval_minutes)
        return et.replace(minute=floored, second=0, microsecond=0)

    def add_tick(self, tick: Tick) -> Bar | None:
        """Add a tick. Returns a completed Bar when one closes, else None."""
        bucket = self.bucket_for(tick.ts)
        completed: Bar | None = None

        if self._bucket_start is None:
            self._bucket_start = bucket
        elif bucket > self._bucket_start:
            completed = self._flush()
            self._bucket_start = bucket
        elif bucket < self._bucket_start:
            # A tick older than the bar being built would corrupt it.
            return None

        if self._open is None:
            self._open = self._high = self._low = tick.price
        else:
            self._high = max(self._high, tick.price)
            self._low = min(self._low, tick.price)
        self._close = tick.price
        self._volume += tick.volume
        return completed

    def _flush(self) -> Bar | None:
        if self._open is None or self._bucket_start is None:
            return None
        bar = Bar(
            ts=self._bucket_start,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._volume,
        )
        self._open = self._high = self._low = self._close = None
        self._volume = 0.0
        return bar

    def force_close(self) -> Bar | None:
        """Close the in-progress bar early. Use only when the session ends."""
        bar = self._flush()
        self._bucket_start = None
        return bar

    @property
    def pending_close(self) -> float | None:
        """Latest price in the bar being built, for real-time marking."""
        return self._close


def extract_path(payload: object, path: str) -> object:
    """Read a dotted path out of nested JSON, e.g. `data.quote.last`.

    Numeric segments index into lists, so `results.0.price` works.
    """
    current = payload
    for part in path.split("."):
        if part == "":
            continue
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise FeedError(f"path segment {part!r} not valid for a list") from exc
        elif isinstance(current, dict):
            if part not in current:
                raise FeedError(f"path segment {part!r} not found; have {list(current)[:8]}")
            current = current[part]
        else:
            raise FeedError(f"cannot descend into {type(current).__name__} at {part!r}")
    return current


def to_number(value: object, label: str) -> float:
    """Coerce a JSON scalar to a float, tolerating quoted and comma'd numbers."""
    if isinstance(value, bool):
        raise FeedError(f"{label} is a boolean, not a price")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "")
        try:
            return float(cleaned)
        except ValueError as exc:
            raise FeedError(f"{label} is not numeric: {value!r}") from exc
    raise FeedError(f"{label} has unsupported type {type(value).__name__}")


@dataclass
class HttpQuoteSource:
    """Poll a JSON endpoint for a price.

    Args:
        url: Endpoint returning JSON.
        price_path: Dotted path to the price, e.g. `data.last` or `results.0.c`.
        volume_path: Optional dotted path to a volume figure.
        headers: Extra headers, e.g. an API key.
    """

    url: str
    price_path: str
    volume_path: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 15.0

    def fetch(self, now: datetime | None = None) -> Tick:
        request = urllib.request.Request(self.url, headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            hint = " (rate limited)" if exc.code == 429 else ""
            raise FeedError(f"quote request failed: HTTP {exc.code}{hint}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise FeedError(f"quote request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise FeedError(f"quote response was not JSON: {exc}") from exc

        price = to_number(extract_path(payload, self.price_path), "price")
        volume = 0.0
        if self.volume_path:
            try:
                volume = to_number(extract_path(payload, self.volume_path), "volume")
            except FeedError:
                # Volume is optional; its absence must not stop the feed.
                volume = 0.0

        return Tick(ts=now or datetime.now(ET), price=price, volume=volume)
