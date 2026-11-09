import pytest

import onchain
from checks import UNREADABLE
from conftest import FakeResponse, FakeSession

MINT = "So11111111111111111111111111111111111111112"
RPC = "https://rpc.test"


def rpc_ok(result):
    return FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "result": result})


def rpc_err(code=429, headers=None):
    return FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": "x"}}, headers=headers)


MINT_ACCOUNT = {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"program": "spl-token",
                "parsed": {"type": "mint", "info": {"mintAuthority": None, "freezeAuthority": "Frz", "decimals": 6,
                                                    "supply": "1000", "isInitialized": True}}}}}
REPORT = {"token": {"mintAuthority": None, "freezeAuthority": None, "supply": 1000},
          "tokenProgram": "Tokenz", "score_normalised": 3, "rugged": False, "totalHolders": 77,
          "risks": [{"name": "Low liquidity", "level": "warn"}],
          "knownAccounts": {"POOL": {"name": "Pump Fun", "type": "AMM"}, "CRE": {"name": "Creator", "type": "CREATOR"}},
          "markets": [{"pubkey": "PAIR1", "lp": {"lpLockedPct": 100, "baseUSD": 10, "quoteUSD": 10}},
                      {"pubkey": "PAIR2", "lp": {"lpLockedPct": 0, "baseUSD": 5000, "quoteUSD": 5000}}],
          "topHolders": [{"address": "a1", "owner": "POOL", "pct": 51.9}, {"address": "a2", "owner": "w1", "pct": 4.5},
                         {"address": "a3", "owner": "CRE", "pct": 3.0}]}


def test_rpc_call_retries_json_rpc_429_then_raises():
    s = FakeSession({("POST", RPC): [rpc_err(), rpc_ok({"v": 1})]})
    assert onchain.rpc_call(s, RPC, "m", []) == {"v": 1} and len(s.calls) == 2
    s = FakeSession({("POST", RPC): [rpc_err()]})
    with pytest.raises(onchain.RpcError):
        onchain.rpc_call(s, RPC, "m", [], retries=3)
    assert len(s.calls) == 3


def test_rpc_call_fails_fast_when_method_is_not_served_on_this_tier():
    s = FakeSession({("POST", RPC): [rpc_err(headers={"x-ratelimit-method-limit": "0"})]})
    with pytest.raises(onchain.RpcError, match="not available on this RPC tier"):
        onchain.rpc_call(s, RPC, "getTokenLargestAccounts", [MINT])
    assert len(s.calls) == 1
    with pytest.raises(onchain.RpcError):  # other JSON-RPC errors are final too
        onchain.rpc_call(FakeSession({("POST", RPC): [rpc_err(code=-32602)]}), RPC, "m", [])


def test_mint_info_parses_both_programs_and_marks_unparsed_unreadable():
    info = onchain.mint_info(FakeSession({("POST", RPC): [rpc_ok(MINT_ACCOUNT)]}), RPC, MINT)
    assert info["mint_authority"] is None and info["freeze_authority"] == "Frz" and info["supply"] == 1000.0
    raw = {"value": {"owner": "X", "data": ["AAAA", "base64"]}}
    info = onchain.mint_info(FakeSession({("POST", RPC): [rpc_ok(raw)]}), RPC, MINT)
    assert info["mint_authority"] == UNREADABLE and info["freeze_authority"] == UNREADABLE
    with pytest.raises(onchain.RpcError):
        onchain.mint_info(FakeSession({("POST", RPC): [rpc_ok({"value": None})]}), RPC, MINT)


def test_largest_holders_joins_owners():
    largest = {"value": [{"address": "a1", "amount": "600"}, {"address": "a2", "amount": "100"}]}
    owners = {"value": [{"data": {"parsed": {"info": {"owner": "POOL"}}}}, None]}
    s = FakeSession({("POST", RPC): [rpc_ok(largest), rpc_ok(owners)]})
    assert onchain.largest_holders(s, RPC, MINT) == [{"address": "a1", "amount": "600", "owner": "POOL"},
                                                     {"address": "a2", "amount": "100", "owner": None}]


