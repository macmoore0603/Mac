"""Read Sierra Chart's binary intraday files (`.scid`) into bars.

Sierra Chart is the one data source in reach that fixes this project's largest
gap: no real-data backtest has ever run, because free intraday futures history
was not obtainable. A `.scid` file sitting in `Data/` is real exchange history
for the instrument you actually trade, at tick resolution, already on disk.

Two properties of the format make a naive reader dangerous, and both are handled
here:

* **Most NQ files are tick data, not bars.** Sierra records a single trade by
  overloading the OHLC fields: `Open` is set to a sentinel, `High` carries the
  *ask*, `Low` the *bid*, and `Close` the trade price. Reading those four floats
  as an OHLC bar yields an open of zero and a range spanning the spread — prices
  that are not merely wrong but wrong in a way that still looks plottable. Tick
  records are detected and aggregated from the trade price alone.
* **The files are large.** A year of NQ ticks runs to gigabytes, so records are
  streamed in chunks and never held in memory at once. Because records are
  fixed-width and ordered by time, a date filter binary-searches to its start
  offset instead of scanning.

The format itself (documented at `sierrachart.com`, *Intraday Data File Format*):
a 56-byte header, then fixed 40-byte records, little-endian throughout.
Timestamps are microseconds since 1899-12-30 UTC.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .bars import ET, Bar
from .data import DataError
from .feed import BarAggregator, Tick


class ScidError(DataError):
    """Raised when a .scid file cannot be read or understood.

    A subclass of `DataError` so every caller that already handles "bars could
    not be loaded" handles this too — notably the `--watch` reload loop, which
    must survive a transient read of a file Sierra Chart is still appending to.
    """


SCID_MAGIC = b"SCID"

# s_IntradayHeader: 4+4+4+2+2+4+36 = 56 bytes.
_HEADER_STRUCT = struct.Struct("<4sIIHHI36s")
# s_IntradayRecord: 8 + 4*4 + 4*4 = 40 bytes.
_RECORD_STRUCT = struct.Struct("<q4f4I")

HEADER_SIZE = _HEADER_STRUCT.size
RECORD_SIZE = _RECORD_STRUCT.size

# Sierra Chart counts microseconds from this instant, in UTC.
SCID_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)

# Records read per file operation. 8192 records is 320 KB — large enough that
# syscall overhead disappears on a multi-GB file, small enough to stay cheap.
_CHUNK_RECORDS = 8192

# Sentinels Sierra Chart writes into `Open` to mark a single-trade record.
# Comparing against the documented literals with `==` would fail: they are
# float32 constants, and widening them to a Python float does not reproduce the
# float64 literal exactly. Both live far outside any real price, so they are
# recognised by magnitude instead — which is exact where equality is not.
_UNBUNDLED_SENTINEL_CEILING = -1e36


@dataclass(frozen=True)
class IntradayRecord:
    """One 40-byte record, decoded but not yet interpreted.

    Whether this is a bar or a single trade depends on `is_tick`; the OHLC
    fields mean different things in each case.
    """

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    num_trades: int
    total_volume: int
    bid_volume: int
    ask_volume: int

    @property
    def is_tick(self) -> bool:
        """True when this record is a single trade rather than an OHLC bar.

        `Open` is 0.0 for a trade carrying bid/ask, or a large negative sentinel
        for one leg of an unbundled trade. A genuine futures bar never opens at
        zero, so the test is unambiguous.
        """
        return self.open == 0.0 or self.open < _UNBUNDLED_SENTINEL_CEILING

    @property
    def price(self) -> float:
        """The traded price, valid for tick records."""
        return self.close

    @property
    def bid(self) -> float | None:
        """Bid at the time of the trade, when this is a tick with quotes."""
        return self.low if self.open == 0.0 else None

    @property
    def ask(self) -> float | None:
        """Ask at the time of the trade, when this is a tick with quotes."""
        return self.high if self.open == 0.0 else None


@dataclass(frozen=True)
class ScidInfo:
    """Cheap summary of a file: header fields and the span it covers."""

    path: Path
    version: int
    record_size: int
    record_count: int
    first_ts: datetime | None
    last_ts: datetime | None
    is_tick_data: bool

    def describe(self) -> str:
        if self.record_count == 0:
            return f"{self.path.name}: empty (no records)"
        kind = "tick data" if self.is_tick_data else "OHLC bars"
        first = self.first_ts.astimezone(ET).strftime("%Y-%m-%d %H:%M") if self.first_ts else "?"
        last = self.last_ts.astimezone(ET).strftime("%Y-%m-%d %H:%M") if self.last_ts else "?"
        return (
            f"{self.path.name}: {self.record_count:,} records of {kind}, "
            f"{first} to {last} ET"
        )


def _to_datetime(microseconds: int) -> datetime:
    """Convert a SCDateTimeMS value to a UTC datetime."""
    return SCID_EPOCH + timedelta(microseconds=microseconds)


def _to_scid_time(ts: datetime) -> int:
    """Convert a datetime to a SCDateTimeMS value.

    Computed with integer arithmetic rather than `total_seconds()`: modern
    timestamps are ~4e15 microseconds from the 1899 epoch, close enough to the
    2^53 limit of exact float64 integers that the float path can land a
    microsecond off.
    """
    if ts.tzinfo is None:
        raise ScidError(f"timestamp must be timezone-aware: {ts!r}")
    delta = ts.astimezone(timezone.utc) - SCID_EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _read_header(fh, path: Path) -> tuple[int, int, int]:
    """Validate the file header. Returns (header_size, record_size, version)."""
    raw = fh.read(HEADER_SIZE)
    if len(raw) < HEADER_SIZE:
        raise ScidError(f"{path}: file is shorter than a {HEADER_SIZE}-byte header")

    magic, header_size, record_size, version, _unused, _utc_start, _reserve = (
        _HEADER_STRUCT.unpack(raw)
    )
    if magic != SCID_MAGIC:
        raise ScidError(
            f"{path}: not a Sierra Chart intraday file "
            f"(expected header {SCID_MAGIC!r}, found {magic!r})"
        )
    # Trust the header's own sizes rather than the constants, so a future format
    # revision that appends fields still reads correctly instead of silently
    # decoding misaligned bytes.
    if header_size < HEADER_SIZE:
        raise ScidError(f"{path}: header claims {header_size} bytes, minimum is {HEADER_SIZE}")
    if record_size < RECORD_SIZE:
        raise ScidError(
            f"{path}: record size {record_size} is smaller than the known "
            f"{RECORD_SIZE}-byte layout; this reader cannot decode it"
        )
    return header_size, record_size, version


def _record_count(file_size: int, header_size: int, record_size: int) -> int:
    if file_size <= header_size:
        return 0
    return (file_size - header_size) // record_size


def _decode(raw: bytes) -> IntradayRecord:
    (
        dt,
        open_,
        high,
        low,
        close,
        num_trades,
        total_volume,
        bid_volume,
        ask_volume,
    ) = _RECORD_STRUCT.unpack(raw[:RECORD_SIZE])
    return IntradayRecord(
        ts=_to_datetime(dt),
        open=open_,
        high=high,
        low=low,
        close=close,
        num_trades=num_trades,
        total_volume=total_volume,
        bid_volume=bid_volume,
        ask_volume=ask_volume,
    )


def _read_record_at(fh, header_size: int, record_size: int, index: int) -> IntradayRecord:
    fh.seek(header_size + index * record_size)
    raw = fh.read(record_size)
    if len(raw) < RECORD_SIZE:
        raise ScidError(f"truncated record at index {index}")
    return _decode(raw)


def _seek_first_at_or_after(
    fh, header_size: int, record_size: int, count: int, target_us: int
) -> int:
    """Binary-search for the first record at or after `target_us`.

    Records are written in chronological order, so this turns a date-filtered
    read of a multi-gigabyte file into a handful of seeks.
    """
    lo, hi = 0, count
    while lo < hi:
        mid = (lo + hi) // 2
        fh.seek(header_size + mid * record_size)
        raw = fh.read(record_size)
        if len(raw) < RECORD_SIZE:
            hi = mid
            continue
        (dt,) = struct.unpack_from("<q", raw, 0)
        if dt < target_us:
            lo = mid + 1
        else:
            hi = mid
    return lo


def scid_info(path: str | Path) -> ScidInfo:
    """Summarise a file without reading it end to end."""
    path = Path(path)
    if not path.exists():
        raise ScidError(f"no such file: {path}")

    with path.open("rb") as fh:
        header_size, record_size, version = _read_header(fh, path)
        count = _record_count(path.stat().st_size, header_size, record_size)
        if count == 0:
            return ScidInfo(path, version, record_size, 0, None, None, False)
        first = _read_record_at(fh, header_size, record_size, 0)
        last = _read_record_at(fh, header_size, record_size, count - 1)

    return ScidInfo(
        path=path,
        version=version,
        record_size=record_size,
        record_count=count,
        first_ts=first.ts,
        last_ts=last.ts,
        is_tick_data=first.is_tick,
    )


def iter_records(
    path: str | Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Iterator[IntradayRecord]:
    """Stream decoded records, optionally limited to a time window.

    `start` is inclusive and `end` exclusive. Memory use is constant regardless
    of file size.
    """
    path = Path(path)
    if not path.exists():
        raise ScidError(f"no such file: {path}")

    end_us = _to_scid_time(end) if end is not None else None

    with path.open("rb") as fh:
        header_size, record_size, _version = _read_header(fh, path)
        count = _record_count(path.stat().st_size, header_size, record_size)
        if count == 0:
            return

        index = 0
        if start is not None:
            index = _seek_first_at_or_after(
                fh, header_size, record_size, count, _to_scid_time(start)
            )

        fh.seek(header_size + index * record_size)
        remaining = count - index
        while remaining > 0:
            batch = min(_CHUNK_RECORDS, remaining)
            blob = fh.read(batch * record_size)
            if not blob:
                return
            usable = len(blob) // record_size
            for i in range(usable):
                offset = i * record_size
                if end_us is not None:
                    (dt,) = struct.unpack_from("<q", blob, offset)
                    if dt >= end_us:
                        return
                yield _decode(blob[offset : offset + record_size])
            if usable < batch:
                return
            remaining -= usable


def read_scid(
    path: str | Path,
    *,
    interval_minutes: int = 5,
    start: datetime | None = None,
    end: datetime | None = None,
    include_partial: bool = False,
) -> list[Bar]:
    """Load a `.scid` file as bars on `interval_minutes` boundaries.

    Handles both file flavours. Tick records are aggregated from the trade price;
    OHLC records are merged into the target interval. Timestamps are converted to
    Eastern time, matching every other bar source in this package.

    The trailing bucket is dropped unless `include_partial` is set. A file can be
    captured mid-bar, and nothing in it says whether the last bucket finished —
    an unfinished bar understates its own range and volume, which quietly biases
    every indicator that reads it. Dropping it costs one bar and removes the
    question.
    """
    if interval_minutes <= 0:
        raise ScidError("interval_minutes must be positive")

    aggregator = BarAggregator(interval_minutes=interval_minutes)
    bars: list[Bar] = []
    pending: list[Bar] = []  # OHLC records for the bucket being merged
    bucket_start: datetime | None = None
    saw_tick = False
    saw_bar = False

    for record in iter_records(path, start=start, end=end):
        if record.is_tick:
            saw_tick = True
            completed = aggregator.add_tick(
                Tick(
                    ts=record.ts,
                    price=record.price,
                    volume=float(record.total_volume),
                )
            )
            if completed is not None:
                bars.append(completed)
            continue

        saw_bar = True
        bucket = aggregator.bucket_for(record.ts)
        if bucket_start is None:
            bucket_start = bucket
        elif bucket > bucket_start:
            merged = _merge(pending, bucket_start)
            if merged is not None:
                bars.append(merged)
            pending = []
            bucket_start = bucket
        elif bucket < bucket_start:
            # Out-of-order record; merging it would corrupt a closed bar.
            continue
        pending.append(_record_to_bar(record))

    if saw_tick and saw_bar:
        raise ScidError(
            f"{path} mixes tick and OHLC records; this reader cannot tell which "
            "interval the bars represent, so aggregating them would be a guess"
        )

    if include_partial:
        if saw_tick:
            final = aggregator.force_close()
            if final is not None:
                bars.append(final)
        elif pending and bucket_start is not None:
            merged = _merge(pending, bucket_start)
            if merged is not None:
                bars.append(merged)

    if not bars:
        raise ScidError(
            f"{path} produced no complete bars for the requested window; "
            "widen --scid-start/--scid-end or check the file covers the dates"
        )
    return bars


def _record_to_bar(record: IntradayRecord) -> Bar:
    """Convert an OHLC record to a Bar, in Eastern time."""
    return Bar(
        ts=record.ts.astimezone(ET),
        open=record.open,
        high=record.high,
        low=record.low,
        close=record.close,
        volume=float(record.total_volume),
    )


def _merge(bars: list[Bar], bucket_start: datetime) -> Bar | None:
    """Combine sub-interval bars into one bar stamped at the bucket open."""
    if not bars:
        return None
    return Bar(
        ts=bucket_start,
        open=bars[0].open,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
        close=bars[-1].close,
        volume=sum(b.volume for b in bars),
    )


def write_scid(path: str | Path, records: list[IntradayRecord]) -> None:
    """Write a `.scid` file. Exists so tests can build fixtures the reader reads.

    Not part of the trading path — Sierra Chart owns its own data directory, and
    writing into it from outside is a good way to corrupt a live feed.
    """
    path = Path(path)
    header = _HEADER_STRUCT.pack(
        SCID_MAGIC, HEADER_SIZE, RECORD_SIZE, 1, 0, 0, b"\x00" * 36
    )
    with path.open("wb") as fh:
        fh.write(header)
        for record in records:
            fh.write(
                _RECORD_STRUCT.pack(
                    _to_scid_time(record.ts),
                    record.open,
                    record.high,
                    record.low,
                    record.close,
                    record.num_trades,
                    record.total_volume,
                    record.bid_volume,
                    record.ask_volume,
                )
            )


def make_tick_record(
    ts: datetime, price: float, volume: int = 1, *, bid: float | None = None,
    ask: float | None = None,
) -> IntradayRecord:
    """Build a single-trade record the way Sierra Chart writes one."""
    return IntradayRecord(
        ts=ts,
        open=0.0,
        high=ask if ask is not None else price,
        low=bid if bid is not None else price,
        close=price,
        num_trades=1,
        total_volume=volume,
        bid_volume=0,
        ask_volume=volume,
    )
