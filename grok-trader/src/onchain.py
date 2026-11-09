"""On-chain reads (Solana JSON-RPC) and the LP / rug-signal provider (RugCheck).

Everything here can fail: the free public RPC rate-limits getTokenLargestAccounts
hard (it answers a JSON-RPC error with code 429 inside an HTTP 200). A failed
read becomes None / UNKNOWN, and UNKNOWN never passes a check.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import requests

from checks import DEFAULT_IGNORED_OWNERS, UNREADABLE
from feed import RateLimiter, backoff_sleep, get_json

log = logging.getLogger("onchain")

DEFAULT_RPC_URL = "https://api.mainnet.solana.com"  # documented public endpoint; the old -beta name serves the same node
RUGCHECK_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
_rugcheck_limiter = RateLimiter(60)  # documented anonymous limit: 1 request per second


class RpcError(Exception):
    pass


def rpc_call(session: requests.Session, url: str, method: str, params: list,
             *, timeout: float = 20, retries: int = 4):
    """One JSON-RPC call with backoff on HTTP 429/403/5xx and JSON-RPC 429 errors."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    host = urlsplit(url).hostname or "rpc"  # provider URLs carry keys: log the host, never the URL
    last, retry_after = "no attempt", None
    for attempt in range(retries):
        try:
            r = session.post(url, json=body, timeout=timeout)
            retry_after = r.headers.get("Retry-After")
            if r.headers.get("x-ratelimit-method-limit") == "0":  # the free tier does not serve this method
                raise RpcError(f"{method}: not available on this RPC tier "
                               f"(x-ratelimit-method-limit: 0); use a dedicated RPC provider")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "result" in data:
                    return data["result"]
                err = (data.get("error") if isinstance(data, dict) else None) or {}
                last = f"rpc error {err.get('code')}: {str(err.get('message'))[:120]}"
                if err.get("code") != 429:  # not a rate limit: retrying will not help
                    raise RpcError(f"{method}: {last}")
            else:
                last = f"HTTP {r.status_code} from {host}"
                if r.status_code not in (429, 403) and r.status_code < 500:
                    raise RpcError(f"{method}: {last}")
        except (requests.RequestException, ValueError) as exc:
            last = f"{type(exc).__name__} talking to {host}"  # never str(exc): it can echo the URL and key
        if attempt + 1 < retries:
            log.warning("%s failed (%s), retry %d/%d", method, last, attempt + 1, retries)
            backoff_sleep(attempt, retry_after=retry_after)
    raise RpcError(f"{method}: {last}")


SYSTEM_PROGRAM = "11111111111111111111111111111111"


def normalize_authority(value):
    """RugCheck encodes 'no authority' as null, "" or the system program; the RPC uses null."""
    if value in (None, "", SYSTEM_PROGRAM):
        return None
    return value if isinstance(value, str) else UNREADABLE


