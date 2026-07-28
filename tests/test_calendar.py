"""Tests for economic-calendar parsing and the news-blackout wiring.

No test here touches the network. The parser is exercised against recorded and
synthetic payload shapes, because the upstream response format is not formally
documented and the parser's whole job is to survive that.

The failure modes get more attention than the happy path. A calendar that
silently returns nothing would remove a safety gate without saying so, which is
worse than one that fails outright.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from nqcopilot.bars import ET
from nqcopilot.calendar import (
    IMPORTANCE_HIGH,
    IMPORTANCE_LOW,
    IMPORTANCE_MEDIUM,
    CalendarError,
    EconomicEvent,
    fetch_economic_calendar,
    parse_calendar_payload,
)


class TestPayloadParsing:
    def test_parses_a_conventional_envelope(self):
        payload = {
            "result": [
                {
                    "date": "2026-07-28T12:30:00Z",  # 08:30 ET
                    "title": "Core CPI m/m",
                    "country": "US",
                    "importance": 1,
                }
            ]
        }
        events = parse_calendar_payload(payload)
        assert len(events) == 1
        assert events[0].et.hour == 8 and events[0].et.minute == 30
        assert events[0].title == "Core CPI m/m"
        assert events[0].country == "US"

    @pytest.mark.parametrize("key", ["result", "data", "items", "events", "results", "list"])
    def test_accepts_any_common_envelope_key(self, key):
        payload = {key: [{"date": "2026-07-28T12:30:00Z", "title": "X", "importance": 1}]}
        assert len(parse_calendar_payload(payload)) == 1

    def test_accepts_a_bare_list(self):
        payload = [{"date": "2026-07-28T12:30:00Z", "title": "X", "importance": 1}]
        assert len(parse_calendar_payload(payload)) == 1

    def test_finds_events_nested_deeply(self):
        payload = {"response": {"payload": {"calendar": {"rows": [
            {"date": "2026-07-28T12:30:00Z", "title": "X", "importance": 1}
        ]}}}}
        assert len(parse_calendar_payload(payload)) == 1

    @pytest.mark.parametrize(
        "field", ["date", "time", "datetime", "timestamp", "releaseDate", "dateTime"]
    )
    def test_accepts_any_common_date_field(self, field):
        payload = {"result": [{field: "2026-07-28T12:30:00Z", "title": "X", "importance": 1}]}
        assert len(parse_calendar_payload(payload)) == 1

    @pytest.mark.parametrize(
        "title_field", ["title", "event", "indicator", "name", "comment"]
    )
    def test_accepts_any_common_title_field(self, title_field):
        payload = {"result": [
            {"date": "2026-07-28T12:30:00Z", title_field: "FOMC", "importance": 1}
        ]}
        assert parse_calendar_payload(payload)[0].title == "FOMC"

    def test_epoch_seconds_and_milliseconds(self):
        seconds = 1785587400          # 2026-07-28 12:30 UTC
        payload = {"result": [
            {"date": seconds, "title": "A", "importance": 1},
            {"date": seconds * 1000, "title": "B", "importance": 1},
        ]}
        events = parse_calendar_payload(payload)
        assert len(events) == 2
        assert events[0].ts == events[1].ts

    def test_naive_timestamps_are_treated_as_utc(self):
        """Not local time — a four-hour error would blackout the wrong window."""
        payload = {"result": [
            {"date": "2026-07-28 12:30:00", "title": "CPI", "importance": 1}
        ]}
        event = parse_calendar_payload(payload)[0]
        assert event.et.hour == 8 and event.et.minute == 30

    def test_events_are_sorted_chronologically(self):
        payload = {"result": [
            {"date": "2026-07-28T18:00:00Z", "title": "late", "importance": 1},
            {"date": "2026-07-28T12:30:00Z", "title": "early", "importance": 1},
        ]}
        titles = [e.title for e in parse_calendar_payload(payload)]
        assert titles == ["early", "late"]

    def test_records_without_a_date_are_skipped_not_fatal(self):
        payload = {"result": [
            {"title": "no date here", "importance": 1},
            {"date": "2026-07-28T12:30:00Z", "title": "good", "importance": 1},
        ]}
        events = parse_calendar_payload(payload)
        assert len(events) == 1 and events[0].title == "good"

    def test_missing_title_gets_a_placeholder(self):
        payload = {"result": [{"date": "2026-07-28T12:30:00Z", "importance": 1}]}
        assert parse_calendar_payload(payload)[0].title == "scheduled release"


class TestImportanceFiltering:
    def test_filters_below_the_threshold(self):
        payload = {"result": [
            {"date": "2026-07-28T12:30:00Z", "title": "high", "importance": 1},
            {"date": "2026-07-28T13:30:00Z", "title": "medium", "importance": 0},
            {"date": "2026-07-28T14:30:00Z", "title": "low", "importance": -1},
        ]}
        high_only = parse_calendar_payload(payload, min_importance=IMPORTANCE_HIGH)
        assert [e.title for e in high_only] == ["high"]

        medium_up = parse_calendar_payload(payload, min_importance=IMPORTANCE_MEDIUM)
        assert [e.title for e in medium_up] == ["high", "medium"]

        everything = parse_calendar_payload(payload, min_importance=IMPORTANCE_LOW)
        assert len(everything) == 3

    @pytest.mark.parametrize(
        "raw,expected", [("high", 1), ("High", 1), ("medium", 0), ("low", -1), ("3", 1)]
    )
    def test_textual_importance_is_understood(self, raw, expected):
        payload = {"result": [
            {"date": "2026-07-28T12:30:00Z", "title": "X", "importance": raw}
        ]}
        events = parse_calendar_payload(payload, min_importance=IMPORTANCE_LOW)
        assert events[0].importance == expected

    def test_unknown_importance_is_kept_not_dropped(self):
        """An unparseable importance must not silently hide a real release."""
        payload = {"result": [{"date": "2026-07-28T12:30:00Z", "title": "X"}]}
        assert len(parse_calendar_payload(payload, min_importance=IMPORTANCE_HIGH)) == 1


class TestFailureModes:
    def test_unrecognisable_response_raises(self):
        """Never return [] on a shape change — that would look like 'no news'."""
        with pytest.raises(CalendarError, match="could not locate"):
            parse_calendar_payload({"unexpected": {"shape": True}})

    def test_genuinely_empty_calendar_returns_empty_list(self):
        """A quiet day is a real answer and must be distinguishable from failure."""
        assert parse_calendar_payload({"result": []}) == []

    def test_missing_api_key_raises_with_a_usable_hint(self, monkeypatch):
        monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
        with pytest.raises(CalendarError, match="--news"):
            fetch_economic_calendar()

    def test_http_error_is_wrapped_with_context(self, monkeypatch):
        import urllib.error

        def boom(*a, **k):
            raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(CalendarError, match="RAPIDAPI_KEY"):
            fetch_economic_calendar(api_key="test-key")

    def test_rate_limit_is_reported_clearly(self, monkeypatch):
        import urllib.error

        def boom(*a, **k):
            raise urllib.error.HTTPError("u", 429, "Too Many", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(CalendarError, match="rate limited"):
            fetch_economic_calendar(api_key="test-key")


class TestEventFormatting:
    def test_str_shows_eastern_time_and_country(self):
        event = EconomicEvent(
            ts=datetime(2026, 7, 28, 12, 30, tzinfo=timezone.utc),
            title="Core CPI",
            country="US",
        )
        assert str(event) == "08:30 ET  US Core CPI"


class TestBlackoutWiring:
    """The calendar is only useful if it actually gates entries."""

    def test_fetched_events_become_blackout_windows(self, monkeypatch):
        from nqcopilot.apex import APEX_50K_INTRADAY, AccountState, RiskEngine, RiskLimits

        release = datetime.now(ET).replace(hour=10, minute=0, second=0, microsecond=0)
        engine = RiskEngine(
            AccountState.fresh(APEX_50K_INTRADAY),
            RiskLimits(news_blackout_minutes=15.0),
            news_times=[release],
        )
        codes = {b.code for b in engine.check(release)}
        assert "news_blackout" in codes

    def test_cli_collects_manual_and_fetched_windows(self, monkeypatch):
        from nqcopilot.cli import build_parser, collect_news

        fake = [
            EconomicEvent(
                ts=datetime(2026, 7, 28, 12, 30, tzinfo=timezone.utc),
                title="CPI",
                country="US",
            )
        ]
        monkeypatch.setattr("nqcopilot.cli.fetch_economic_calendar", lambda **k: fake)

        args = build_parser().parse_args(["--demo", "--news", "14:00", "--news-auto"])
        times, events, warning = collect_news(args)
        assert warning is None
        assert len(times) == 2          # one manual, one fetched
        assert events == fake

    def test_cli_warns_loudly_when_the_calendar_fails(self, monkeypatch):
        from nqcopilot.cli import build_parser, collect_news

        def boom(**kwargs):
            raise CalendarError("no key")

        monkeypatch.setattr("nqcopilot.cli.fetch_economic_calendar", boom)
        args = build_parser().parse_args(["--demo", "--news-auto"])
        times, events, warning = collect_news(args)

        assert times == [] and events == []
        # The warning must say the gate is off, not merely that a fetch failed.
        assert warning is not None
        assert "NOT" in warning

    def test_calendar_is_not_consulted_without_the_flag(self, monkeypatch):
        from nqcopilot.cli import build_parser, collect_news

        def boom(**kwargs):
            raise AssertionError("calendar must not be fetched without --news-auto")

        monkeypatch.setattr("nqcopilot.cli.fetch_economic_calendar", boom)
        args = build_parser().parse_args(["--demo"])
        assert collect_news(args) == ([], [], None)
