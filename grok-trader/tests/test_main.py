import json
from datetime import datetime, timezone

import pytest

import feed
import main
import onchain
import scoring
from main import Bot, DEMO_TOKENS, load_config, run_demo

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return load_config(main.os.path.join(main.ROOT, "config.json"))


def paths(tmp_path):
    return {"state": str(tmp_path / "s.json"), "seen": str(tmp_path / "seen.json"), "log": str(tmp_path / "d.jsonl")}


def test_demo_runs_offline_and_passes(tmp_path, monkeypatch, cfg, capsys):
    monkeypatch.setattr(main, "ROOT", str(tmp_path))
    assert run_demo(cfg) == 0
    rows = [json.loads(l) for l in open(tmp_path / "logs" / "decisions.demo.jsonl")]
    assert [r["action"] for r in rows] == ["buy", "skip", "skip", "sell"]
    assert rows[0]["grok"]["risk"] == 2 and rows[1]["grok"] is None  # failed tokens never reach Grok
    assert "DEMO PASSED" in capsys.readouterr().out


def test_non_paper_mode_exits_without_trading(cfg):
    assert main.main(["--mode", "live", "--no-dashboard"]) == 2
    assert main.main(["--mode", "REAL", "--demo"]) == 2


def test_config_validation(tmp_path):
    (tmp_path / "c.json").write_text('{"poll_interval_seconds": 1}')
    with pytest.raises(SystemExit):
        load_config(str(tmp_path / "c.json"))


def test_buy_signal_respects_broker_limits(tmp_path, cfg):
    mint, _, _, snapshot, token, reply = DEMO_TOKENS[0]
    bot = Bot(cfg, paths(tmp_path), grok_fn=lambda p: reply, price_fn=lambda m: None, now_fn=lambda: T0)
    first = bot.process_token(mint, dict(snapshot), dict(token), None)
    assert first["action"] == "buy" and "position" in first
    again = bot.process_token(mint, dict(snapshot), dict(token), None)
    assert again["action"] == "skip" and "already holding" in again["reason"]
    assert bot.process_token("X", {**snapshot, "price_usd": None}, dict(token), None)["reason"].endswith("no price")


def test_cycle_logs_errors_and_marks_tokens_seen(tmp_path, cfg, monkeypatch):
    bot = Bot(cfg, paths(tmp_path), grok_fn=lambda p: None, price_fn=lambda m: None, now_fn=lambda: T0)
    monkeypatch.setattr(feed, "latest_solana_tokens", lambda s: ["GOOD", "BOOM"])
    monkeypatch.setattr(feed, "token_pairs", lambda s, m: [] if m == "GOOD" else (_ for _ in ()).throw(RuntimeError("feed exploded")))
    monkeypatch.setattr(onchain, "gather", lambda *a, **k: ({"mint_authority": None, "errors": []}, None))
    bot.cycle()
    rows = [json.loads(l) for l in open(tmp_path / "d.jsonl")]
    assert [r["action"] for r in rows] == ["skip", "error"] and "feed exploded" in rows[1]["reason"]
    assert "GOOD" in bot.seen and "BOOM" in bot.seen
    bot.cycle()  # nothing new: no extra rows
    assert len(open(tmp_path / "d.jsonl").readlines()) == 2


def test_feed_outage_does_not_crash_cycle(tmp_path, cfg, monkeypatch):
    bot = Bot(cfg, paths(tmp_path), price_fn=lambda m: None, now_fn=lambda: T0)
    monkeypatch.setattr(feed, "latest_solana_tokens", lambda s: (_ for _ in ()).throw(feed.FeedError("down")))
    bot.cycle()


def test_dashboard_renders(tmp_path, cfg):
    from dashboard import create_app
    mint, _, _, snapshot, token, reply = DEMO_TOKENS[0]
    bot = Bot(cfg, paths(tmp_path), grok_fn=lambda p: reply, price_fn=lambda m: None, now_fn=lambda: T0)
    bot.process_token(mint, dict(snapshot), {**token, "symbol": "<b>x</b>"}, None)
    client = create_app(bot).test_client()
    page = client.get("/")
    assert page.status_code == 200 and b"SAFEPUP" in page.data and b"Open positions (1)" in page.data
    state = client.get("/api/state").get_json()
    assert state["paper"]["open_positions"][0]["mint"] == mint and state["stats"]["actions"] == {"buy": 1}


def test_dashboard_escapes_untrusted_symbols(tmp_path, cfg):
    from dashboard import create_app
    mint, _, _, snapshot, token, _ = DEMO_TOKENS[1]
    bot = Bot(cfg, paths(tmp_path), price_fn=lambda m: None, now_fn=lambda: T0)
    bot.process_token(mint, {**snapshot, "symbol": "<script>alert(1)</script>"}, dict(token), None)
    page = create_app(bot).test_client().get("/").data
    assert b"<script>alert(1)</script>" not in page and b"&lt;script&gt;" in page
