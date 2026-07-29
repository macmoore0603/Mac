"""Futures contract specifications and tick-exact price/dollar math.

Every price the copilot emits is snapped to a legal tick. A stop at 20512.13 is
not a real price on NQ and would be rejected or silently adjusted by the broker,
which would make the risk numbers a lie. All rounding goes through this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Rounding is done in integer tick space, so this epsilon only guards against
# a float representation landing a hair under an exact tick boundary.
_TICK_EPS = 1e-9


@dataclass(frozen=True)
class ContractSpec:
    """Static definition of a futures contract.

    Attributes:
        symbol: Exchange root symbol (e.g. "NQ").
        name: Human readable description.
        tick_size: Minimum price increment, in index points.
        tick_value: Dollar value of one tick, for one contract.
        round_turn_cost: All-in commission + fees for one contract, in and out.
            Apex/Rithmic and Apex/Tradovate differ; override per your platform.
    """

    symbol: str
    name: str
    tick_size: float
    tick_value: float
    round_turn_cost: float
    currency: str = "USD"

    @property
    def point_value(self) -> float:
        """Dollar value of a one-point move, for one contract."""
        return self.tick_value / self.tick_size

    def points_to_ticks(self, points: float) -> float:
        return points / self.tick_size

    def ticks_to_points(self, ticks: float) -> float:
        return ticks * self.tick_size

    def points_to_dollars(self, points: float, quantity: int = 1) -> float:
        return points * self.point_value * quantity

    def dollars_to_points(self, dollars: float, quantity: int = 1) -> float:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        return dollars / (self.point_value * quantity)

    def commission(self, quantity: int) -> float:
        """Round-turn cost of trading `quantity` contracts."""
        return self.round_turn_cost * quantity

    def round_to_tick(self, price: float, mode: str = "nearest") -> float:
        """Snap `price` to a legal tick.

        Args:
            price: Raw price.
            mode: "nearest", "up" (toward +inf), or "down" (toward -inf).
        """
        raw = price / self.tick_size
        if mode == "nearest":
            n = math.floor(raw + 0.5)
        elif mode == "up":
            n = math.ceil(raw - _TICK_EPS)
        elif mode == "down":
            n = math.floor(raw + _TICK_EPS)
        else:
            raise ValueError(f"unknown rounding mode: {mode!r}")
        # Re-round the product: 0.25 is exact in binary but the multiply can
        # still surface a long tail for large tick counts.
        return round(n * self.tick_size, 10)

    def round_stop(self, price: float, is_long: bool) -> float:
        """Round a stop price in the conservative direction (wider stop).

        A long's stop sits below entry, so rounding down widens it. Never round
        a stop to a tighter price than the model asked for: that silently
        increases the chance of being taken out inside the noise band.
        """
        return self.round_to_tick(price, "down" if is_long else "up")

    def round_target(self, price: float, is_long: bool) -> float:
        """Round a target in the conservative direction (nearer target).

        Rounding a target further away can leave it unfilled by one tick.
        """
        return self.round_to_tick(price, "down" if is_long else "up")


# Round-turn costs below are typical Apex-platform all-in rates as of 2026 and
# are deliberately on the pessimistic side. Confirm against your own fee
# schedule and override via `--round-turn` — every sizing calculation nets them
# out, so understating them overstates your edge.
NQ = ContractSpec(
    symbol="NQ",
    name="E-mini Nasdaq-100",
    tick_size=0.25,
    tick_value=5.00,
    round_turn_cost=4.28,
)

MNQ = ContractSpec(
    symbol="MNQ",
    name="Micro E-mini Nasdaq-100",
    tick_size=0.25,
    tick_value=0.50,
    round_turn_cost=1.34,
)

ES = ContractSpec(
    symbol="ES",
    name="E-mini S&P 500",
    tick_size=0.25,
    tick_value=12.50,
    round_turn_cost=4.28,
)

MES = ContractSpec(
    symbol="MES",
    name="Micro E-mini S&P 500",
    tick_size=0.25,
    tick_value=1.25,
    round_turn_cost=1.34,
)

REGISTRY: dict[str, ContractSpec] = {c.symbol: c for c in (NQ, MNQ, ES, MES)}

# Micro contracts are 1/10th the notional of their mini parent. Apex expresses
# contract caps in minis and allows 10 micros per mini of allowance.
MICRO_PARENT: dict[str, str] = {"MNQ": "NQ", "MES": "ES"}
MICROS_PER_MINI = 10


def get_contract(symbol: str) -> ContractSpec:
    """Look up a contract by root symbol, case-insensitively."""
    key = symbol.upper().strip()
    if key not in REGISTRY:
        raise KeyError(f"unknown contract {symbol!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[key]


def is_micro(spec: ContractSpec) -> bool:
    return spec.symbol in MICRO_PARENT
