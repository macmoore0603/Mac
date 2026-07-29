"""Tests for the quote-to-bar adapter.

The important property is that a bar is emitted only once its interval has
fully elapsed. Handing the engine a bar that can still change would reintroduce
the lookahead the indicator layer is careful to avoid.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from nqcopilot.bars import ET
from nqcopilot.feed import (
    BarAggregator,
    FeedError,
    HttpQuoteSource,
    Tick,
    extract_path,
    to_number,
)

BASE = datetime(2026, 7, 27, 10, 0, tzinfo=ET)


def tick(offset_seconds: int, price: float, volume: float = 0.0) -> Tick:
    return Tick(ts=BASE + timedelta(seconds=offset_seconds), price=price, volume=volume)


class TestBucketing:
    def test_buckets_land_on_wall_clock_boundaries(self):
        agg = BarAggregator(interval_minutes=5)
        assert agg.bucket_for(datetime(2026, 7, 27, 10, 3, 42, tzinfo=ET)).minute == 0
        assert agg.bucket_for(datetime(2026, 7, 27, 10, 7, 1, tzinfo=ET)).minute == 5
        assert agg.bucket_for(datetime(2026, 7, 27, 10, 59, 59, tzinfo=ET)).minute == 55

    def test_seconds_and_micros_are_stripped(self):
        agg = BarAggregator()
        bucket = agg.bucket_for(datetime(2026, 7, 27, 10, 3, 42, 123456, tzinfo=ET))
        assert bucket.second == 0 and bucket.microsecond == 0

    def test_rejects_a_nonpositive_interval(self):
        with pytest.raises(ValueError):
            BarAggregator(interval_minutes=0)


class TestAggregation:
    def test_no_bar_until_the_interval_elapses(self):
        """A bar that could still change must never reach the engine."""
        agg = BarAggregator(interval_minutes=5)
        assert agg.add_tick(tick(0, 20_000)) is None
        assert agg.add_tick(tick(60, 20_010)) is None
        assert agg.add_tick(tick(299, 20_005)) is None  # still inside the bar

    def test_bar_emitted_when_the_next_interval_starts(self):
        agg = BarAggregator(interval_minutes=5)
        agg.add_tick(tick(0, 20_000))
        agg.add_tick(tick(60, 20_020))
        agg.add_tick(tick(120, 19_990))
        agg.add_tick(tick(180, 20_010))

        bar = agg.add_tick(tick(301, 20_015))  # crosses into 10:05
        assert bar is not None
        assert bar.ts == BASE
        assert bar.open == 20_000
        assert bar.high == 20_020
        assert bar.low == 19_990
        assert bar.close == 20_010

    def test_volume_accumulates_within_a_bar(self):
        agg = BarAggregator()
        agg.add_tick(tick(0, 20_000, volume=10))
        agg.add_tick(tick(60, 20_001, volume=15))
        bar = agg.add_tick(tick(301, 20_002, volume=99))
        assert bar.volume == pytest.approx(25.0)

    def test_a_later_bar_starts_clean(self):
        agg = BarAggregator()
        agg.add_tick(tick(0, 20_000, volume=10))
        agg.add_tick(tick(301, 20_500, volume=5))
        second = agg.add_tick(tick(601, 20_600))
        assert second.open == 20_500
        assert second.volume == pytest.approx(5.0)

    def test_out_of_order_ticks_are_ignored(self):
        """A stale tick must not corrupt the bar being built."""
        agg = BarAggregator()
        agg.add_tick(tick(301, 20_500))         # now building 10:05
        assert agg.add_tick(tick(0, 19_000)) is None
        bar = agg.add_tick(tick(601, 20_600))
        assert bar.low == 20_500                # the 19,000 never landed

    def test_skipped_intervals_do_not_invent_bars(self):
        """A quiet period yields no bar rather than a fabricated flat one."""
        agg = BarAggregator()
        agg.add_tick(tick(0, 20_000))
        bar = agg.add_tick(tick(1_500, 20_100))  # 25 minutes later
        assert bar is not None and bar.ts == BASE
        assert agg.add_tick(tick(1_560, 20_110)) is None

    def test_single_tick_bar_is_valid(self):
        agg = BarAggregator()
        agg.add_tick(tick(0, 20_000))
        bar = agg.add_tick(tick(301, 20_100))
        assert bar.open == bar.high == bar.low == bar.close == 20_000

    def test_force_close_emits_the_partial_bar(self):
        agg = BarAggregator()
        agg.add_tick(tick(0, 20_000))
        agg.add_tick(tick(60, 20_050))
        bar = agg.force_close()
        assert bar is not None and bar.close == 20_050
        assert agg.force_close() is None  # nothing left

    def test_pending_close_tracks_the_live_price(self):
        agg = BarAggregator()
        assert agg.pending_close is None
        agg.add_tick(tick(0, 20_000))
        agg.add_tick(tick(30, 20_042))
        assert agg.pending_close == pytest.approx(20_042)

    def test_emitted_bars_are_valid_ohlc(self):
        """Bar's own consistency check must never trip on aggregator output."""
        agg = BarAggregator()
        prices = [20_000, 20_050, 19_980, 20_030, 20_010]
        for i, price in enumerate(prices):
            agg.add_tick(tick(i * 30, price))
        bar = agg.add_tick(tick(301, 20_100))
        assert bar.high >= max(bar.open, bar.close)
        assert bar.low <= min(bar.open, bar.close)


