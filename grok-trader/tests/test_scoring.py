from checks import evaluate
from scoring import BUY, SKIP, decide, score
from test_checks import CFG as CHECK_CFG, GOOD

CFG = {**CHECK_CFG, "grok_max_risk": 4}
OK = evaluate(GOOD, CFG)
BAD = evaluate({**GOOD, "mint_authority": "deployer"}, CFG)
GROK = {"risk": 3, "reasons": ["liquidity fine"], "missing_data": [], "error": None}


def test_buy_requires_all_pass_and_low_risk():
    assert decide(OK, GROK, CFG).action == BUY
    assert decide(OK, {**GROK, "risk": 4}, CFG).action == BUY
    assert decide(OK, {**GROK, "risk": 5}, CFG).action == SKIP


def test_risk_can_never_override_a_failed_rule():
    d = decide(BAD, {**GROK, "risk": 0}, CFG)
    assert d.action == SKIP and "mint_authority=FAIL" in d.reason


def test_grok_problems_mean_skip():
    assert decide(OK, None, CFG).action == SKIP
    assert decide(OK, {**GROK, "error": "XAI_API_KEY not set"}, CFG).action == SKIP
    assert decide(OK, {**GROK, "risk": None}, CFG).action == SKIP
    assert decide(OK, {**GROK, "risk": 11}, CFG).action == SKIP
    assert decide(OK, {**GROK, "reasons": []}, CFG).action == SKIP
    assert decide([], GROK, CFG).action == SKIP


def test_score_is_informational_and_monotonic():
    assert score(OK, {**GROK, "risk": 0}) == 100
    assert score(OK, {**GROK, "risk": 10}) == 60
    assert score(OK, None) == 60
    assert score(BAD, None) < 60
    assert score([], GROK) == 0
