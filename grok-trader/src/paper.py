"""Paper trading: a fake balance, simulated fills, stop-loss and take-profit.

No real transactions, no wallet, no keys, no signing code anywhere in this
project. Paper fills are optimistic by nature (they ignore slippage and the fact
that thin pools move when you trade them), so a flat haircut, paper_fee_pct, is
charged on every buy and every sell to keep the numbers a little more honest.

"Day" means UTC calendar day. When realised losses for the day reach
max_daily_loss_pct of the equity the day started with, no new positions open
until the next UTC day. Existing positions still get their stops checked.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from journal import iso


class PaperBroker:
    def __init__(self, path: str, cfg: dict):
        self.path = path
        self.cfg = cfg
        self.state = self._load()

    # ----- persistence -------------------------------------------------------
    def _load(self) -> dict:
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        start = float(self.cfg["paper_starting_balance_usd"])
        return {"balance_usd": start, "starting_balance_usd": start,
                "positions": {}, "closed": [], "days": {}}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2)
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written file

    # ----- daily loss bookkeeping -------------------------------------------
    def _day(self, ts: datetime) -> dict:
        key = ts.date().isoformat()
        day = self.state["days"].get(key)
        if day is None:
            invested = sum(p["cost_usd"] for p in self.state["positions"].values())
            day = {"start_equity": self.state["balance_usd"] + invested,
                   "realized_pnl": 0.0, "halted": False}
            self.state["days"][key] = day
        return day

    def halted(self, ts: datetime) -> bool:
        return self._day(ts)["halted"]

    # ----- trading -------------------------------------------------------------
    def can_open(self, mint: str, ts: datetime) -> tuple[bool, str]:
        if self._day(ts)["halted"]:
            return False, "daily loss limit hit, no new trades until the next UTC day"
        if mint in self.state["positions"]:
            return False, "already holding this token"
        if len(self.state["positions"]) >= self.cfg["max_open_positions"]:
            return False, "max open positions reached"
        if self.state["balance_usd"] * self.cfg["max_position_pct"] < 1.0:
            return False, "paper balance too small to open a position"
        return True, ""

    def open(self, mint: str, symbol: str, price: float, ts: datetime,
             pair_address: str | None = None) -> dict:
        ok, why = self.can_open(mint, ts)
        if not ok:
            raise ValueError(why)
        if not price or price <= 0:
            raise ValueError("invalid price")
        size = self.state["balance_usd"] * self.cfg["max_position_pct"]
        fee = size * self.cfg["paper_fee_pct"]
        pos = {
            "mint": mint, "symbol": symbol, "qty": (size - fee) / price,
            "entry_price": price, "cost_usd": size, "entry_fee_usd": fee,
            "stop_price": price * (1 - self.cfg["stop_loss_pct"]),
            "take_profit_price": price * (1 + self.cfg["take_profit_pct"]),
            "opened_at": iso(ts), "pair_address": pair_address, "last_price": price,
        }
        self.state["balance_usd"] -= size
        self.state["positions"][mint] = pos
        self.save()
        return pos

    def mark(self, mint: str, price: float | None, ts: datetime) -> dict | None:
        """Update one position's last price; close it if stop or target is hit."""
        pos = self.state["positions"].get(mint)
        if not pos or not price or price <= 0:
            return None
        pos["last_price"] = price
        if price <= pos["stop_price"]:
            return self.close(mint, price, ts, "stop_loss")
        if price >= pos["take_profit_price"]:
            return self.close(mint, price, ts, "take_profit")
        self.save()
        return None

    def close(self, mint: str, price: float, ts: datetime, reason: str) -> dict:
        day = self._day(ts)  # before the trade changes balance or positions, so a new day starts clean
        pos = self.state["positions"].pop(mint)
        gross = pos["qty"] * price
        fee = gross * self.cfg["paper_fee_pct"]
        proceeds = gross - fee
        pnl = proceeds - pos["cost_usd"]
        self.state["balance_usd"] += proceeds
        day["realized_pnl"] += pnl
        if day["realized_pnl"] <= -self.cfg["max_daily_loss_pct"] * day["start_equity"]:
            day["halted"] = True
        rec = {**pos, "exit_price": price, "proceeds_usd": proceeds, "exit_fee_usd": fee,
               "pnl_usd": pnl, "pnl_pct": pnl / pos["cost_usd"], "reason": reason,
               "closed_at": iso(ts)}
        self.state["closed"].append(rec)
        self.save()
        return rec

    # ----- reporting ----------------------------------------------------------
    def equity(self) -> float:
        held = sum(p["qty"] * (p.get("last_price") or p["entry_price"])
                   for p in self.state["positions"].values())
        return self.state["balance_usd"] + held

    def summary(self, ts: datetime) -> dict:
        closed = self.state["closed"]
        wins = [c for c in closed if c["pnl_usd"] > 0]
        losses = [c for c in closed if c["pnl_usd"] <= 0]
        day = self._day(ts)
        return {
            "balance_usd": round(self.state["balance_usd"], 2),
            "equity_usd": round(self.equity(), 2),
            "starting_balance_usd": self.state["starting_balance_usd"],
            "open_positions": list(self.state["positions"].values()),
            "closed_trades": len(closed), "wins": len(wins), "losses": len(losses),
            "avg_win_usd": round(sum(c["pnl_usd"] for c in wins) / len(wins), 2) if wins else 0.0,
            "avg_loss_usd": round(sum(c["pnl_usd"] for c in losses) / len(losses), 2) if losses else 0.0,
            "biggest_loss_usd": round(min((c["pnl_usd"] for c in closed), default=0.0), 2),
            "stop_losses_fired": sum(1 for c in closed if c["reason"] == "stop_loss"),
            "realized_pnl_usd": round(sum(c["pnl_usd"] for c in closed), 2),
            "today_realized_pnl_usd": round(day["realized_pnl"], 2),
            "halted_today": day["halted"],
        }