class TestPathExtraction:
    def test_nested_dict_path(self):
        payload = {"data": {"quote": {"last": 20_412.25}}}
        assert extract_path(payload, "data.quote.last") == 20_412.25

    def test_list_index_in_path(self):
        payload = {"results": [{"c": 1.0}, {"c": 2.0}]}
        assert extract_path(payload, "results.1.c") == 2.0

    def test_missing_key_names_what_was_available(self):
        with pytest.raises(FeedError, match="not found"):
            extract_path({"a": 1, "b": 2}, "c")

    def test_descending_into_a_scalar_is_an_error(self):
        with pytest.raises(FeedError, match="cannot descend"):
            extract_path({"a": 5}, "a.b")

    def test_bad_list_index(self):
        with pytest.raises(FeedError):
            extract_path({"r": [1, 2]}, "r.9")


class TestNumberCoercion:
    @pytest.mark.parametrize(
        "value,expected",
        [(20_412.25, 20_412.25), (20412, 20412.0), ("20412.25", 20_412.25),
         ("20,412.25", 20_412.25), ("$20412.25", 20_412.25), ("  20412 ", 20_412.0)],
    )
    def test_accepts_common_shapes(self, value, expected):
        assert to_number(value, "price") == pytest.approx(expected)

    def test_rejects_booleans(self):
        with pytest.raises(FeedError, match="boolean"):
            to_number(True, "price")

    def test_rejects_nonsense(self):
        with pytest.raises(FeedError, match="not numeric"):
            to_number("N/A", "price")


class TestHttpQuoteSource:
    def _stub(self, monkeypatch, payload, status=None):
        class FakeResponse:
            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResponse())

    def test_fetches_and_parses_a_price(self, monkeypatch):
        self._stub(monkeypatch, {"data": {"last": "20,412.25", "vol": 1234}})
        source = HttpQuoteSource(
            url="https://example.test/q", price_path="data.last", volume_path="data.vol"
        )
        result = source.fetch(now=BASE)
        assert result.price == pytest.approx(20_412.25)
        assert result.volume == pytest.approx(1234.0)
        assert result.ts == BASE

    def test_missing_volume_is_not_fatal(self, monkeypatch):
        """A feed without volume should still produce usable bars."""
        self._stub(monkeypatch, {"data": {"last": 20_412.25}})
        source = HttpQuoteSource(
            url="https://example.test/q", price_path="data.last", volume_path="data.nope"
        )
        assert source.fetch(now=BASE).volume == 0.0

    def test_missing_price_is_fatal(self, monkeypatch):
        self._stub(monkeypatch, {"data": {}})
        source = HttpQuoteSource(url="https://example.test/q", price_path="data.last")
        with pytest.raises(FeedError):
            source.fetch()

    def test_http_error_is_wrapped(self, monkeypatch):
        import urllib.error

        def boom(*a, **k):
            raise urllib.error.HTTPError("u", 429, "Too Many", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", boom)
        source = HttpQuoteSource(url="https://example.test/q", price_path="p")
        with pytest.raises(FeedError, match="rate limited"):
            source.fetch()

    def test_non_json_is_wrapped(self, monkeypatch):
        class FakeResponse:
            def read(self):
                return b"<html>not json</html>"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResponse())
        source = HttpQuoteSource(url="https://example.test/q", price_path="p")
        with pytest.raises(FeedError, match="not JSON"):
            source.fetch()


class TestEndToEnd:
    def test_quotes_become_bars_the_engine_accepts(self, monkeypatch):
        """The whole point: a price stream turns into engine-readable bars."""
        from nqcopilot.bars import validate_series

        agg = BarAggregator(interval_minutes=5)
        bars = []
        price = 20_000.0
        for minute in range(0, 60):
            price += 3 if minute % 3 else -2
            bar = agg.add_tick(
                Tick(ts=BASE + timedelta(minutes=minute), price=price, volume=10)
            )
            if bar:
                bars.append(bar)

        assert len(bars) == 11  # 12 buckets touched, last still open
        validate_series(bars)   # must not raise
