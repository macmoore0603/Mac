"""Tests for the TradingView webhook feed.

Two areas get the most attention:

* **Ingestion integrity.** TradingView resends alerts and fires more than once
  per bar. Duplicates or out-of-order bars silently corrupt every rolling
  average, so idempotency is asserted directly.
* **Authentication.** The endpoint has to be publicly reachable for TradingView
  to post to it, which means anyone else can reach it too. An unauthenticated
  post must not be able to inject a fake bar and move a real decision.
"""

from __future__ import annotations

import http.client
import json
from datetime import datetime, timedelta, timezone

import pytest

from nqcopilot.apex import APEX_50K_INTRADAY, AccountState, RiskEngine, RiskLimits
from nqcopilot.bars import ET, Bar
from nqcopilot.contracts import MNQ
from nqcopilot.data import generate_demo_bars
from nqcopilot.webhook import (
    BarStore,
    LivePosition,
    WebhookConfig,
    WebhookContext,
    WebhookError,
    WebhookServer,
    parse_alert,
)

DEMO_END = datetime(2026, 7, 27, 15, 0, tzinfo=ET)


_BASE = datetime(2026, 7, 27, 10, 0, tzinfo=ET)


def _bar(minutes: int, close: float = 20_000.0) -> Bar:
    """A bar `minutes` after 10:00 ET (may run past the hour)."""
    return Bar(
        ts=_BASE + timedelta(minutes=minutes),
        open=close,
        high=close + 5,
        low=close - 5,
        close=close,
        volume=100.0,
    )


def _payload(**overrides) -> dict:
    base = {
        "symbol": "CME_MINI:MNQ1!",
        "interval": "5",
        # Pine sends epoch milliseconds.
        "time": 1785587400000,
        "open": 20400.0,
        "high": 20410.0,
        "low": 20395.0,
        "close": 20405.0,
        "volume": 1234,
    }
    base.update(overrides)
    return base


class TestPayloadParsing:
    def test_parses_a_pine_payload(self):
        bar, meta = parse_alert(_payload())
        assert bar.open == 20400.0 and bar.close == 20405.0
        assert bar.volume == 1234.0
        assert meta["symbol"] == "CME_MINI:MNQ1!"
        assert meta["interval"] == "5"

    def test_epoch_milliseconds_become_eastern_time(self):
        bar, _ = parse_alert(_payload(time=1785587400000))
        assert bar.ts.tzinfo is not None
        # 1785587400 == 2026-07-28 12:30 UTC == 08:30 ET
        assert bar.et.hour == 8 and bar.et.minute == 30

    def test_accepts_iso_timestamps_too(self):
        bar, _ = parse_alert(_payload(time="2026-07-28T12:30:00Z"))
        assert bar.et.hour == 8 and bar.et.minute == 30

    def test_accepts_quoted_numbers(self):
        """A hand-edited alert body easily ends up with quoted placeholders."""
        bar, _ = parse_alert(
            _payload(open="20400.0", high="20410.0", low="20395.0", close="20405.0")
        )
        assert bar.close == 20405.0

    def test_missing_volume_defaults_to_zero(self):
        payload = _payload()
        del payload["volume"]
        bar, _ = parse_alert(payload)
        assert bar.volume == 0.0

    def test_carries_the_charts_own_read_as_metadata(self):
        _, meta = parse_alert(
            _payload(tv_action="GO LONG", tv_setup="Trend Pullback", tv_regime="TREND UP")
        )
        assert meta["tv_action"] == "GO LONG"
        assert meta["tv_setup"] == "Trend Pullback"

    @pytest.mark.parametrize("field", ["open", "high", "low", "close"])
    def test_missing_price_field_is_rejected(self, field):
        payload = _payload()
        del payload[field]
        with pytest.raises(WebhookError, match=field):
            parse_alert(payload)

    def test_missing_time_is_rejected(self):
        payload = _payload()
        del payload["time"]
        with pytest.raises(WebhookError, match="bar time"):
            parse_alert(payload)

    def test_non_numeric_price_is_rejected(self):
        with pytest.raises(WebhookError, match="not numeric"):
            parse_alert(_payload(close="not-a-price"))

    def test_inconsistent_ohlc_is_rejected(self):
        """A high below the close is corrupt data, not something to repair."""
        with pytest.raises(WebhookError):
            parse_alert(_payload(high=20_000.0, close=20_405.0))

    def test_non_object_payload_is_rejected(self):
        with pytest.raises(WebhookError, match="JSON object"):
            parse_alert(["not", "an", "object"])


