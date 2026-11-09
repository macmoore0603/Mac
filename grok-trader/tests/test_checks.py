import pytest

import checks
from checks import PASS, FAIL, UNKNOWN, UNREADABLE, evaluate, all_pass, holder_shares, not_passed

CFG = dict(min_liquidity_usd=25000, min_pool_age_minutes=10, max_single_holder=0.10, max_top10_share=0.40,
           require_mint_disabled=True, require_freeze_disabled=True, require_lp_locked=True, min_lp_locked_pct=90)
GOOD = dict(liquidity_usd=50000.0, pool_age_minutes=30.0, largest_holder_share=0.05, top10_share=0.30,
            mint_authority=None, freeze_authority=None, lp_status="LOCKED", lp_locked_pct=100.0)


def status(name, token, cfg=CFG):
    return {r.name: r.status for r in evaluate(token, cfg)}[name]


def test_good_token_passes_every_rule():
    results = evaluate(GOOD, CFG)
    assert len(results) == 7 and all_pass(results) and not_passed(results) == []


def test_unknown_never_counts_as_pass():
    for key in GOOD:
        unknown = UNREADABLE if key in ("mint_authority", "freeze_authority") else None
        results = evaluate({**GOOD, key: unknown}, CFG)
        assert not all_pass(results), key
        assert status("lp_locked", {**GOOD, "lp_locked_pct": None}) == UNKNOWN
    assert status("mint_authority", {**GOOD, "mint_authority": UNREADABLE}) == UNKNOWN
    assert status("liquidity", {k: v for k, v in GOOD.items() if k != "liquidity_usd"}) == UNKNOWN


def test_boundaries_are_inclusive_on_the_safe_side():
    assert status("liquidity", {**GOOD, "liquidity_usd": 25000}) == PASS
    assert status("liquidity", {**GOOD, "liquidity_usd": 24999.99}) == FAIL
    assert status("pool_age", {**GOOD, "pool_age_minutes": 10}) == PASS
    assert status("pool_age", {**GOOD, "pool_age_minutes": 9.9}) == FAIL
    assert status("largest_holder", {**GOOD, "largest_holder_share": 0.10}) == PASS
    assert status("largest_holder", {**GOOD, "largest_holder_share": 0.1001}) == FAIL
    assert status("top10_share", {**GOOD, "top10_share": 0.40}) == PASS
    assert status("top10_share", {**GOOD, "top10_share": 0.41}) == FAIL


def test_authorities():
    assert status("mint_authority", {**GOOD, "mint_authority": "SomeWallet"}) == FAIL
    assert status("freeze_authority", {**GOOD, "freeze_authority": "SomeWallet"}) == FAIL
    relaxed = {**CFG, "require_mint_disabled": False}
    assert status("mint_authority", {**GOOD, "mint_authority": "SomeWallet"}, relaxed) == PASS


def test_lp_rule():
    assert status("lp_locked", {**GOOD, "lp_status": "UNLOCKED", "lp_locked_pct": 0}) == FAIL
    assert status("lp_locked", {**GOOD, "lp_status": "LOCKED", "lp_locked_pct": 50}) == FAIL
    assert status("lp_locked", {**GOOD, "lp_status": "BURNED", "lp_locked_pct": 95}) == PASS
    assert status("lp_locked", {**GOOD, "lp_status": "UNKNOWN", "lp_locked_pct": None}) == UNKNOWN
    assert status("lp_locked", {**GOOD, "lp_status": None}) == UNKNOWN
    assert status("lp_locked", {**GOOD, "lp_status": "UNKNOWN"}, {**CFG, "require_lp_locked": False}) == PASS


def test_holder_shares_exclude_pools_and_handle_bad_supply():
    pool = next(iter(checks.DEFAULT_IGNORED_OWNERS))
    holders = [{"address": "a", "amount": "500", "owner": pool},
               {"address": "b", "amount": "80", "owner": "wallet1"},
               {"address": "c", "amount": "20", "owner": "wallet2"}]
    largest, top10 = holder_shares(holders, 1000)
    assert largest == 0.08 and top10 == 0.10
    assert holder_shares(holders, None) == (None, None)
    assert holder_shares([], 1000) == (None, None)
    assert holder_shares([holders[0]], 1000) == (None, None)  # only a pool: nothing to judge
    assert holder_shares(holders, 1000, ignored_owners=set()) == (0.5, 0.6)


def test_not_passed_names_statuses():
    results = evaluate({**GOOD, "liquidity_usd": 1, "lp_status": "UNKNOWN"}, CFG)
    assert not_passed(results) == ["liquidity=FAIL", "lp_locked=UNKNOWN"]
    assert results[0].to_dict()["status"] == FAIL


def test_holder_shares_sum_token_accounts_per_wallet():
    split = [{"address": f"a{i}", "amount": "19", "owner": "WHALE"} for i in range(20)]  # 38% in 20 accounts
    pool = [{"address": "pool", "amount": "620", "owner": next(iter(checks.DEFAULT_IGNORED_OWNERS))}]
    largest, top10 = holder_shares(split + pool, 1000)
    assert largest == pytest.approx(0.38) and top10 == pytest.approx(0.38)
    unknown_owner = [{"address": "x", "amount": "50", "owner": None}, {"address": "y", "amount": "50", "owner": None}]
    assert holder_shares(unknown_owner, 1000) == (0.05, 0.10)  # no owner: each account is its own wallet
    assert holder_shares([{"address": "z", "amount": "abc", "owner": "w"}], 1000) == (None, None)
