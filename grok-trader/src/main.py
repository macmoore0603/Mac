"""grok-trader: scan new Solana tokens, filter rug risk, ask Grok, paper trade.

    python src/main.py --mode paper      # run (default)
    python src/main.py --demo            # offline test on 3 fictional tokens
    python src/main.py --once            # one scan cycle, then exit

PAPER TRADING ONLY. There is no live mode, no wallet and no signing code here.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # so `python src/main.py` works

import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

import checks, feed, grok, journal, onchain, scoring  # noqa: E401,E402
from paper import PaperBroker  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log = logging.getLogger("bot")

REQUIRED = ("poll_interval_seconds", "max_new_tokens_per_cycle", "min_liquidity_usd", "min_pool_age_minutes",
            "max_single_holder", "max_top10_share", "require_mint_disabled", "require_freeze_disabled",
            "require_lp_locked", "grok_model", "grok_max_risk", "paper_starting_balance_usd",
            "max_position_pct", "stop_loss_pct", "take_profit_pct", "max_daily_loss_pct",
            "max_open_positions", "paper_fee_pct")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        sys.exit(f"config.json is missing: {', '.join(missing)}")
    return cfg


def make_paths(suffix: str = "") -> dict:
    return {"state": os.path.join(ROOT, "data", f"paper_state{suffix}.json"),
            "seen": os.path.join(ROOT, "data", f"seen_tokens{suffix}.json"),
            "log": os.path.join(ROOT, "logs", f"decisions{suffix}.jsonl")}


class Bot:
    def __init__(self, cfg: dict, paths: dict, *, session=None, rpc_url: str | None = None,
                 api_key: str | None = None, grok_fn=None, price_fn=None, now_fn=None):
        self.cfg, self.paths = cfg, paths
        self.session = session or requests.Session()
        self.rpc_url = rpc_url or onchain.DEFAULT_RPC_URL
        self.api_key = api_key
        self.now = now_fn or (lambda: datetime.now(timezone.utc))
        self.grok_fn = grok_fn or (lambda payload: grok.ask_grok(
            self.session, self.api_key, cfg["grok_model"], payload, timeout=cfg.get("grok_timeout_seconds", 45)))
        self.price_fn = price_fn or (lambda mint: feed.current_price(self.session, mint))
        self.broker = PaperBroker(paths["state"], cfg)
        self.seen = feed.SeenCache(paths["seen"])
        self.lock = threading.Lock()

    # ----- one token ----------------------------------------------------------
    def process_token(self, mint: str, snapshot: dict, token: dict, report: dict | None) -> dict:
        ts = self.now()
        for key in ("liquidity_usd", "pool_age_minutes"):  # market numbers live in the snapshot
            token.setdefault(key, snapshot.get(key))
        results = checks.evaluate(token, self.cfg)
        rug = onchain.risk_summary(report)
        grok_res = None
        if checks.all_pass(results):  # only clean tokens cost Grok tokens
            grok_res = self.grok_fn(grok.build_payload(mint, snapshot, token, results, rug))
        decision = scoring.decide(results, grok_res, self.cfg)
        record = {"ts": journal.iso(ts), "action": decision.action, "token": mint,
                  "symbol": snapshot.get("symbol"), "pair": snapshot.get("pair_address"),
                  "price_usd": snapshot.get("price_usd"), "score": decision.score, "reason": decision.reason,
                  "checks": [c.to_dict() for c in results], "grok": grok_res, "market": snapshot,
                  "holders": {k: token.get(k) for k in ("largest_holder_share", "top10_share", "holder_source")},
                  "authorities": {k: token.get(k) for k in ("mint_authority", "freeze_authority", "token_program")},
                  "lp": {"status": token.get("lp_status"), "locked_pct": token.get("lp_locked_pct")},
                  "rugcheck": rug, "data_errors": token.get("errors") or []}
        if decision.action == scoring.BUY:
            with self.lock:
                ok, why = self.broker.can_open(mint, ts)
                price = snapshot.get("price_usd")
                if ok and price:
                    record["position"] = self.broker.open(mint, snapshot.get("symbol") or "?", price, ts,
                                                          snapshot.get("pair_address"))
                else:
                    record["action"], record["reason"] = scoring.SKIP, f"would buy, but {why or 'no price'}"
        journal.append(self.paths["log"], record)
        self.seen.add(mint, record["ts"])
        log.info("%-4s %s (%s) score=%s: %s", record["action"].upper(), snapshot.get("symbol"), mint[:8],
                 decision.score, record["reason"])
        return record

    # ----- open positions -----------------------------------------------------
    def mark_positions(self) -> list[dict]:
        closed = []
        for mint in list(self.broker.state["positions"]):
            try:
                price = self.price_fn(mint)
            except Exception as exc:  # keep the position, try again next cycle
                log.warning("price lookup failed for %s: %s", mint, exc)
                continue
            with self.lock:
                rec = self.broker.mark(mint, price, self.now())
            if rec:
                closed.append(journal.append(self.paths["log"], {
                    "action": "sell", "token": mint, "symbol": rec["symbol"], "price_usd": price,
                    "score": None, "reason": f"{rec['reason']} ({rec['pnl_pct']:+.1%}, ${rec['pnl_usd']:+.2f})",
                    "trade": rec}))
                log.info("SELL %s %s %+.1f%%", rec["symbol"], rec["reason"], rec["pnl_pct"] * 100)
        return closed

    # ----- one scan cycle -----------------------------------------------------
    def cycle(self) -> None:
        self.mark_positions()
        try:
            fresh = [m for m in feed.latest_solana_tokens(self.session) if m not in self.seen]
        except feed.FeedError as exc:
            log.error("token feed unavailable: %s", exc)
            return
        for mint in fresh[: self.cfg["max_new_tokens_per_cycle"]]:
            try:
                snapshot = feed.pool_snapshot(feed.main_pool(feed.token_pairs(self.session, mint), mint),
                                              int(self.now().timestamp() * 1000))
                token, report = onchain.gather(self.session, self.rpc_url, mint, snapshot,
                                               use_rugcheck=self.cfg.get("lp_provider") == "rugcheck")
                self.process_token(mint, snapshot, token, report)
            except Exception as exc:  # one bad token must never stop the loop
                log.exception("error on %s", mint)
                journal.append(self.paths["log"], {"action": "error", "token": mint, "symbol": None,
                                                   "score": None, "reason": f"{type(exc).__name__}: {exc}"})
                self.seen.add(mint, journal.iso(self.now()))


# ----- offline demo -------------------------------------------------------------
DEMO_TOKENS = [
    ("DemoSafePup11111111111111111111111111111111", "SAFEPUP", "all rules pass and Grok is relaxed -> paper buy",
     {"symbol": "SAFEPUP", "name": "Safe Pup (fictional)", "price_usd": 0.0012, "liquidity_usd": 82000.0,
      "pool_age_minutes": 45.0, "pair_address": "DemoPair1", "dex_id": "raydium"},
     {"mint_authority": None, "freeze_authority": None, "largest_holder_share": 0.06, "top10_share": 0.31,
      "holder_source": "rpc", "lp_status": "LOCKED", "lp_locked_pct": 100.0, "token_program": "Tokenkeg...", "errors": []},
     {"risk": 2, "reasons": ["liquidity $82,000 is comfortably above $25,000", "top-10 hold 31%, largest 6%",
                             "mint and freeze authority disabled, LP 100% locked"], "missing_data": [], "error": None}),
    ("DemoRugLord11111111111111111111111111111111", "RUGLORD", "mint authority still enabled and one wallet holds 35%",
     {"symbol": "RUGLORD", "name": "Rug Lord (fictional)", "price_usd": 0.5, "liquidity_usd": 60000.0,
      "pool_age_minutes": 30.0, "pair_address": "DemoPair2", "dex_id": "raydium"},
     {"mint_authority": "DemoDeployerWallet", "freeze_authority": None, "largest_holder_share": 0.35,
      "top10_share": 0.62, "holder_source": "rpc", "lp_status": "LOCKED", "lp_locked_pct": 100.0, "errors": []},
     None),
    ("DemoThinPool1111111111111111111111111111111", "THINPOOL", "thin liquidity and LP status unknown",
     {"symbol": "THINPOOL", "name": "Thin Pool (fictional)", "price_usd": 0.00004, "liquidity_usd": 4000.0,
      "pool_age_minutes": 3.0, "pair_address": "DemoPair3", "dex_id": "pumpfun"},
     {"mint_authority": None, "freeze_authority": None, "largest_holder_share": 0.08, "top10_share": 0.38,
      "holder_source": "rugcheck", "lp_status": "UNKNOWN", "lp_locked_pct": None, "errors": ["getTokenLargestAccounts: not available on this RPC tier"]},
     None),
]


def run_demo(cfg: dict) -> int:
    """Full pipeline on three fictional tokens, no network. Separate demo files."""
    paths = make_paths(".demo")
    for p in paths.values():
        if os.path.exists(p):
            os.remove(p)
    replies = {t[0]: t[5] for t in DEMO_TOKENS}
    prices = {}
    bot = Bot(cfg, paths, grok_fn=lambda payload: replies[payload["mint"]], price_fn=lambda m: prices.get(m))
    print("DEMO: three fictional tokens, no network calls, separate .demo state and log files\n")
    for mint, symbol, story, snapshot, token, _ in DEMO_TOKENS:
        rec = bot.process_token(mint, snapshot, token, None)
        print(f"  {rec['action'].upper():5} {symbol:9} score {rec['score']:3}  {story}\n"
              f"        -> {rec['reason']}")
    # Simulate the bought token dropping 30%: the stop-loss must fire.
    prices[DEMO_TOKENS[0][0]] = DEMO_TOKENS[0][3]["price_usd"] * 0.70
    sold = bot.mark_positions()
    for s in sold:
        print(f"  SELL  {s['symbol']:9} {s['reason']}")
    summary = bot.broker.summary(bot.now())
    print(f"\nPaper balance ${summary['balance_usd']:.2f} (started ${summary['starting_balance_usd']:.2f}), "
          f"{summary['closed_trades']} closed, {summary['stop_losses_fired']} stop-loss")
    print(f"Log: {paths['log']}")
    ok = sold and summary["closed_trades"] == 1 and len(journal.tail(paths["log"])) == 4
    print("\nDEMO PASSED" if ok else "\nDEMO FAILED")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Solana new-token scanner + Grok scorer, paper trading only")
    parser.add_argument("--mode", default=None, help="only 'paper' exists (default from .env MODE, else paper)")
    parser.add_argument("--demo", action="store_true", help="offline run on 3 fictional tokens, then exit")
    parser.add_argument("--once", action="store_true", help="run one scan cycle, then exit")
    parser.add_argument("--no-dashboard", action="store_true")
    parser.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    load_dotenv(os.path.join(ROOT, ".env"))
    cfg = load_config(args.config)
    mode = (args.mode or os.getenv("MODE") or "paper").lower()
    if mode != "paper":
        print(f"MODE={mode!r}: live trading is NOT implemented. This bot only paper trades. Exiting.")
        return 2
    if args.demo:
        return run_demo(cfg)

    bot = Bot(cfg, make_paths(), rpc_url=os.getenv("SOLANA_RPC_URL") or onchain.DEFAULT_RPC_URL,
              api_key=os.getenv("XAI_API_KEY"))
    if not bot.api_key or bot.api_key == "your_key_here":
        log.warning("XAI_API_KEY is not set: every token will be skipped at the Grok step (no paper trades).")
    if bot.rpc_url == onchain.DEFAULT_RPC_URL:
        log.warning("Using the public Solana RPC: holder data will come from RugCheck (the free tier does not "
                    "serve getTokenLargestAccounts). A dedicated RPC URL in .env fixes that.")
    if not args.no_dashboard:
        from dashboard import start_dashboard
        host, port = cfg.get("dashboard_host", "127.0.0.1"), int(cfg.get("dashboard_port", 8000))
        start_dashboard(bot, host, port)
        log.info("dashboard: http://%s:%d", "localhost" if host == "127.0.0.1" else host, port)
    log.info("paper mode, polling every %ss, log -> %s", cfg["poll_interval_seconds"], bot.paths["log"])
    try:
        while True:
            bot.cycle()
            if args.once:
                break
            time.sleep(cfg["poll_interval_seconds"])
    except KeyboardInterrupt:
        log.info("stopped by user; paper state saved at %s", bot.paths["state"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
