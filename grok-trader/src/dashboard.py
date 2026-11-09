"""A one-page local dashboard (Flask). Read-only: it never places trades.

Binds to 127.0.0.1 by default so nothing on your network can reach it.
Open http://localhost:8000 while the bot runs. The page refreshes itself.
"""
from __future__ import annotations

import logging
import threading

from flask import Flask, jsonify, render_template_string

import journal

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30"><title>grok-trader (paper)</title>
<style>
 body{font:14px/1.4 system-ui,sans-serif;margin:24px;max-width:1100px;color:#222}
 h1{font-size:20px} h2{font-size:16px;margin-top:28px} table{border-collapse:collapse;width:100%}
 th,td{border-bottom:1px solid #ddd;padding:4px 8px;text-align:left;font-size:13px;vertical-align:top}
 .buy{color:#0a7} .skip{color:#777} .sell{color:#06c} .error{color:#c33}
 .tiles{display:flex;gap:12px;flex-wrap:wrap} .tile{border:1px solid #ddd;border-radius:6px;padding:10px 14px;min-width:150px}
 .tile b{display:block;font-size:18px} small{color:#777}
</style></head><body>
<h1>grok-trader <small>paper mode only · refreshes every 30 s · UTC times</small></h1>
<div class="tiles">
 <div class="tile"><small>Paper balance</small><b>${{ "%.2f"|format(p.balance_usd) }}</b></div>
 <div class="tile"><small>Equity (incl. open)</small><b>${{ "%.2f"|format(p.equity_usd) }}</b></div>
 <div class="tile"><small>Realised P&amp;L</small><b>${{ "%.2f"|format(p.realized_pnl_usd) }}</b></div>
 <div class="tile"><small>Today</small><b>${{ "%.2f"|format(p.today_realized_pnl_usd) }}</b>
   {% if p.halted_today %}<small style="color:#c33">daily loss limit hit</small>{% endif %}</div>
 <div class="tile"><small>Closed trades</small><b>{{ p.closed_trades }}</b><small>{{ p.wins }} wins / {{ p.losses }} losses · {{ p.stop_losses_fired }} stops</small></div>
</div>
<h2>Open positions ({{ p.open_positions|length }})</h2>
<table><tr><th>Token</th><th>Entry</th><th>Last</th><th>Stop</th><th>Target</th><th>Cost</th><th>Opened</th></tr>
{% for o in p.open_positions %}<tr><td>{{ o.symbol }}<br><small>{{ o.mint }}</small></td>
<td>{{ "%.8g"|format(o.entry_price) }}</td><td>{{ "%.8g"|format(o.last_price) }}</td><td>{{ "%.8g"|format(o.stop_price) }}</td>
<td>{{ "%.8g"|format(o.take_profit_price) }}</td><td>${{ "%.2f"|format(o.cost_usd) }}</td><td>{{ o.opened_at }}</td></tr>
{% else %}<tr><td colspan="7"><small>none</small></td></tr>{% endfor %}</table>
<h2>Decisions ({{ stats.actions }})</h2>
<table><tr><th>Check</th>{% for s in ["PASS","FAIL","UNKNOWN"] %}<th>{{ s }}</th>{% endfor %}</tr>
{% for name, c in stats.checks.items() %}<tr><td>{{ name }}</td>{% for s in ["PASS","FAIL","UNKNOWN"] %}<td>{{ c.get(s, 0) }}</td>{% endfor %}</tr>{% endfor %}</table>
<h2>Last {{ decisions|length }} decisions</h2>
<table><tr><th>Time</th><th>Action</th><th>Token</th><th>Score</th><th>Reason</th></tr>
{% for d in decisions|reverse %}<tr><td>{{ d.ts }}</td><td class="{{ d.action }}">{{ d.action }}</td>
<td>{{ d.symbol }}<br><small>{{ d.token }}</small></td><td>{{ d.score }}</td><td>{{ d.reason }}</td></tr>{% endfor %}</table>
</body></html>"""


def create_app(bot) -> Flask:
    app = Flask(__name__)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    def state():
        with bot.lock:
            paper = bot.broker.summary(bot.now())
        recent = journal.tail(bot.paths["log"], 1000)
        return paper, recent[-50:], journal.summarize(recent)

    @app.get("/")
    def index():
        paper, decisions, stats = state()
        return render_template_string(PAGE, p=paper, decisions=decisions, stats=stats)

    @app.get("/api/state")
    def api_state():
        paper, decisions, stats = state()
        return jsonify({"paper": paper, "decisions": decisions, "stats": stats})

    return app


def start_dashboard(bot, host: str, port: int) -> threading.Thread:
    app = create_app(bot)
    thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True),
        name="dashboard", daemon=True)
    thread.start()
    return thread