def test_rugcheck_helpers():
    assert onchain.pool_owners(REPORT) >= {"POOL"} and "CRE" not in onchain.pool_owners(REPORT)
    assert onchain.lp_from_rugcheck(REPORT, "PAIR1") == ("LOCKED", 100.0)
    assert onchain.lp_from_rugcheck(REPORT, "nope") == ("UNLOCKED", 0.0)  # falls back to the biggest market
    assert onchain.lp_from_rugcheck(None, "x") == ("UNKNOWN", None)
    assert onchain.lp_from_rugcheck({"markets": [{"pubkey": "p", "lp": {}}]}, "p") == ("UNKNOWN", None)
    bonding = {"markets": [{"pubkey": "p", "marketType": "pump_fun", "lp": {"lpLockedPct": 100}}]}
    assert onchain.lp_from_rugcheck(bonding, "p") == ("UNKNOWN", None)  # bonding curve: no LP to lock
    assert onchain.lp_from_rugcheck({"markets": [{"pubkey": "p", "lp": {"lpLockedPct": "100"}}]}, "p") == ("LOCKED", 100.0)
    assert onchain.lp_from_rugcheck({"markets": [{"pubkey": "p", "lp": {"lpLockedPct": "lots"}}]}, "p") == ("UNKNOWN", None)
    assert onchain.lp_from_rugcheck({"markets": [{"pubkey": "p", "lp": {"lpLockedPct": 250}}]}, "p") == ("UNKNOWN", None)
    weird = {"topHolders": [{"owner": "w", "pct": "abc"}, {"owner": "w2", "pct": 150}, {"owner": "w3", "pct": None}]}
    assert onchain.holders_from_rugcheck(weird, set()) == (None, None)
    largest, top10 = onchain.holders_from_rugcheck(REPORT, onchain.pool_owners(REPORT))
    assert largest == pytest.approx(0.045) and top10 == pytest.approx(0.075)
    assert onchain.holders_from_rugcheck(None, set()) == (None, None)
    assert onchain.risk_summary(REPORT)["risks"] == ["Low liquidity (warn)"]
    assert onchain.risk_summary(None) == {"available": False}


def test_gather_falls_back_to_rugcheck_holders_when_rpc_refuses():
    s = FakeSession({("GET", "rugcheck"): [FakeResponse(200, REPORT)],
                     ("POST", RPC): [rpc_ok(MINT_ACCOUNT), rpc_err(headers={"x-ratelimit-method-limit": "0"})]})
    token, report = onchain.gather(s, RPC, MINT, {"liquidity_usd": 5.0, "pool_age_minutes": 2.0, "pair_address": "PAIR1"})
    assert report is REPORT and token["mint_authority"] is None and token["freeze_authority"] == "Frz"
    assert token["holder_source"] == "rugcheck" and token["largest_holder_share"] == pytest.approx(0.045)
    assert token["authority_source"] == "rpc"
    assert token["lp_status"] == "LOCKED" and token["lp_locked_pct"] == 100.0
    assert token["liquidity_usd"] == 5.0 and any("not available" in e for e in token["errors"])


def test_gather_with_everything_down_stays_unknown():
    s = FakeSession({("GET", "rugcheck"): [FakeResponse(503)], ("POST", RPC): [FakeResponse(503)]})
    token, report = onchain.gather(s, RPC, MINT, {"pair_address": None})
    assert report is None and token["mint_authority"] == UNREADABLE and token["largest_holder_share"] is None
    assert token["lp_status"] == "UNKNOWN" and token["holder_source"] is None and len(token["errors"]) == 3
    assert token["authority_source"] is None and any("getTokenSupply" in e for e in token["errors"])


def test_rugcheck_report_must_be_an_object():
    assert onchain.rugcheck_report(FakeSession({("GET", "rugcheck"): [FakeResponse(200, None, "null")]}), MINT) is None
    assert onchain.rugcheck_report(FakeSession({("GET", "rugcheck"): [FakeResponse(200, [1, 2])]}), MINT) is None
    assert onchain.rugcheck_report(FakeSession({("GET", "rugcheck"): [FakeResponse(400, {"error": "invalid token mint"})]}), MINT) is None