class TestBarStore:
    def test_appends_newer_bars(self):
        store = BarStore()
        assert store.ingest(_bar(0)) == "appended"
        assert store.ingest(_bar(5)) == "appended"
        assert len(store) == 2

    def test_redelivery_of_the_same_bar_updates_in_place(self):
        """TradingView can resend; a duplicate must not become a second bar."""
        store = BarStore()
        store.ingest(_bar(0, close=20_000.0))
        assert store.ingest(_bar(0, close=20_050.0)) == "updated"
        assert len(store) == 1
        assert store.snapshot()[-1].close == 20_050.0

    def test_older_bars_are_rejected_as_stale(self):
        store = BarStore()
        store.ingest(_bar(10))
        assert store.ingest(_bar(5)) == "stale"
        assert len(store) == 1

    def test_seeded_bars_are_sorted(self):
        store = BarStore([_bar(10), _bar(0), _bar(5)])
        stamps = [b.ts for b in store.snapshot()]
        assert stamps == sorted(stamps)

    def test_rolls_off_beyond_the_cap(self):
        store = BarStore(max_bars=10)
        for minute in range(0, 100, 5):
            store.ingest(_bar(minute))
        assert len(store) == 10
        # The most recent bars are the ones retained.
        assert store.snapshot()[-1].ts == _BASE + timedelta(minutes=95)

    def test_snapshot_is_a_copy(self):
        store = BarStore([_bar(0)])
        snapshot = store.snapshot()
        snapshot.append(_bar(5))
        assert len(store) == 1


