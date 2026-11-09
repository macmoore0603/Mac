"""Hard PASS/FAIL rules. No AI here: cheap arithmetic that drops obvious junk.

Every check returns PASS, FAIL or UNKNOWN. UNKNOWN means "we could not read the
data", and it NEVER counts as a pass. The scorer treats anything that is not
PASS as a reason to skip the token.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"

# Sentinel for "we could not read this authority" (distinct from None = disabled).
UNREADABLE = "UNKNOWN"

# Token accounts owned by these addresses are liquidity pools or burn sinks, not
# wallets that can dump on you, so they are excluded from the holder maths.
DEFAULT_IGNORED_OWNERS = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4 authority
    "1nc1nerator11111111111111111111111111111111",  # Solana burn address
}


@dataclass
class CheckResult:
    name: str
    status: str
    value: object
    rule: str
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def holder_shares(holders: list[dict], supply: float | None,
                  ignored_owners: set[str] | None = None) -> tuple[float | None, float | None]:
    """Return (largest_single_share, top10_share) as fractions of supply.

    `holders` items need an "amount" (raw integer string or number) and may carry
    an "owner" wallet. Amounts are summed per owner wallet, so a deployer who
    splits a bag across many token accounts is still one holder. Accounts owned
    by a liquidity pool or burn address are skipped. (None, None) = no data.
    """
    try:
        supply = float(supply) if supply is not None else 0.0
    except (TypeError, ValueError):
        return None, None
    if not holders or supply <= 0:
        return None, None
    ignored = DEFAULT_IGNORED_OWNERS if ignored_owners is None else ignored_owners
    totals: dict[str, float] = {}  # one wallet may spread its bag over many token accounts
    for h in holders:
        owner = h.get("owner") or h.get("address")
        if not owner or owner in ignored or h.get("address") in ignored:
            continue
        try:
            amount = float(h.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            totals[owner] = totals.get(owner, 0.0) + amount
    amounts = sorted(totals.values(), reverse=True)
    if not amounts:
        return None, None
    return amounts[0] / supply, min(1.0, sum(amounts[:10]) / supply)


def evaluate(token: dict, cfg: dict) -> list[CheckResult]:
    """Run every hard rule over one token's gathered numbers.

    Expected keys in `token` (missing or None means unknown):
      liquidity_usd, pool_age_minutes, largest_holder_share, top10_share,
      mint_authority, freeze_authority (None = disabled, UNREADABLE = unknown),
      lp_status ("LOCKED" | "BURNED" | "UNLOCKED" | "UNKNOWN"), lp_locked_pct.
    """
    results: list[CheckResult] = []

    def numeric(name, value, rule, bad, fmt):
        if value is None:
            results.append(CheckResult(name, UNKNOWN, None, rule, "no data"))
        elif bad(value):
            results.append(CheckResult(name, FAIL, value, rule, fmt(value)))
        else:
            results.append(CheckResult(name, PASS, value, rule, fmt(value)))

    numeric("liquidity", token.get("liquidity_usd"),
            f">= ${cfg['min_liquidity_usd']:,.0f}",
            lambda v: v < cfg["min_liquidity_usd"], lambda v: f"${v:,.0f}")
    numeric("pool_age", token.get("pool_age_minutes"),
            f">= {cfg['min_pool_age_minutes']} min",
            lambda v: v < cfg["min_pool_age_minutes"], lambda v: f"{v:.0f} min")
    numeric("largest_holder", token.get("largest_holder_share"),
            f"<= {cfg['max_single_holder']:.0%}",
            lambda v: v > cfg["max_single_holder"], lambda v: f"{v:.1%}")
    numeric("top10_share", token.get("top10_share"),
            f"<= {cfg['max_top10_share']:.0%}",
            lambda v: v > cfg["max_top10_share"], lambda v: f"{v:.1%}")

    for name, key, flag in (("mint_authority", "mint_authority", "require_mint_disabled"),
                            ("freeze_authority", "freeze_authority", "require_freeze_disabled")):
        value = token.get(key, UNREADABLE)
        if not cfg.get(flag, True):
            results.append(CheckResult(name, PASS, value, "not required", "check disabled in config"))
        elif value == UNREADABLE:
            results.append(CheckResult(name, UNKNOWN, None, "disabled (null)", "could not read mint"))
        elif value is None:
            results.append(CheckResult(name, PASS, None, "disabled (null)", "disabled"))
        else:
            results.append(CheckResult(name, FAIL, value, "disabled (null)", "still enabled"))

    lp_status = (token.get("lp_status") or "UNKNOWN").upper()
    lp_pct = token.get("lp_locked_pct")
    min_pct = cfg.get("min_lp_locked_pct", 90)
    rule = f"locked/burned >= {min_pct}%"
    if not cfg.get("require_lp_locked", True):
        results.append(CheckResult("lp_locked", PASS, lp_status, "not required", "check disabled in config"))
    elif lp_status in ("LOCKED", "BURNED") and lp_pct is not None and lp_pct >= min_pct:
        results.append(CheckResult("lp_locked", PASS, lp_pct, rule, f"{lp_status.lower()} {lp_pct:.0f}%"))
    elif lp_status == "UNLOCKED" or (lp_status in ("LOCKED", "BURNED") and lp_pct is not None):
        results.append(CheckResult("lp_locked", FAIL, lp_pct, rule,
                                   f"{lp_status.lower()} {lp_pct if lp_pct is None else round(lp_pct)}%"))
    else:
        results.append(CheckResult("lp_locked", UNKNOWN, None, rule, "no LP data (UNKNOWN counts as FAIL)"))
    return results


def all_pass(results: list[CheckResult]) -> bool:
    return bool(results) and all(r.status == PASS for r in results)


def not_passed(results: list[CheckResult]) -> list[str]:
    """Names of checks that did not PASS, with their status, for the log line."""
    return [f"{r.name}={r.status}" for r in results if r.status != PASS]