def test_normalize_authority_treats_null_empty_and_system_program_as_disabled():
    for v in (None, "", onchain.SYSTEM_PROGRAM):
        assert onchain.normalize_authority(v) is None
    assert onchain.normalize_authority("SomeWallet") == "SomeWallet"
    assert onchain.normalize_authority({"lamports": 1}) == UNREADABLE
    fallback = {**REPORT, "token": {"mintAuthority": onchain.SYSTEM_PROGRAM, "freezeAuthority": "", "supply": 1}}
    s = FakeSession({("GET", "rugcheck"): [FakeResponse(200, fallback)], ("POST", RPC): [FakeResponse(503)]})
    token, _ = onchain.gather(s, RPC, MINT, {"pair_address": "PAIR1"})
    assert token["mint_authority"] is None and token["freeze_authority"] is None


def test_rpc_call_passes_retry_after_to_backoff(monkeypatch):
    import feed
    seen = []
    monkeypatch.setattr(feed, "backoff_sleep", lambda attempt, **kw: seen.append(kw.get("retry_after")))
    monkeypatch.setattr(onchain, "backoff_sleep", lambda attempt, **kw: seen.append(kw.get("retry_after")))
    s = FakeSession({("POST", RPC): [FakeResponse(429, headers={"Retry-After": "10"}), rpc_ok({"v": 1})]})
    assert onchain.rpc_call(s, RPC, "m", []) == {"v": 1} and seen == ["10"]


def test_http_429_with_method_limit_zero_fails_fast_too():
    s = FakeSession({("POST", RPC): [FakeResponse(429, {"error": {"code": 429}}, headers={"x-ratelimit-method-limit": "0", "Retry-After": "10"})]})
    with pytest.raises(onchain.RpcError, match="not available on this RPC tier"):
        onchain.rpc_call(s, RPC, "getTokenLargestAccounts", [MINT])
    assert len(s.calls) == 1


def test_rpc_errors_never_echo_the_provider_url():
    import requests
    secret_url = "https://rpc.example.com/?api-key=sk_live_SUPERSECRET"
    boom = requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {secret_url}")
    s = FakeSession({("POST", "rpc.example.com"): [boom]})
    with pytest.raises(onchain.RpcError) as info:
        onchain.rpc_call(s, secret_url, "getAccountInfo", [MINT], retries=2)
    assert "SUPERSECRET" not in str(info.value) and "rpc.example.com" in str(info.value)


def test_rugcheck_fallback_without_authority_keys_stays_unknown():
    partial = {**REPORT, "token": {"supply": 1000, "decimals": 6}}
    s = FakeSession({("GET", "rugcheck"): [FakeResponse(200, partial)], ("POST", RPC): [FakeResponse(503)]})
    token, _ = onchain.gather(s, RPC, MINT, {"pair_address": "PAIR1"})
    assert token["mint_authority"] == UNREADABLE and token["freeze_authority"] == UNREADABLE
    assert token["authority_source"] is None
    labelled = {**REPORT, "token": {"mintAuthority": None, "freezeAuthority": None, "supply": 1000}}
    s = FakeSession({("GET", "rugcheck"): [FakeResponse(200, labelled)], ("POST", RPC): [FakeResponse(503)]})
    token, _ = onchain.gather(s, RPC, MINT, {"pair_address": "PAIR1"})
    assert token["mint_authority"] is None and token["authority_source"] == "rugcheck"


def test_mint_info_without_authority_keys_is_unreadable_and_supply_falls_back():
    odd = {"value": {"owner": "Tokenkeg", "data": {"parsed": {"type": "mint", "info": {"decimals": 6}}}}}
    s = FakeSession({("GET", "rugcheck"): [FakeResponse(404)],
                     ("POST", RPC): [rpc_ok(odd), rpc_ok({"value": {"amount": "5000", "decimals": 6}}),
                                     rpc_err(headers={"x-ratelimit-method-limit": "0"})]})
    token, _ = onchain.gather(s, RPC, MINT, {"pair_address": None})
    assert token["mint_authority"] == UNREADABLE and token["supply"] == 5000.0
    assert [c[2]["json"]["method"] for c in s.calls if c[0] == "POST"][:2] == ["getAccountInfo", "getTokenSupply"]


def test_rugcheck_holders_are_summed_per_wallet():
    split = {"topHolders": [{"address": f"a{i}", "owner": "WHALE", "pct": 3.0} for i in range(5)]
             + [{"address": "b", "owner": "small", "pct": 2.0}]}
    largest, top10 = onchain.holders_from_rugcheck(split, set())
    assert largest == pytest.approx(0.15) and top10 == pytest.approx(0.17)