class TestServer:
    @pytest.fixture
    def server(self):
        state = AccountState.fresh(APEX_50K_INTRADAY, today=DEMO_END.date())
        seed = generate_demo_bars(count=200, seed=7, end=DEMO_END)
        context = WebhookContext(
            config=WebhookConfig(host="127.0.0.1", port=0, secret="s3cret"),
            spec=MNQ,
            state=state,
            store=BarStore(seed),
            engine=RiskEngine(state, RiskLimits()),
        )
        srv = WebhookServer(context)
        srv.start_background()
        yield srv
        srv.shutdown()

    @staticmethod
    def _post(server, payload, path="/webhook", raw: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        body = raw if raw is not None else json.dumps(payload)
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read().decode()
        conn.close()
        return response.status, data

    @staticmethod
    def _get(server, path):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        data = response.read().decode()
        conn.close()
        return response.status, data

    def _next_bar_payload(self, server, **overrides):
        """A payload one interval after the newest seeded bar."""
        last = server.context.store.snapshot()[-1]
        ts = last.ts + timedelta(minutes=5)
        payload = _payload(
            time=int(ts.timestamp() * 1000),
            open=last.close,
            high=last.close + 8,
            low=last.close - 3,
            close=last.close + 6,
        )
        payload.update(overrides)
        return payload

    def test_health_reports_state(self, server):
        status, body = self._get(server, "/health")
        assert status == 200
        payload = json.loads(body)
        assert payload["status"] == "ok"
        assert payload["bars"] == 200
        assert payload["symbol"] == "MNQ"

    def test_accepts_an_authenticated_bar_and_decides(self, server):
        payload = self._next_bar_payload(server, secret="s3cret")
        status, body = self._post(server, payload)
        assert status == 200
        result = json.loads(body)
        assert result["status"] == "appended"
        assert result["bars"] == 201
        assert "action" in result and result["headline"]

    def test_rejects_a_wrong_secret(self, server):
        payload = self._next_bar_payload(server, secret="wrong")
        status, _ = self._post(server, payload)
        assert status == 401
        # The bad post must not have entered the series.
        assert len(server.context.store) == 200

    def test_rejects_a_missing_secret(self, server):
        status, _ = self._post(server, self._next_bar_payload(server))
        assert status == 401
        assert len(server.context.store) == 200

    def test_accepts_the_secret_via_header(self, server):
        payload = self._next_bar_payload(server)
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request(
            "POST",
            "/webhook",
            body=json.dumps(payload),
            headers={"Content-Type": "application/json", "X-Webhook-Secret": "s3cret"},
        )
        assert conn.getresponse().status == 200
        conn.close()

    def test_rejects_malformed_json(self, server):
        status, body = self._post(server, None, raw="{not json")
        assert status == 400
        assert "invalid JSON" in body

    def test_rejects_an_oversized_body(self, server):
        huge = json.dumps({"secret": "s3cret", "pad": "x" * 100_000})
        status, _ = self._post(server, None, raw=huge)
        assert status == 413

    def test_rejects_a_bad_bar_with_a_reason(self, server):
        payload = self._next_bar_payload(server, secret="s3cret")
        del payload["close"]
        status, body = self._post(server, payload)
        assert status == 400
        assert "close" in body

    def test_unknown_paths_404(self, server):
        assert self._post(server, {"secret": "s3cret"}, path="/elsewhere")[0] == 404
        assert self._get(server, "/nope")[0] == 404

    def test_stale_bar_is_acknowledged_but_not_stored(self, server):
        last = server.context.store.snapshot()[-1]
        old = last.ts - timedelta(minutes=30)
        payload = _payload(
            secret="s3cret",
            time=int(old.timestamp() * 1000),
            open=last.close, high=last.close + 1, low=last.close - 1, close=last.close,
        )
        status, body = self._post(server, payload)
        assert status == 200
        assert json.loads(body)["status"] == "stale"
        assert len(server.context.store) == 200

    def test_decision_endpoint_serves_the_latest_read(self, server):
        assert self._get(server, "/decision")[0] == 404  # nothing yet
        self._post(server, self._next_bar_payload(server, secret="s3cret"))
        status, body = self._get(server, "/decision")
        assert status == 200
        payload = json.loads(body)
        assert "action" in payload and "account" in payload
        assert payload["account"]["threshold"] == pytest.approx(48_000.0)

    def test_decision_respects_account_rules(self, server):
        """The live feed goes through the same risk veto as everything else."""
        server.context.state.close_trade(-600.0)
        server.context.engine = RiskEngine(
            server.context.state, RiskLimits(daily_loss_limit=600.0)
        )
        status, body = self._post(
            server, self._next_bar_payload(server, secret="s3cret")
        )
        assert status == 200
        assert json.loads(body)["action"] == "STAND_DOWN"

    def test_callback_receives_directive_and_metadata(self, server):
        seen = []
        server.context.on_directive = lambda d, m: seen.append((d, m))
        self._post(
            server,
            self._next_bar_payload(server, secret="s3cret", tv_action="WAIT"),
        )
        assert len(seen) == 1
        directive, meta = seen[0]
        assert directive.headline
        assert meta["tv_action"] == "WAIT"


class TestUnauthenticatedServer:
    def test_no_secret_configured_accepts_posts(self):
        """Opt-out is allowed, but the CLI warns loudly when it is used."""
        state = AccountState.fresh(APEX_50K_INTRADAY, today=DEMO_END.date())
        context = WebhookContext(
            config=WebhookConfig(host="127.0.0.1", port=0, secret=None),
            spec=MNQ,
            state=state,
            store=BarStore(),
        )
        server = WebhookServer(context)
        server.start_background()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            conn.request("POST", "/webhook", body=json.dumps(_payload()))
            assert conn.getresponse().status == 200
            conn.close()
            assert len(context.store) == 1
        finally:
            server.shutdown()


class TestRealTimeMarking:
    """The threshold tracks peak equity in real time, not on bar close.

    Waiting for a 5-minute bar before marking means reporting room the account
    no longer has: a spike two minutes in has already moved the threshold.
    """

    @pytest.fixture
    def ctx(self):
        state = AccountState.fresh(APEX_50K_INTRADAY, today=DEMO_END.date())
        return WebhookContext(
            config=WebhookConfig(host="127.0.0.1", port=0),
            spec=MNQ,
            state=state,
            store=BarStore(),
        )

    def test_price_mark_ratchets_the_threshold_immediately(self, ctx):
        ctx.set_position(LivePosition(side=1, quantity=10, entry=20_000.0))
        before = ctx.state.threshold

        # +50 points on 10 micros = $1,000 of unrealised profit.
        snapshot = ctx.mark_price(20_050.0)

        assert snapshot["open_pnl"] == pytest.approx(1_000.0)
        assert snapshot["threshold"] == pytest.approx(before + 1_000.0)
        assert snapshot["room"] == pytest.approx(2_000.0)

    def test_giving_it_back_costs_room_in_real_time(self, ctx):
        ctx.set_position(LivePosition(side=1, quantity=10, entry=20_000.0))
        ctx.mark_price(20_050.0)          # +$1,000 unrealised
        snapshot = ctx.mark_price(20_000.0)  # straight back to entry

        assert snapshot["open_pnl"] == pytest.approx(0.0)
        assert snapshot["room"] == pytest.approx(1_000.0)  # half the buffer, gone

    def test_short_positions_mark_the_other_way(self, ctx):
        ctx.set_position(LivePosition(side=-1, quantity=5, entry=20_000.0))
        snapshot = ctx.mark_price(19_900.0)  # 100 points in favour
        assert snapshot["open_pnl"] == pytest.approx(1_000.0)

    def test_mark_by_pnl_directly(self, ctx):
        snapshot = ctx.mark_pnl(750.0)
        assert snapshot["open_pnl"] == pytest.approx(750.0)
        assert snapshot["threshold"] == pytest.approx(48_750.0)

    def test_clearing_the_position_zeroes_open_pnl(self, ctx):
        ctx.set_position(LivePosition(side=1, quantity=10, entry=20_000.0))
        ctx.mark_price(20_050.0)
        snapshot = ctx.set_position(None)
        assert snapshot["open_pnl"] == 0.0
        # The ratchet is permanent; clearing the position does not give it back.
        assert snapshot["threshold"] == pytest.approx(49_000.0)

    def test_bar_close_marks_the_favourable_extreme(self, ctx):
        """A spike inside the bar already moved the threshold in reality."""
        ctx.set_position(LivePosition(side=1, quantity=10, entry=20_000.0))
        bar = Bar(
            ts=_BASE,
            open=20_000.0,
            high=20_060.0,   # +$1,200 at the intrabar peak
            low=19_995.0,
            close=20_000.0,  # but closes flat
            volume=500.0,
        )
        ctx.handle_bar(bar, {})
        # Threshold reflects the peak, not the close.
        assert ctx.state.threshold == pytest.approx(49_200.0)
        assert ctx.state.open_pnl == pytest.approx(0.0)

    def test_flat_bars_do_not_ratchet(self, ctx):
        before = ctx.state.threshold
        ctx.handle_bar(_bar(0), {})
        assert ctx.state.threshold == pytest.approx(before)

    def test_breach_is_visible_immediately(self, ctx):
        ctx.set_position(LivePosition(side=1, quantity=10, entry=20_000.0))
        snapshot = ctx.mark_price(19_900.0)  # -$2,000 unrealised
        assert snapshot["breached"] is True

    def test_position_parsing_accepts_common_spellings(self):
        from nqcopilot.webhook import _parse_position

        assert _parse_position({"side": "buy", "quantity": 2, "entry": 1.0}).side == 1
        assert _parse_position({"side": "SHORT", "quantity": 2, "entry": 1.0}).side == -1
        assert _parse_position({"side": "flat"}) is None
        assert _parse_position({}) is None

    def test_position_parsing_rejects_nonsense(self):
        from nqcopilot.webhook import _parse_position

        with pytest.raises(WebhookError, match="unknown side"):
            _parse_position({"side": "sideways", "quantity": 1, "entry": 1.0})
        with pytest.raises(WebhookError, match="numeric"):
            _parse_position({"side": "long"})
        with pytest.raises(WebhookError, match="positive"):
            _parse_position({"side": "long", "quantity": 0, "entry": 1.0})


class TestRealTimeEndpoints:
    @pytest.fixture
    def server(self):
        state = AccountState.fresh(APEX_50K_INTRADAY, today=DEMO_END.date())
        context = WebhookContext(
            config=WebhookConfig(host="127.0.0.1", port=0, secret="s3cret"),
            spec=MNQ,
            state=state,
            store=BarStore(),
        )
        srv = WebhookServer(context)
        srv.start_background()
        yield srv
        srv.shutdown()

    @staticmethod
    def _post(server, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("POST", path, body=json.dumps(payload))
        response = conn.getresponse()
        data = response.read().decode()
        conn.close()
        return response.status, json.loads(data) if data else {}

    @staticmethod
    def _get(server, path):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        data = response.read().decode()
        conn.close()
        return response.status, json.loads(data) if data else {}

    def test_declare_position_then_mark(self, server):
        status, _ = self._post(
            server, "/position",
            {"secret": "s3cret", "side": "long", "quantity": 10, "entry": 20_000.0},
        )
        assert status == 200

        status, snapshot = self._post(server, "/mark", {"secret": "s3cret", "price": 20_050.0})
        assert status == 200
        assert snapshot["open_pnl"] == pytest.approx(1_000.0)
        assert snapshot["threshold"] == pytest.approx(49_000.0)
        assert snapshot["room"] == pytest.approx(2_000.0)

    def test_account_endpoint_is_pollable(self, server):
        status, snapshot = self._get(server, "/account")
        assert status == 200
        assert snapshot["threshold"] == pytest.approx(48_000.0)
        assert snapshot["room"] == pytest.approx(2_000.0)
        assert snapshot["position"] is None

    def test_mark_requires_authentication(self, server):
        status, _ = self._post(server, "/mark", {"price": 20_050.0})
        assert status == 401

    def test_position_requires_authentication(self, server):
        status, _ = self._post(
            server, "/position", {"side": "long", "quantity": 1, "entry": 1.0}
        )
        assert status == 401

    def test_mark_without_price_or_pnl_is_rejected(self, server):
        status, body = self._post(server, "/mark", {"secret": "s3cret"})
        assert status == 400
        assert "price" in body["error"]
