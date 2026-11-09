import pytest

import feed
from conftest import FakeResponse, FakeSession


def pair(liq, created=None, price="0.5", addr="P"):
    return {"pairAddress": addr, "dexId": "raydium", "priceUsd": price, "pairCreatedAt": created,
            "liquidity": {"usd": liq} if liq is not None else None,
            "baseToken": {"symbol": "TOK", "name": "Token"}, "quoteToken": {"symbol": "SOL"},
            "volume": {"h24": 100}, "txns": {"h1": {"buys": 3, "sells": 1}}}


def test_main_pool_prefers_base_side_pools_for_the_token():
    quote_side = {**pair(999, addr="q", price="150"), "baseToken": {"address": "SOL", "symbol": "SOL"},
                  "quoteToken": {"address": "MINT", "symbol": "TOK"}}
    base_side = {**pair(10, addr="b", price="0.01"), "baseToken": {"address": "mint", "symbol": "TOK"}}
    assert feed.main_pool([quote_side, base_side], "MINT")["pairAddress"] == "b"  # case-insensitive match
    assert feed.main_pool([quote_side], "MINT")["pairAddress"] == "q"  # nothing better: fall back


def test_main_pool_prefers_most_liquidity_and_penalises_unknown():
    assert feed.main_pool([pair(10, addr="a"), pair(50, addr="b"), pair(None, addr="c")])["pairAddress"] == "b"
    assert feed.main_pool([pair(None, addr="a"), pair(None, addr="b")])["pairAddress"] == "a"
    assert feed.main_pool([]) is None


def test_pool_snapshot_flattens_and_computes_age():
    now_ms = 1_700_000_000_000
    snap = feed.pool_snapshot(pair(1234.5, created=now_ms - 90 * 60_000), now_ms)
    assert snap["liquidity_usd"] == 1234.5 and snap["pool_age_minutes"] == pytest.approx(90)
    assert snap["price_usd"] == 0.5 and snap["symbol"] == "TOK" and snap["buys_h1"] == 3
    assert feed.pool_snapshot(pair(None), now_ms)["liquidity_usd"] is None
    assert feed.pool_snapshot(pair(5), now_ms)["pool_age_minutes"] is None
    assert feed.pool_snapshot(None, now_ms)["price_usd"] is None


def test_latest_solana_tokens_filters_and_dedups():
    data = [{"chainId": "solana", "tokenAddress": "A"}, {"chainId": "ethereum", "tokenAddress": "E"},
            {"chainId": "solana", "tokenAddress": "A"}, {"chainId": "solana", "tokenAddress": "B"}, {"chainId": "solana"}]
    s = FakeSession({("GET", "token-profiles/latest"): [FakeResponse(200, data)]})
    assert feed.latest_solana_tokens(s) == ["A", "B"]


def test_get_json_retries_on_429_and_gives_up():
    s = FakeSession({("GET", "x"): [FakeResponse(429), FakeResponse(200, {"ok": 1})]})
    assert feed.get_json(s, "https://x/") == {"ok": 1} and len(s.calls) == 2
    s = FakeSession({("GET", "x"): [FakeResponse(503)]})
    with pytest.raises(feed.FeedError):
        feed.get_json(s, "https://x/", retries=3)
    assert len(s.calls) == 3
    assert feed.get_json(FakeSession({("GET", "x"): [FakeResponse(404)]}), "https://x/") is None


def test_current_price_uses_main_pool():
    s = FakeSession({("GET", "token-pairs/v1/solana/M"): [FakeResponse(200, [pair(1, price="2"), pair(9, price="3")])]})
    assert feed.current_price(s, "M") == 3.0


def test_seen_cache_persists_and_prunes(tmp_path):
    path = str(tmp_path / "seen.json")
    c = feed.SeenCache(path, keep=3)
    for m in "abcd":
        c.add(m, "t")
    assert "a" not in c and "d" in c and len(c.seen) == 3
    assert "d" in feed.SeenCache(path)


def test_rate_limiter_spacing():
    assert feed.RateLimiter(60).interval == pytest.approx(1.0)
    assert feed.RateLimiter(0).interval == 60.0
