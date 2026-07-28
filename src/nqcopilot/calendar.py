"""Economic calendar: automatic news-blackout windows.

NQ does not care about your setup at 08:30 ET when CPI prints. Spreads blow out,
stops fill wherever they fill, and a 15-point stop is decoration. The risk layer
has always supported blackout windows; this module removes the need to type them
in by hand each morning.

Backed by the RapidAPI "Trading View" API (apidojo). That API has **no
historical OHLC endpoint** — it cannot be used as a price feed — but its
economic calendar is well suited to this job.

Design note: a calendar fetch failing must never silently remove a safety gate.
Every failure path here raises or warns loudly, and the caller decides. Quietly
trading through FOMC because an HTTP call timed out is exactly the class of
mistake this project exists to avoid.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .bars import ET

# The RapidAPI marketplace host. The path prefix differs between apidojo's own
# gateway and the RapidAPI edge, so it is configurable; override with
# NQCOPILOT_CALENDAR_PATH if your subscription routes differently.
DEFAULT_HOST = "trading-view.p.rapidapi.com"
DEFAULT_PATH = "/calendars/get-economic-calendar"

# TradingView's importance scale.
IMPORTANCE_HIGH = 1
IMPORTANCE_MEDIUM = 0
IMPORTANCE_LOW = -1


class CalendarError(RuntimeError):
    """Raised when calendar data cannot be fetched or parsed."""


@dataclass(frozen=True)
class EconomicEvent:
    """A scheduled release that can move the market."""

    ts: datetime
    title: str
    country: str = ""
    importance: int = IMPORTANCE_HIGH

    @property
    def et(self) -> datetime:
        return self.ts.astimezone(ET)

    def __str__(self) -> str:
        label = f"{self.country} " if self.country else ""
        return f"{self.et:%H:%M ET}  {label}{self.title}"


def fetch_economic_calendar(
    api_key: str | None = None,
    *,
    day: date | None = None,
    countries: str = "US",
    min_importance: int = IMPORTANCE_HIGH,
    host: str | None = None,
    path: str | None = None,
    timeout: float = 20.0,
) -> list[EconomicEvent]:
    """Fetch scheduled releases for `day` (default: today, Eastern).

    Args:
        api_key: RapidAPI key. Falls back to the RAPIDAPI_KEY environment
            variable so it never has to be passed on a command line, where it
            would land in shell history.
        countries: Comma-separated country codes. US alone drives NQ.
        min_importance: -1 low, 0 medium, 1 high. High only, by default.

    Raises:
        CalendarError: on a missing key, a transport failure, or a response that
            cannot be understood. Never returns an empty list to signal failure —
            an empty list means "no events", which is a materially different
            statement.
    """
    key = api_key or os.environ.get("RAPIDAPI_KEY")
    if not key:
        raise CalendarError(
            "no API key: pass api_key or set RAPIDAPI_KEY. "
            "Without it, use --news HH:MM to set blackout windows manually."
        )

    target_day = day or datetime.now(ET).date()
    query = urllib.parse.urlencode(
        {
            "from": target_day.isoformat(),
            "to": (target_day + timedelta(days=1)).isoformat(),
            "countries": countries,
            "minImportance": min_importance,
            "lang": "en",
        }
    )
    resolved_host = host or os.environ.get("NQCOPILOT_CALENDAR_HOST", DEFAULT_HOST)
    resolved_path = path or os.environ.get("NQCOPILOT_CALENDAR_PATH", DEFAULT_PATH)
    url = f"https://{resolved_host}{resolved_path}?{query}"

    request = urllib.request.Request(
        url,
        headers={
            "x-rapidapi-key": key,
            "x-rapidapi-host": resolved_host,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code in (401, 403):
            hint = " — check RAPIDAPI_KEY and that you are subscribed to the API"
        elif exc.code == 404:
            hint = (
                " — the endpoint path may differ on your plan; override it with "
                "NQCOPILOT_CALENDAR_PATH"
            )
        elif exc.code == 429:
            hint = " — rate limited by the API"
        raise CalendarError(f"calendar request failed: HTTP {exc.code}{hint}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CalendarError(f"calendar request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CalendarError(f"calendar response was not JSON: {exc}") from exc

    return parse_calendar_payload(payload, min_importance=min_importance)


def parse_calendar_payload(
    payload: object, *, min_importance: int = IMPORTANCE_HIGH
) -> list[EconomicEvent]:
    """Extract events from a calendar response.

    Written tolerantly on purpose: the upstream response shape is not formally
    documented, and a rigid parser would break on a field rename and take the
    blackout gate down with it. Locates the event list wherever it sits and
    accepts any of the common field spellings.
    """
    records = _find_event_records(payload)
    if records is None:
        raise CalendarError(
            "could not locate an event list in the calendar response; "
            "the API shape may have changed"
        )

    events: list[EconomicEvent] = []
    for record in records:
        ts = _extract_timestamp(record)
        if ts is None:
            continue
        importance = _extract_importance(record)
        if importance is not None and importance < min_importance:
            continue
        title = _first_string(record, ("title", "event", "indicator", "name", "comment"))
        country = _first_string(record, ("country", "currency", "countryCode", "region"))
        events.append(
            EconomicEvent(
                ts=ts,
                title=title or "scheduled release",
                country=(country or "").upper(),
                importance=importance if importance is not None else IMPORTANCE_HIGH,
            )
        )

    events.sort(key=lambda e: e.ts)
    return events


def _find_event_records(payload: object, depth: int = 0) -> list[dict] | None:
    """Locate the list of event dicts inside an arbitrarily nested response.

    Returns None when no event list can be found, and `[]` when one is found and
    is genuinely empty. That distinction is load-bearing: a quiet news day must
    not be reported as a parse failure, or the caller would warn that blackout
    windows are inactive when in fact there is simply nothing scheduled.
    """
    if depth > 6:
        return None

    if isinstance(payload, list):
        if not payload:
            return []  # an empty list is a real, empty answer
        dicts = [item for item in payload if isinstance(item, dict)]
        # A list of dicts carrying a date-ish field is the event list.
        if dicts and any(_extract_timestamp(d) is not None for d in dicts):
            return dicts
        return None

    if isinstance(payload, dict):
        # A conventional envelope key is trusted, so an empty list under it
        # counts as a found-but-empty result.
        for key in ("result", "data", "items", "events", "results", "list"):
            if key in payload:
                found = _find_event_records(payload[key], depth + 1)
                if found is not None:
                    return found
        # The blind search is not trusted the same way: only a non-empty match
        # counts, so an unrelated empty list elsewhere cannot masquerade as
        # "no events today".
        for value in payload.values():
            found = _find_event_records(value, depth + 1)
            if found:
                return found

    return None


def _extract_timestamp(record: dict) -> datetime | None:
    """Parse whichever date field the record carries, into Eastern time."""
    for key in ("date", "time", "datetime", "timestamp", "releaseDate", "dateTime"):
        if key not in record:
            continue
        raw = record[key]
        if raw is None:
            continue
        parsed = _parse_moment(raw)
        if parsed is not None:
            return parsed
    return None


def _parse_moment(raw: object) -> datetime | None:
    if isinstance(raw, (int, float)):
        epoch = float(raw)
        if epoch > 1e11:  # milliseconds
            epoch /= 1000.0
        try:
            return datetime.fromtimestamp(epoch, tz=ET)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        text = raw.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                epoch = float(text)
            except ValueError:
                return None
            return _parse_moment(epoch)
        # A naive timestamp from this API is UTC, not local.
        if parsed.tzinfo is None:
            from datetime import timezone

            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ET)
    return None


def _extract_importance(record: dict) -> int | None:
    for key in ("importance", "impact", "importanceLevel", "priority"):
        if key not in record:
            continue
        raw = record[key]
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            return int(raw)
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text in ("high", "3"):
                return IMPORTANCE_HIGH
            if text in ("medium", "moderate", "2"):
                return IMPORTANCE_MEDIUM
            if text in ("low", "1"):
                return IMPORTANCE_LOW
            try:
                return int(float(text))
            except ValueError:
                continue
    return None


def _first_string(record: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
