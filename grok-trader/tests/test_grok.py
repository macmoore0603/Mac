import json

import grok
from conftest import FakeResponse, FakeSession


def test_parse_reply_accepts_strict_json_and_rounds():
    r = grok.parse_reply('{"risk": 3.6, "reasons": ["a", "b"], "missing_data": []}')
    assert r == {"risk": 4, "reasons": ["a", "b"], "missing_data": [], "error": None}
    r = grok.parse_reply('Sure! Here it is: {"risk": 2, "reasons": ["x"]} hope that helps')
    assert r["risk"] == 2 and r["error"] is None


def test_parse_reply_rejects_garbage():
    assert grok.parse_reply("").error if False else grok.parse_reply("")["error"] == "empty reply"
    assert grok.parse_reply("no json here")["risk"] is None
    assert grok.parse_reply('{"risk": "high", "reasons": ["x"]}')["risk"] is None
    assert grok.parse_reply('{"risk": 11, "reasons": ["x"]}')["risk"] is None
    assert grok.parse_reply('{"risk": true, "reasons": ["x"]}')["risk"] is None
    assert grok.parse_reply('{"risk": 1, "reasons": "not a list"}')["reasons"] == []
    long = grok.parse_reply(json.dumps({"risk": 1, "reasons": ["y" * 1000] * 20}))
    assert len(long["reasons"]) == 10 and len(long["reasons"][0]) == 300


def test_missing_key_never_calls_the_network():
    s = FakeSession({})
    r = grok.ask_grok(s, None, "grok-x", {"mint": "m"})
    assert r["error"] == "XAI_API_KEY not set" and r["risk"] is None and s.calls == []
    assert grok.ask_grok(s, "your_key_here", "grok-x", {})["error"] == "XAI_API_KEY not set"


def test_success_returns_parsed_reply_and_usage():
    body = {"choices": [{"message": {"content": '{"risk": 2, "reasons": ["ok"], "missing_data": ["lp"]}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    s = FakeSession({("POST", "api.x.ai"): [FakeResponse(200, body)]})
    r = grok.ask_grok(s, "key", "grok-x", {"mint": "m"})
    assert r["risk"] == 2 and r["reasons"] == ["ok"] and r["usage"]["prompt_tokens"] == 10 and r["error"] is None
    sent = s.calls[0][2]["json"]
    assert sent["model"] == "grok-x" and sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["schema"]["required"] == ["risk", "reasons", "missing_data"]
    assert s.calls[0][2]["headers"]["Authorization"] == "Bearer key"


def test_401_is_final_and_400_retries_without_response_format():
    s = FakeSession({("POST", "api.x.ai"): [FakeResponse(401, {}, "bad key")]})
    assert "401" in grok.ask_grok(s, "key", "m", {})["error"] and len(s.calls) == 1
    ok = {"choices": [{"message": {"content": '{"risk": 1, "reasons": ["r"]}'}}]}
    s = FakeSession({("POST", "api.x.ai"): [FakeResponse(400, {}, "unsupported response_format"), FakeResponse(200, ok)]})
    r = grok.ask_grok(s, "key", "m", {})
    assert r["risk"] == 1 and "response_format" not in s.calls[1][2]["json"]


def test_rate_limit_then_give_up_is_data_not_exception():
    s = FakeSession({("POST", "api.x.ai"): [FakeResponse(429, {}, "slow down")]})
    r = grok.ask_grok(s, "key", "m", {}, retries=2)
    assert r["risk"] is None and "unavailable" in r["error"] and len(s.calls) == 2


def test_refusal_is_reported_as_error():
    body = {"choices": [{"message": {"content": None, "refusal": "I cannot help with that"}}]}
    r = grok.ask_grok(FakeSession({("POST", "api.x.ai"): [FakeResponse(200, body)]}), "key", "m", {})
    assert r["risk"] is None and r["error"].startswith("model refused")
