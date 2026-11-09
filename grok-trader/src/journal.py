"""Append-only JSON Lines log of every decision, plus a tail reader.

One line per event (buy, skip, sell, error). This file is what you review
after a few days of paper trading; never edit it by hand.
"""
from __future__ import annotations

import json
import os
from collections import Counter, deque
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def append(path: str, record: dict) -> dict:
    """Write one record as a single JSON line. Adds a UTC timestamp if missing."""
    record.setdefault("ts", iso(utcnow()))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    return record


def tail(path: str, n: int = 50) -> list[dict]:
    """Last `n` records, oldest first. Skips corrupt lines instead of crashing."""
    if not os.path.exists(path):
        return []
    last: deque[str] = deque(maxlen=n)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                last.append(line)
    out = []
    for line in last:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def summarize(records: list[dict]) -> dict:
    """Action counts and per-check PASS/FAIL/UNKNOWN counts for the dashboard."""
    actions = Counter(r.get("action", "?") for r in records)
    checks: dict[str, Counter] = {}
    grok_calls, grok_cost = 0, 0.0
    for r in records:
        for c in r.get("checks") or []:
            checks.setdefault(c["name"], Counter())[c["status"]] += 1
        g = r.get("grok")
        if isinstance(g, dict) and g.get("usage"):
            grok_calls += 1
            grok_cost += g.get("cost_usd") or 0.0
    return {"actions": dict(actions), "checks": {k: dict(v) for k, v in checks.items()},
            "grok_calls": grok_calls, "grok_cost_usd": round(grok_cost, 4)}
