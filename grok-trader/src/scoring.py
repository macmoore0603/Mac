"""Combine the hard checks and Grok's risk read into one decision.

The score is a convenience number for the log and dashboard. The DECISION is a
strict gate: every hard check must PASS and Grok's risk must be at or below
grok_max_risk. No score, however high, can override a failed rule.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from checks import CheckResult, PASS, not_passed

BUY, SKIP = "buy", "skip"


@dataclass
class Decision:
    action: str
    score: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def score(checks: list[CheckResult], grok: dict | None) -> int:
    """0-100. Up to 60 points from the hard checks, up to 40 from Grok (10 - risk)."""
    if not checks:
        return 0
    hard = 60 * sum(1 for c in checks if c.status == PASS) / len(checks)
    risk = (grok or {}).get("risk")
    ai = 40 * (10 - risk) / 10 if isinstance(risk, (int, float)) else 0
    return int(round(hard + ai))


def decide(checks: list[CheckResult], grok: dict | None, cfg: dict) -> Decision:
    s = score(checks, grok)
    failed = not_passed(checks)
    if failed or not checks:
        return Decision(SKIP, s, "hard checks not passed: " + (", ".join(failed) or "no checks ran"))
    if not grok or grok.get("error"):
        return Decision(SKIP, s, "grok unavailable: " + str((grok or {}).get("error") or "no reply"))
    risk = grok.get("risk")
    if not isinstance(risk, (int, float)) or not 0 <= risk <= 10:
        return Decision(SKIP, s, "grok risk missing or out of range")
    if not grok.get("reasons"):
        return Decision(SKIP, s, "grok gave a number without reasons")
    if risk > cfg["grok_max_risk"]:
        return Decision(SKIP, s, f"grok risk {risk} > {cfg['grok_max_risk']}")
    return Decision(BUY, s, f"all checks PASS, grok risk {risk} <= {cfg['grok_max_risk']}")
