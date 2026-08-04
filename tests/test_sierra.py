"""Tests for the Sierra Chart .scid reader.

The property that matters most is that tick records are never mistaken for OHLC
bars. Sierra overloads the same four floats for both, and the tick layout puts
the bid in `Low`, the ask in `High` and a sentinel in `Open` — read as a bar
that is not a small error, it is an invented price series with a plausible
shape. Several tests below exist only to pin that distinction down.
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta, timezone

import pytest

from nqcopilot.bars import ET, Bar
from nqcopilot.sierra import (
    HEADER_SIZE,
    RECORD_SIZE,
    SCID_EPOCH,
    SCID_MAGIC,
    IntradayRecord,
    ScidError,
    _to_scid_time,
    iter_records,
    make_tick_record,
    read_scid,
    scid_info,
    write_scid,
)

BASE = datetime(2026, 7, 27, 10, 0, tzinfo=ET)


def bar_record(
    ts: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: int = 100,
) -> IntradayRecord:
    """An OHLC record, the shape Sierra writes for non-tick files."""
    return IntradayRecord(
        ts=ts,
        open=open_,
        high=high,
        low=low,
        close=close,
        num_trades=10,
        total_volume=volume,
        bid_volume=volume // 2,
        ask_volume=volume - volume // 2,
    )


class TestFormatConstants:
    def test_struct_sizes_match_the_documented_layout(self):
        assert HEADER_SIZE == 56
        assert RECORD_SIZE == 40

    def test_epoch_is_december_30_1899_utc(self):
        assert SCID_EPOCH == datetime(1899, 12, 30, tzinfo=timezone.utc)
        assert _to_scid_time(SCID_EPOCH) == 0

    def test_timestamp_conversion_is_exact_at_microsecond_resolution(self):
        # ~4e15 microseconds from the epoch, near the limit of exact float64
        # integers. A float-based conversion drifts here; integer math must not.
        ts = datetime(2026, 7, 27, 10, 0, 0, 1, tzinfo=timezone.utc)
        assert _to_scid_time(ts) % 1_000_000 == 1

    def test_naive_timestamps_are_rejected(self):
        with pytest.raises(ScidError):
            _to_scid_time(datetime(2026, 7, 27, 10, 0))


class TestHeaderValidation:
    def test_rejects_a_file_without_the_scid_magic(self, tmp_path):
        path = tmp_path / "bad.scid"
        path.write_bytes(b"NOPE" + b"\x00" * 100)
        with pytest.raises(ScidError, match="not a Sierra Chart"):
            scid_info(path)

    def test_rejects_a_file_shorter_than_the_header(self, tmp_path):
        path = tmp_path / "stub.scid"
        path.write_bytes(b"SCID")
        with pytest.raises(ScidError, match="shorter than"):
            scid_info(path)

    def test_rejects_a_record_size_smaller_than_the_known_layout(self, tmp_path):
        path = tmp_path / "narrow.scid"
        header = struct.pack("<4sIIHHI36s", SCID_MAGIC, 56, 24, 1, 0, 0, b"\x00" * 36)
        path.write_bytes(header + b"\x00" * 240)
        with pytest.raises(ScidError, match="cannot decode"):
            scid_info(path)

    def test_missing_file_is_reported_clearly(self, tmp_path):
        with pytest.raises(ScidError, match="no such file"):
            scid_info(tmp_path / "absent.scid")

    def test_reads_a_larger_record_size_by_striding_past_the_extra_bytes(self, tmp_path):
        # A future format revision that appends fields must still decode, not
        # silently misalign every record after the first.
        path = tmp_path / "wide.scid"
        header = struct.pack("<4sIIHHI36s", SCID_MAGIC, 56, 48, 1, 0, 0, b"\x00" * 36)
        body = b""
        for i in range(4):
            ts = BASE + timedelta(minutes=i)
            body += struct.Struct("<q4f4I").pack(
                _to_scid_time(ts), 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1, 10, 5, 5
            )
            body += b"\x00" * 8  # the hypothetical appended fields
        path.write_bytes(header + body)

        records = list(iter_records(path))
        assert len(records) == 4
        assert [r.open for r in records] == [100.0, 101.0, 102.0, 103.0]


class TestEmptyFile:
    def test_header_only_file_reports_zero_records(self, tmp_path):
        path = tmp_path / "empty.scid"
        write_scid(path, [])
        info = scid_info(path)
        assert info.record_count == 0
        assert info.first_ts is None
        assert "empty" in info.describe()

    def test_iterating_an_empty_file_yields_nothing(self, tmp_path):
        path = tmp_path / "empty.scid"
        write_scid(path, [])
        assert list(iter_records(path)) == []

    def test_reading_an_empty_file_raises_rather_than_returning_nothing(self, tmp_path):
        path = tmp_path / "empty.scid"
        write_scid(path, [])
        with pytest.raises(ScidError, match="no complete bars"):
            read_scid(path)


class TestTickRecordDetection:
    def test_a_tick_with_bid_ask_is_not_read_as_a_bar(self, tmp_path):
        # The trap: Open=0, High=ask, Low=bid, Close=trade price.
        record = make_tick_record(BASE, price=20_000.25, bid=20_000.00, ask=20_000.50)
        assert record.is_tick
        assert record.price == 20_000.25
        assert record.bid == 20_000.00
        assert record.ask == 20_000.50

    def test_unbundled_sub_trade_sentinels_are_recognised(self):
        # These are float32 constants; widening them to float64 does not
        # reproduce the documented literal, so equality would miss them.
        for sentinel in (-1.99900095e37, -1.99900197e37):
            packed = struct.pack("<f", sentinel)
            (as_float32,) = struct.unpack("<f", packed)
            assert as_float32 != sentinel, "test is meaningless if these match"
            record = bar_record(BASE, as_float32, 1.0, 0.0, 0.5)
            assert record.is_tick

    def test_a_genuine_bar_is_not_flagged_as_a_tick(self):
        assert not bar_record(BASE, 20_000.0, 20_010.0, 19_990.0, 20_005.0).is_tick

    def test_ticks_aggregate_from_trade_price_ignoring_the_spread(self, tmp_path):
        # Bid/ask sit far outside the trades. If the reader treated High/Low as
        # bar extremes the range would blow out to the spread.
        path = tmp_path / "ticks.scid"
        records = [
            make_tick_record(BASE + timedelta(seconds=s), price=p, bid=1.0, ask=99_000.0)
            for s, p in [(0, 100.0), (30, 102.0), (60, 98.0), (120, 101.0)]
        ]
        # A later bucket, so the first bar closes.
        records.append(make_tick_record(BASE + timedelta(minutes=6), price=105.0))
        write_scid(path, records)

        bars = read_scid(path, interval_minutes=5)
        assert len(bars) == 1
        bar = bars[0]
        assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 102.0, 98.0, 101.0)

    def test_tick_volume_sums_into_the_bar(self, tmp_path):
        path = tmp_path / "vol.scid"
        records = [
            make_tick_record(BASE + timedelta(seconds=s), price=100.0, volume=v)
            for s, v in [(0, 3), (30, 5), (60, 7)]
        ]
        records.append(make_tick_record(BASE + timedelta(minutes=6), price=100.0))
        write_scid(path, records)
        assert read_scid(path, interval_minutes=5)[0].volume == 15.0


class TestOhlcRecords:
    def test_one_minute_bars_merge_into_five_minute_bars(self, tmp_path):
        path = tmp_path / "bars.scid"
        records = [
            bar_record(BASE + timedelta(minutes=0), 100.0, 105.0, 99.0, 104.0, volume=10),
            bar_record(BASE + timedelta(minutes=1), 104.0, 108.0, 103.0, 107.0, volume=20),
            bar_record(BASE + timedelta(minutes=2), 107.0, 109.0, 101.0, 102.0, volume=30),
            bar_record(BASE + timedelta(minutes=3), 102.0, 106.0, 100.0, 105.0, volume=40),
            bar_record(BASE + timedelta(minutes=4), 105.0, 110.0, 104.0, 109.0, volume=50),
            # Next bucket, forcing the first to close.
            bar_record(BASE + timedelta(minutes=5), 109.0, 111.0, 108.0, 110.0, volume=60),
        ]
        write_scid(path, records)

        bars = read_scid(path, interval_minutes=5)
        assert len(bars) == 1
        bar = bars[0]
        assert bar.open == 100.0  # first open in the bucket
        assert bar.high == 110.0  # max across the bucket
        assert bar.low == 99.0    # min across the bucket
        assert bar.close == 109.0  # last close in the bucket
        assert bar.volume == 150.0

    def test_bar_is_stamped_at_the_bucket_open(self, tmp_path):
        path = tmp_path / "stamp.scid"
        start = datetime(2026, 7, 27, 10, 2, tzinfo=ET)  # mid-bucket
        write_scid(
            path,
            [
                bar_record(start, 100.0, 101.0, 99.0, 100.5),
                bar_record(start + timedelta(minutes=5), 100.0, 101.0, 99.0, 100.5),
            ],
        )
        bars = read_scid(path, interval_minutes=5)
        assert bars[0].ts == datetime(2026, 7, 27, 10, 0, tzinfo=ET)

    def test_out_of_order_records_do_not_corrupt_a_closed_bar(self, tmp_path):
        path = tmp_path / "unordered.scid"
        write_scid(
            path,
            [
                bar_record(BASE, 100.0, 101.0, 99.0, 100.5),
                bar_record(BASE + timedelta(minutes=6), 200.0, 201.0, 199.0, 200.5),
                # Backdated into the already-closed first bucket.
                bar_record(BASE + timedelta(minutes=1), 500.0, 900.0, 1.0, 500.0),
                bar_record(BASE + timedelta(minutes=11), 300.0, 301.0, 299.0, 300.5),
            ],
        )
        bars = read_scid(path, interval_minutes=5)
        assert bars[0].high == 101.0 and bars[0].low == 99.0


class TestMixedFiles:
    def test_a_file_mixing_ticks_and_bars_is_refused(self, tmp_path):
        path = tmp_path / "mixed.scid"
        write_scid(
            path,
            [
                make_tick_record(BASE, price=100.0),
                bar_record(BASE + timedelta(minutes=6), 100.0, 101.0, 99.0, 100.5),
                bar_record(BASE + timedelta(minutes=11), 100.0, 101.0, 99.0, 100.5),
            ],
        )
        with pytest.raises(ScidError, match="mixes tick and OHLC"):
            read_scid(path)


class TestPartialBars:
    def test_the_trailing_incomplete_bucket_is_dropped_by_default(self, tmp_path):
        path = tmp_path / "partial.scid"
        records = [
            make_tick_record(BASE + timedelta(seconds=s), price=100.0 + s)
            for s in (0, 60, 120)
        ]
        # Opens a second bucket that never closes.
        records.append(make_tick_record(BASE + timedelta(minutes=6), price=500.0))
        write_scid(path, records)

        bars = read_scid(path, interval_minutes=5)
        assert len(bars) == 1
        assert all(b.ts < BASE + timedelta(minutes=5) for b in bars)

    def test_include_partial_emits_the_final_bucket(self, tmp_path):
        path = tmp_path / "partial.scid"
        records = [
            make_tick_record(BASE + timedelta(seconds=s), price=100.0 + s)
            for s in (0, 60, 120)
        ]
        records.append(make_tick_record(BASE + timedelta(minutes=6), price=500.0))
        write_scid(path, records)

        bars = read_scid(path, interval_minutes=5, include_partial=True)
        assert len(bars) == 2
        assert bars[-1].close == 500.0

    def test_include_partial_also_applies_to_ohlc_files(self, tmp_path):
        path = tmp_path / "partial_bars.scid"
        write_scid(path, [bar_record(BASE, 100.0, 101.0, 99.0, 100.5)])
        assert len(read_scid(path, interval_minutes=5, include_partial=True)) == 1
        with pytest.raises(ScidError):
            read_scid(path, interval_minutes=5)


class TestTimeWindow:
    def build(self, tmp_path):
        path = tmp_path / "window.scid"
        write_scid(
            path,
            [
                bar_record(BASE + timedelta(minutes=i), 100.0, 101.0, 99.0, 100.5)
                for i in range(60)
            ],
        )
        return path

    def test_start_is_inclusive(self, tmp_path):
        path = self.build(tmp_path)
        cutoff = BASE + timedelta(minutes=30)
        records = list(iter_records(path, start=cutoff))
        assert records[0].ts.astimezone(ET) == cutoff
        assert len(records) == 30

    def test_end_is_exclusive(self, tmp_path):
        path = self.build(tmp_path)
        cutoff = BASE + timedelta(minutes=30)
        records = list(iter_records(path, end=cutoff))
        assert len(records) == 30
        assert all(r.ts.astimezone(ET) < cutoff for r in records)

    def test_window_selects_the_interior(self, tmp_path):
        path = self.build(tmp_path)
        records = list(
            iter_records(
                path,
                start=BASE + timedelta(minutes=10),
                end=BASE + timedelta(minutes=20),
            )
        )
        assert len(records) == 10

    def test_a_start_past_the_end_of_the_file_yields_nothing(self, tmp_path):
        path = self.build(tmp_path)
        assert list(iter_records(path, start=BASE + timedelta(days=5))) == []

    def test_binary_search_agrees_with_a_full_scan(self, tmp_path):
        # The seek is an optimisation; it must not change which records appear.
        path = self.build(tmp_path)
        for minutes in (0, 1, 17, 59, 60):
            cutoff = BASE + timedelta(minutes=minutes)
            fast = [r.ts for r in iter_records(path, start=cutoff)]
            slow = [r.ts for r in iter_records(path) if r.ts >= cutoff]
            assert fast == slow, f"disagreement at +{minutes}m"


class TestChunkBoundaries:
    def test_records_spanning_many_chunks_are_all_read(self, tmp_path):
        # Larger than one internal read batch, to catch an off-by-one at the seam.
        from nqcopilot.sierra import _CHUNK_RECORDS

        count = _CHUNK_RECORDS * 2 + 5
        path = tmp_path / "big.scid"
        write_scid(
            path,
            [
                make_tick_record(BASE + timedelta(seconds=i), price=100.0 + (i % 7))
                for i in range(count)
            ],
        )
        assert len(list(iter_records(path))) == count


class TestScidInfo:
    def test_reports_span_and_kind_for_tick_data(self, tmp_path):
        path = tmp_path / "info.scid"
        write_scid(
            path,
            [
                make_tick_record(BASE + timedelta(minutes=i), price=100.0)
                for i in range(10)
            ],
        )
        info = scid_info(path)
        assert info.record_count == 10
        assert info.is_tick_data
        assert info.first_ts.astimezone(ET) == BASE
        assert info.last_ts.astimezone(ET) == BASE + timedelta(minutes=9)
        assert "tick data" in info.describe()

    def test_reports_ohlc_kind_for_bar_files(self, tmp_path):
        path = tmp_path / "info_bars.scid"
        write_scid(path, [bar_record(BASE, 100.0, 101.0, 99.0, 100.5)])
        assert not scid_info(path).is_tick_data
        assert "OHLC bars" in scid_info(path).describe()


class TestRoundTrip:
    def test_written_records_read_back_identically(self, tmp_path):
        path = tmp_path / "round.scid"
        original = [
            bar_record(BASE + timedelta(minutes=i), 100.0 + i, 105.0 + i, 95.0 + i, 102.0 + i)
            for i in range(5)
        ]
        write_scid(path, original)
        for before, after in zip(original, iter_records(path)):
            assert after.ts.astimezone(ET) == before.ts
            assert after.close == pytest.approx(before.close)
            assert after.total_volume == before.total_volume

    def test_bars_are_returned_in_eastern_time(self, tmp_path):
        path = tmp_path / "tz.scid"
        write_scid(
            path,
            [
                bar_record(BASE + timedelta(minutes=i), 100.0, 101.0, 99.0, 100.5)
                for i in range(12)
            ],
        )
        bars = read_scid(path, interval_minutes=5)
        assert all(str(b.ts.tzinfo) == "America/New_York" for b in bars)


class TestInvalidArguments:
    def test_nonpositive_interval_is_rejected(self, tmp_path):
        path = tmp_path / "x.scid"
        write_scid(path, [make_tick_record(BASE, price=100.0)])
        with pytest.raises(ScidError, match="must be positive"):
            read_scid(path, interval_minutes=0)


class TestEngineCompatibility:
    def test_output_is_accepted_by_the_bar_series_validator(self, tmp_path):
        from nqcopilot.bars import validate_series

        path = tmp_path / "series.scid"
        write_scid(
            path,
            [
                make_tick_record(BASE + timedelta(seconds=30 * i), price=100.0 + (i % 5))
                for i in range(200)
            ],
        )
        bars = read_scid(path, interval_minutes=5)
        validate_series(bars)
        assert all(isinstance(b, Bar) for b in bars)