def mint_info(session, url: str, mint: str) -> dict:
    """mintAuthority / freezeAuthority (None = disabled), decimals, supply, program.

    Works for both the classic SPL Token program and Token-2022: both parse to
    data.parsed.info with the same keys.
    """
    value = rpc_call(session, url, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    if not isinstance(value, dict) or not value.get("value"):
        raise RpcError("mint account not found")
    acct = value["value"]
    parsed = (acct.get("data") or {}).get("parsed") if isinstance(acct.get("data"), dict) else None
    if not parsed or parsed.get("type") != "mint":
        return {"mint_authority": UNREADABLE, "freeze_authority": UNREADABLE,
                "decimals": None, "supply": None, "token_program": acct.get("owner")}
    info = parsed["info"]  # a disabled authority is an explicit JSON null; an absent key is unknown
    return {"mint_authority": info.get("mintAuthority", UNREADABLE),
            "freeze_authority": info.get("freezeAuthority", UNREADABLE),
            "decimals": info.get("decimals"), "supply": float(info["supply"]) if info.get("supply") else None,
            "token_program": acct.get("owner")}


def token_supply(session, url: str, mint: str) -> float:
    """Raw supply via getTokenSupply (used when the mint account could not be parsed)."""
    value = rpc_call(session, url, "getTokenSupply", [mint])
    return float(value["value"]["amount"])


def largest_holders(session, url: str, mint: str) -> list[dict]:
    """Top-20 token accounts with their owner wallets (two RPC calls)."""
    accounts = rpc_call(session, url, "getTokenLargestAccounts", [mint])["value"]
    addrs = [a["address"] for a in accounts]
    owners = rpc_call(session, url, "getMultipleAccounts", [addrs, {"encoding": "jsonParsed"}])["value"]
    out = []
    for acct, meta in zip(accounts, owners):
        parsed = ((meta or {}).get("data") or {}).get("parsed") if isinstance((meta or {}).get("data"), dict) else None
        owner = (parsed or {}).get("info", {}).get("owner")
        out.append({"address": acct["address"], "amount": acct["amount"], "owner": owner})
    return out


def rugcheck_report(session, mint: str) -> dict | None:
    """Full RugCheck report, or None when the token is unknown / provider is down."""
    try:
        report = get_json(session, RUGCHECK_REPORT_URL.format(mint=mint), limiter=_rugcheck_limiter)
    except Exception as exc:  # provider trouble must not kill the cycle
        log.warning("rugcheck failed for %s: %s", mint, exc)
        return None
    return report if isinstance(report, dict) else None  # the API can answer 200 with a bare null


def pool_owners(report: dict | None) -> set[str]:
    """Addresses RugCheck labels as AMM pools, to exclude from the holder maths."""
    known = (report or {}).get("knownAccounts") or {}
    return DEFAULT_IGNORED_OWNERS | {k for k, v in known.items() if (v or {}).get("type") == "AMM"}


def lp_from_rugcheck(report: dict | None, pair_address: str | None) -> tuple[str, float | None]:
    """(lp_status, lp_locked_pct) for the token's main market.

    Prefers the market matching the DEX Screener pair; otherwise the market with
    the most USD in it. No market data at all -> ("UNKNOWN", None).
    """
    markets = [m for m in (report or {}).get("markets") or [] if isinstance(m.get("lp"), dict)]
    if not markets:
        return "UNKNOWN", None
    match = [m for m in markets if m.get("pubkey") == pair_address]
    market = match[0] if match else max(
        markets, key=lambda m: float(m["lp"].get("baseUSD") or 0) + float(m["lp"].get("quoteUSD") or 0))
    if market.get("marketType") == "pump_fun":  # bonding curve: there is no LP token to lock
        return "UNKNOWN", None
    pct = market["lp"].get("lpLockedPct")
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return "UNKNOWN", None
    if not 0 <= pct <= 100:
        return "UNKNOWN", None
    return ("LOCKED" if pct > 0 else "UNLOCKED"), pct


def holders_from_rugcheck(report: dict | None, ignored: set[str]) -> tuple[float | None, float | None]:
    """Fallback holder shares from RugCheck's topHolders when the RPC refuses us."""
    totals: dict[str, float] = {}  # per owner wallet, like checks.holder_shares
    for h in (report or {}).get("topHolders") or []:
        if not isinstance(h, dict) or h.get("owner") in ignored or h.get("address") in ignored:
            continue
        key = h.get("owner") or h.get("address")
        if not key or h.get("pct") is None:
            continue
        try:
            pct = float(h["pct"])
        except (TypeError, ValueError):
            continue
        if 0 <= pct <= 100:
            totals[key] = totals.get(key, 0.0) + pct / 100
    if not totals:
        return None, None
    pcts = sorted(totals.values(), reverse=True)
    return pcts[0], min(1.0, sum(pcts[:10]))


def risk_summary(report: dict | None) -> dict:
    """The few RugCheck fields worth showing Grok and keeping in the log."""
    if not report:
        return {"available": False}
    return {"available": True, "score_normalised": report.get("score_normalised"),
            "total_holders": report.get("totalHolders"),
            "risks": [f"{r.get('name')} ({r.get('level')})" for r in report.get("risks") or []][:10]}


def gather(session, rpc_url: str, mint: str, snapshot: dict, use_rugcheck: bool = True) -> tuple[dict, dict | None]:
    """Collect the numbers for checks.evaluate(). Anything unreadable stays unknown.

    Returns (token, report). `token` carries holder shares, authorities and LP
    status plus `holder_source` ("rpc" or "rugcheck") and `errors` for the log.
    """
    from checks import holder_shares  # local import keeps module load order simple

    token: dict = {"liquidity_usd": snapshot.get("liquidity_usd"),
                   "pool_age_minutes": snapshot.get("pool_age_minutes"),
                   "mint_authority": UNREADABLE, "freeze_authority": UNREADABLE, "authority_source": None,
                   "supply": None, "token_program": None,
                   "largest_holder_share": None, "top10_share": None, "holder_source": None,
                   "lp_status": "UNKNOWN", "lp_locked_pct": None, "errors": []}
    report = rugcheck_report(session, mint) if use_rugcheck else None
    ignored = pool_owners(report)

    try:
        token.update(mint_info(session, rpc_url, mint))
        if token["mint_authority"] != UNREADABLE:
            token["authority_source"] = "rpc"
    except RpcError as exc:
        token["errors"].append(str(exc))
        t = report.get("token") if report else None
        if isinstance(t, dict):  # fallback: RugCheck read the mint too. Absent key = unknown, never "disabled".
            token.update({
                "mint_authority": normalize_authority(t["mintAuthority"]) if "mintAuthority" in t else UNREADABLE,
                "freeze_authority": normalize_authority(t["freezeAuthority"]) if "freezeAuthority" in t else UNREADABLE,
                "supply": t.get("supply"), "token_program": report.get("tokenProgram")})
            if token["mint_authority"] != UNREADABLE:
                token["authority_source"] = "rugcheck"
    if token.get("supply") is None:
        try:
            token["supply"] = token_supply(session, rpc_url, mint)
        except RpcError as exc:
            token["errors"].append(str(exc))

    try:
        holders = largest_holders(session, rpc_url, mint)
        token["largest_holder_share"], token["top10_share"] = holder_shares(holders, token.get("supply"), ignored)
        token["holder_source"] = "rpc" if token["largest_holder_share"] is not None else None
    except RpcError as exc:
        token["errors"].append(str(exc))
    if token["largest_holder_share"] is None and report:
        token["largest_holder_share"], token["top10_share"] = holders_from_rugcheck(report, ignored)
        token["holder_source"] = "rugcheck" if token["largest_holder_share"] is not None else None

    token["lp_status"], token["lp_locked_pct"] = lp_from_rugcheck(report, snapshot.get("pair_address"))
    return token, report
