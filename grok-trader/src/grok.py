"""Ask Grok (xAI) for a second opinion on a token that already passed every rule.

The reply is DATA. We parse one integer and two string lists out of it and
nothing else; nothing in Grok's text is ever executed or used as an instruction.
"""
from __future__ import annotations

import json
import logging
import re

import requests

from feed import backoff_sleep

log = logging.getLogger("grok")

XAI_URL = "https://api.x.ai/v1/chat/completions"

# xAI structured outputs: response_format.type "json_schema" is the documented primary
# mode; "json_object" is also accepted. If a model rejects it (400) we retry as text.
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "risk_read", "strict": True, "schema": {
    "type": "object", "additionalProperties": False,
    "properties": {"risk": {"type": "integer", "minimum": 0, "maximum": 10},
                   "reasons": {"type": "array", "items": {"type": "string"}},
                   "missing_data": {"type": "array", "items": {"type": "string"}}},
    "required": ["risk", "reasons", "missing_data"]}}}

SYSTEM_PROMPT = (
    "You are a risk analyst for brand-new Solana tokens. You receive raw on-chain and "
    "market numbers for one token. Reply ONLY with a JSON object of the form "
    '{"risk": <integer 0-10>, "reasons": [<short strings, each citing a number you were given>], '
    '"missing_data": [<short strings naming data you lacked>]}. '
    "10 means almost certainly a rug pull or untradeable, 0 means no red flags in the data given. "
    "Judge only the numbers provided. Missing or unknown data raises the risk. No prose outside the JSON."
)


def build_payload(mint: str, snapshot: dict, token: dict, checks: list, rug: dict) -> dict:
    """The raw numbers Grok sees. Everything is explicit so it cannot invent context."""
    return {
        "mint": mint,
        "market": {k: snapshot.get(k) for k in ("symbol", "name", "dex_id", "quote_symbol", "price_usd",
                                               "liquidity_usd", "pool_age_minutes", "fdv_usd",
                                               "volume_h24_usd", "buys_h1", "sells_h1")},
        "holders": {k: token.get(k) for k in ("largest_holder_share", "top10_share", "holder_source")},
        "authorities": {"mint_authority": token.get("mint_authority"),
                        "freeze_authority": token.get("freeze_authority"),
                        "token_program": token.get("token_program")},
        "liquidity_lock": {"status": token.get("lp_status"), "locked_pct": token.get("lp_locked_pct")},
        "rugcheck": rug,
        "hard_checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
    }


def parse_reply(text: str) -> dict:
    """Pull {risk, reasons, missing_data} out of the model text, strictly."""
    if not text:
        return {"risk": None, "reasons": [], "missing_data": [], "error": "empty reply"}
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"risk": None, "reasons": [], "missing_data": [], "error": "no JSON object in reply"}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        return {"risk": None, "reasons": [], "missing_data": [], "error": f"bad JSON: {exc}"}
    risk = obj.get("risk") if isinstance(obj, dict) else None
    if isinstance(risk, bool) or not isinstance(risk, (int, float)) or not 0 <= risk <= 10:
        return {"risk": None, "reasons": [], "missing_data": [], "error": "risk missing or not 0-10"}

    def strings(v):
        return [str(x)[:300] for x in v if str(x).strip()][:10] if isinstance(v, list) else []

    return {"risk": int(round(risk)), "reasons": strings(obj.get("reasons")),
            "missing_data": strings(obj.get("missing_data")), "error": None}


def ask_grok(session: requests.Session, api_key: str | None, model: str, payload: dict,
             *, timeout: float = 45, retries: int = 3) -> dict:
    """Return parse_reply(...) plus model/usage/raw. Never raises; errors are data."""
    base = {"risk": None, "reasons": [], "missing_data": [], "model": model, "usage": None, "raw": None}
    if not api_key or api_key == "your_key_here":
        return {**base, "error": "XAI_API_KEY not set"}
    body = {
        "model": model, "temperature": 0,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": json.dumps(payload, default=str)}],
        "response_format": RESPONSE_FORMAT,
    }
    last = "no attempt"
    for attempt in range(retries):
        try:
            r = session.post(XAI_URL, json=body, timeout=timeout,
                             headers={"Authorization": f"Bearer {api_key}"})
            if r.status_code == 200:
                data = r.json()
                message = (data.get("choices") or [{}])[0].get("message") or {}
                text = message.get("content") or ""
                parsed = parse_reply(text)
                if parsed["error"] and message.get("refusal"):
                    parsed["error"] = f"model refused: {str(message['refusal'])[:200]}"
                return {**base, **parsed, "usage": data.get("usage"), "raw": text[:2000]}
            if r.status_code == 401:
                return {**base, "error": "xAI rejected the API key (401)"}
            if r.status_code == 400 and "response_format" in body:
                body.pop("response_format")  # older models: retry as plain text
                last = "400, retrying without response_format"
            else:
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code != 429 and r.status_code < 500:
                    return {**base, "error": last}
        except (requests.RequestException, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        log.warning("grok call failed (%s), retry %d/%d", last, attempt + 1, retries)
        backoff_sleep(attempt)
    return {**base, "error": f"grok unavailable after {retries} tries: {last}"}
