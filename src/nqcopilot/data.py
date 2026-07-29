"""Bar loading: CSV files, a live HTTP feed, and a deterministic demo generator.

No third-party dependencies anywhere in this package. That is a deliberate
reliability choice for something you may run against a funded account: fewer
moving parts, nothing to break on a dependency upgrade, and it runs on a stock
Python install.
"""

from __future__ import annotations

import csv
import json
import math
import random
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta
from pathlib import Path

from .bars import ET, Bar, is_market_open

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Yahoo's chart endpoint keyed by our contract symbols.
_YAHOO_SYMBOLS = {"NQ": "NQ=F", "MNQ": "NQ=F", "ES": "ES=F", "MES": "ES=F"}


class DataError(RuntimeError):
    """Raised when bars cannot be obtained or are unusable."""


def load_csv(path: str | Path, *, assume_tz=ET) -> list[Bar]:
    """Load bars from CSV.

    Expects a header row containing `timestamp,open,high,low,close,volume`
    (case-insensitive; `time`/`date`/`datetime` are accepted for the first
    column, and `vol` for the last). Timestamps may be ISO-8601 or a Unix epoch;
    naive timestamps are interpreted in `assume_tz`.

    This is the format NinjaTrader, Tradovate and TradingView all export, so it
    is the reliable path when a live feed is unavailable.
    """
    path = Path(path)
    if not path.exists():
        raise DataError(f"no such file: {path}")

    rows: list[Bar] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise DataError(f"{path} has no header row")
        field_map = {name.strip().lower(): name for name in reader.fieldnames}

        def pick(*options: str) -> str:
            for opt in options:
                if opt in field_map:
                    return field_map[opt]
            raise DataError(
                f"{path} is missing a column for {options[0]!r}; "
                f"found columns: {list(field_map)}"
            )

        ts_col = pick("timestamp", "time", "datetime", "date")
        o_col, h_col = pick("open", "o"), pick("high", "h")
        l_col, c_col = pick("low", "l"), pick("close", "c")
        try:
            v_col = pick("volume", "vol", "v")
        except DataError:
            v_col = None

        for lineno, row in enumerate(reader, start=2):
            raw_ts = (row[ts_col] or "").strip()
            if not raw_ts:
                continue
            try:
                ts = _parse_timestamp(raw_ts, assume_tz)
                rows.append(
                    Bar(
                        ts=ts,
                        open=float(row[o_col]),
                        high=float(row[h_col]),
                        low=float(row[l_col]),
                        close=float(row[c_col]),
                        volume=float(row[v_col]) if v_col and row[v_col] else 0.0,
                    )
                )
            except (ValueError, TypeError) as exc:
                raise DataError(f"{path} line {lineno}: {exc}") from exc

    if not rows:
        raise DataError(f"{path} contained no usable rows")
    rows.sort(key=lambda b: b.ts)
    return rows


def _parse_timestamp(raw: str, assume_tz) -> datetime:
    """Parse an ISO-8601 or epoch timestamp into a tz-aware datetime."""
    text = raw.strip().replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        try:
            epoch = float(text)
        except ValueError as exc:
            raise ValueError(f"unrecognised timestamp {raw!r}") from exc
        # Heuristic: values this large are milliseconds, not seconds.
        if epoch > 1e11:
            epoch /= 1000.0
        return datetime.fromtimestamp(epoch, tz=assume_tz)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=assume_tz)
    return ts


def fetch_live(
    symbol: str = "NQ",
    interval: str = "5m",
    lookback: str = "5d",
    *,
    timeout: float = 20.0,
    retries: int = 3,
) -> list[Bar]:
    """Fetch recent bars from Yahoo's public chart endpoint.

    Convenient but explicitly not a trading-grade feed: it is delayed, it can
    rate-limit, and bars are occasionally revised. Use it to see the copilot
    working and for after-hours review. For live decisions on a funded account,
    export from your broker platform and use `load_csv`, or wire your Rithmic /
    Tradovate feed to the same `list[Bar]` interface.
    """
    yahoo_symbol = _YAHOO_SYMBOLS.get(symbol.upper(), symbol)
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(yahoo_symbol)}?range={lookback}&interval={interval}"
    )
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return _parse_yahoo(payload)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                # Shared/proxied IPs get throttled; back off and retry.
                _sleep_backoff(attempt)
                continue
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            _sleep_backoff(attempt)

    raise DataError(
        f"could not fetch {symbol} bars ({last_error}). "
        "Export bars from your platform and pass --csv instead."
    )


