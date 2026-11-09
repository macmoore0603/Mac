"""New-token feed and pool data from the DEX Screener public API (no key).

Endpoints (verified live, see README "What was verified"):
  GET https://api.dexscreener.com/token-profiles/latest/v1          60 req/min
  GET https://api.dexscreener.com/token-pairs/v1/{chainId}/{token}  300 req/min
"""
from __future__ import annotations

import json
import logging
import os
import random
import time

import requests

log = logging.getLogger("feed")

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain}/{token}"
USER_AGENT = "grok-trader/0.1 (paper-trading research bot)"


class FeedError(Exception):
    """Raised when an HTTP source keeps failing after retries."""


class RateLimiter:
    """Keep at most `per_minute` calls per minute by spacing them evenly."""

    def __init__(self, per_minute: int):
        self.interval = 60.0 / max(1, per_minute)
        self.next_ok = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self.next_ok:
            time.sleep(self.next_ok - now)
        self.next_ok = max(now, self.next_ok) + self.interval


def backoff_sleep(attempt: int, base: float = 1.5, cap: float = 30.0) -> None:
    """Exponential backoff with jitter: 1.5s, 3s, 6s, ... capped at 30s."""
    time.sleep(min(cap, base * 2 ** attempt) * (0.7 + 0.6 * random.random()))


def get_json(session: requests.Session, url: str, *, limiter: RateLimiter | None = None,
             timeout: float = 20, retries: int = 4):
    """GET and parse JSON, retrying on 429 / 5xx / network errors."""
    last = "no attempt"
    for attempt in range(retries):
        if limiter:
            limiter.wait()
        try:
            r = session.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
            if r.status_code == 200:
                return r.json()
            if r.status_code in (400, 404):
                return None  # unknown token: nothing to retry
            last = f"HTTP {r.status_code}"
            if r.status_code not in (429, 403) and r.status_code < 500:
                break
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        log.warning("GET %s failed (%s), retry %d/%d", url, last, attempt + 1, retries)
        backoff_sleep(attempt)
    raise FeedError(f"GET {url}: {last}")


_profiles_limiter = RateLimiter(60)
_pairs_limiter = RateLimiter(300)


def latest_solana_tokens(session: requests.Session) -> list[str]:
    """Token addresses from the latest-profiles feed, Solana only, newest first."""
    data = get_json(session, PROFILES_URL, limiter=_profiles_limiter) or []
    seen, out = set(), []
    for item in data:
        addr = item.get("tokenAddress")
        if item.get("chainId") == "solana" and addr and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def token_pairs(session: requests.Session, mint: str) -> list[dict]:
    url = PAIRS_URL.format(chain="solana", token=mint)
    return get_json(session, url, limiter=_pairs_limiter) or []


def main_pool(pairs: list[dict], mint: str | None = None) -> dict | None:
    """The pool with the most USD liquidity; pools without a liquidity figure lose.

    DEX Screener also returns pools where the token is the QUOTE side; their
    priceUsd belongs to the other token, so base-side pools are preferred.
    """
    if not pairs:
        return None
    if mint:
        base = [p for p in pairs if ((p.get("baseToken") or {}).get("address") or "").lower() == mint.lower()]
        pairs = base or pairs

    def liq(p):
        v = (p.get("liquidity") or {}).get("usd")
        return float(v) if v is not None else -1.0

    return max(pairs, key=liq)


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def pool_snapshot(pair: dict | None, now_ms: int) -> dict:
    """Flatten the fields the checks and Grok need. Missing data stays None."""
    if not pair:
        return {"pair_address": None, "liquidity_usd": None, "pool_age_minutes": None,
                "price_usd": None, "symbol": "?", "name": "?"}
    created = pair.get("pairCreatedAt")  # milliseconds since epoch
    base = pair.get("baseToken") or {}
    return {
        "pair_address": pair.get("pairAddress"),
        "dex_id": pair.get("dexId"),
        "url": pair.get("url"),
        "symbol": base.get("symbol") or "?",
        "name": base.get("name") or "?",
        "quote_symbol": (pair.get("quoteToken") or {}).get("symbol"),
        "price_usd": _num(pair.get("priceUsd")),
        "liquidity_usd": _num((pair.get("liquidity") or {}).get("usd")),
        "pool_age_minutes": (now_ms - created) / 60000 if created else None,
        "fdv_usd": _num(pair.get("fdv")),
        "market_cap_usd": _num(pair.get("marketCap")),
        "volume_h24_usd": _num((pair.get("volume") or {}).get("h24")),
        "buys_h1": ((pair.get("txns") or {}).get("h1") or {}).get("buys"),
        "sells_h1": ((pair.get("txns") or {}).get("h1") or {}).get("sells"),
    }


def current_price(session: requests.Session, mint: str) -> float | None:
    """Latest USD price of the token's main pool, for marking paper positions."""
    pool = main_pool(token_pairs(session, mint), mint)
    return _num(pool.get("priceUsd")) if pool else None


class SeenCache:
    """Remembers tokens already evaluated so each one is scored once."""

    def __init__(self, path: str, keep: int = 5000):
        self.path, self.keep = path, keep
        self.seen: dict[str, str] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.seen = json.load(fh)

    def __contains__(self, mint: str) -> bool:
        return mint in self.seen

    def add(self, mint: str, when: str) -> None:
        self.seen[mint] = when
        if len(self.seen) > self.keep:  # drop the oldest entries
            for old in list(self.seen)[: len(self.seen) - self.keep]:
                del self.seen[old]
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.seen, fh)