def _sleep_backoff(attempt: int) -> None:
    import time as _time

    _time.sleep(min(8.0, 2.0**attempt))


def _parse_yahoo(payload: dict) -> list[Bar]:
    """Convert a Yahoo chart response into bars, discarding incomplete entries."""
    try:
        result = payload["chart"]["result"][0]
        stamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as exc:
        error = (payload.get("chart") or {}).get("error")
        raise DataError(f"unexpected chart response: {error or exc}") from exc

    bars: list[Bar] = []
    for i, epoch in enumerate(stamps):
        o, h, l, c = (
            quote["open"][i],
            quote["high"][i],
            quote["low"][i],
            quote["close"][i],
        )
        # Yahoo emits nulls for gaps; a bar with any missing leg is unusable.
        if None in (o, h, l, c):
            continue
        volume = quote.get("volume", [None] * len(stamps))[i] or 0.0
        ts = datetime.fromtimestamp(epoch, tz=ET)
        try:
            bars.append(Bar(ts=ts, open=o, high=h, low=l, close=c, volume=float(volume)))
        except ValueError:
            # Reject rather than repair a bar that violates OHLC consistency.
            continue

    if not bars:
        raise DataError("chart response contained no complete bars")
    return bars


def generate_demo_bars(
    count: int = 400,
    *,
    seed: int = 7,
    start_price: float = 20_400.0,
    interval_minutes: int = 5,
    end: datetime | None = None,
) -> list[Bar]:
    """Deterministic synthetic NQ-like bars for demos and tests.

    Produces a session-aware random walk: volatility peaks at the open, decays
    into lunch and picks up into the close, with a slow drift regime. Seeded, so
    the same seed always yields the same bars — which makes it usable as test
    input, not just a demo toy.

    This is simulated data. Any result produced from it says nothing about how
    the strategy performs on real markets.
    """
    rng = random.Random(seed)
    anchor = (end or datetime.now(ET)).astimezone(ET).replace(second=0, microsecond=0)

    # Walk backwards to find enough tradeable slots, then build forwards.
    slots: list[datetime] = []
    cursor = anchor - timedelta(minutes=anchor.minute % interval_minutes)
    while len(slots) < count:
        if is_market_open(cursor) and time(9, 30) <= cursor.time() < time(16, 0):
            slots.append(cursor)
        cursor -= timedelta(minutes=interval_minutes)
    slots.reverse()

    bars: list[Bar] = []
    price = start_price
    drift = 0.0
    for i, ts in enumerate(slots):
        # Re-roll the drift regime periodically so trends and ranges both appear.
        if i % 60 == 0:
            drift = rng.uniform(-0.35, 0.35)

        minutes_in = (ts.hour * 60 + ts.minute) - (9 * 60 + 30)
        # U-shaped intraday volatility, the well-documented equity index profile.
        shape = 1.6 * math.exp(-minutes_in / 90.0) + 0.55 + 0.5 * math.exp(
            (minutes_in - 390) / 60.0
        )
        vol = 7.5 * shape

        open_price = price
        close_price = open_price + rng.gauss(drift, vol)
        wick_up = abs(rng.gauss(0, vol * 0.55))
        wick_down = abs(rng.gauss(0, vol * 0.55))
        high = max(open_price, close_price) + wick_up
        low = min(open_price, close_price) - wick_down
        volume = max(1.0, rng.gauss(1400, 420) * shape)

        quantise = lambda p: round(p * 4) / 4  # NQ trades in 0.25 increments
        bars.append(
            Bar(
                ts=ts,
                open=quantise(open_price),
                high=quantise(high),
                low=quantise(low),
                close=quantise(close_price),
                volume=round(volume),
            )
        )
        price = close_price

    return bars
