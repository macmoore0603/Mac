"""Receive live bars from TradingView alert webhooks.

This is how the copilot sees what you see, without ever touching your
TradingView credentials.

The Pine indicator runs on *your* chart — your symbol, your timeframe, your
session and data feed — and fires an alert on every bar close. TradingView
POSTs that bar here. The engine then evaluates the same series you are looking
at, and the decision it produces is grounded in your chart rather than in some
other provider's approximation of it.

Why this design rather than logging in:

* TradingView's terms prohibit automated access. A scripted login risks your
  account, and the copilot is not worth losing your charts over.
* There is no bar-data API. A login-based approach means scraping a private
  WebSocket with your session cookie, which breaks without warning.
* No credentials exist here to leak. The only secret is one you choose, used to
  reject unauthenticated posts.

The trade-off, stated honestly: TradingView must be able to reach this server,
so it needs a public URL (a tunnel such as cloudflared/ngrok, or a small VPS).
It binds to localhost by default precisely so it is not exposed by accident.
"""

from __future__ import annotations

import hmac
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .apex import AccountState, RiskEngine
from .bars import ET, Bar
from .contracts import ContractSpec
from .market import IndicatorConfig
from .playbook import Directive, PlaybookConfig, evaluate


class WebhookError(ValueError):
    """Raised when an incoming payload cannot be turned into a bar."""


@dataclass(frozen=True)
class WebhookConfig:
    """Server and ingestion settings."""

    host: str = "127.0.0.1"
    port: int = 8787
    secret: str | None = None
    max_bars: int = 2000
    max_body_bytes: int = 64 * 1024
    path: str = "/webhook"


class BarStore:
    """Thread-safe rolling bar series fed by webhook deliveries.

    TradingView can resend an alert, and an alert may fire more than once for
    the same bar. Ingestion is therefore idempotent on timestamp: a repeat of
    the newest bar replaces it, an older bar is rejected as stale, and only a
    genuinely newer bar is appended. Without that, duplicates would silently
    corrupt every rolling average downstream.
    """

    def __init__(self, initial: list[Bar] | None = None, max_bars: int = 2000) -> None:
        self._bars: list[Bar] = sorted(initial or [], key=lambda b: b.ts)
        self._max_bars = max_bars
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._bars)

    def ingest(self, bar: Bar) -> str:
        """Add a bar. Returns "appended", "updated" or "stale"."""
        with self._lock:
            if not self._bars:
                self._bars.append(bar)
                return "appended"

            last = self._bars[-1]
            if bar.ts > last.ts:
                self._bars.append(bar)
                result = "appended"
            elif bar.ts == last.ts:
                # The same bar re-delivered, possibly with updated values.
                self._bars[-1] = bar
                result = "updated"
            else:
                return "stale"

            if len(self._bars) > self._max_bars:
                del self._bars[: len(self._bars) - self._max_bars]
            return result

    def snapshot(self) -> list[Bar]:
        with self._lock:
            return list(self._bars)


def parse_alert(payload: dict) -> tuple[Bar, dict]:
    """Convert a TradingView alert payload into a bar plus its metadata.

    TradingView renders `{{open}}`-style placeholders as bare numbers, but a
    user-edited alert body can easily quote them, so numeric fields are accepted
    as either. Bar time arrives as epoch milliseconds, which avoids the
    date-format escaping problems of building an ISO string inside Pine.
    """
    if not isinstance(payload, dict):
        raise WebhookError("payload must be a JSON object")

    def number(key: str, required: bool = True) -> float | None:
        if key not in payload or payload[key] is None:
            if required:
                raise WebhookError(f"missing required field {key!r}")
            return None
        raw = payload[key]
        if isinstance(raw, bool):
            raise WebhookError(f"field {key!r} must be numeric")
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return float(raw.strip().replace(",", ""))
            except ValueError as exc:
                raise WebhookError(f"field {key!r} is not numeric: {raw!r}") from exc
        raise WebhookError(f"field {key!r} has unsupported type {type(raw).__name__}")

    ts = _parse_bar_time(payload)
    volume = number("volume", required=False)

    try:
        bar = Bar(
            ts=ts,
            open=number("open"),
            high=number("high"),
            low=number("low"),
            close=number("close"),
            volume=volume if volume is not None else 0.0,
        )
    except ValueError as exc:
        # Bar's own OHLC consistency check; surface it as a webhook error.
        raise WebhookError(str(exc)) from exc

    meta = {
        "symbol": payload.get("symbol") or payload.get("ticker") or "",
        "interval": str(payload.get("interval") or ""),
        "tv_action": payload.get("tv_action") or "",
        "tv_setup": payload.get("tv_setup") or "",
        "tv_regime": payload.get("tv_regime") or "",
        "tv_score": payload.get("tv_score"),
    }
    return bar, meta


def _parse_bar_time(payload: dict) -> datetime:
    """Read the bar's open time from whichever field the alert carries."""
    for key in ("time", "bar_time", "timestamp"):
        if key not in payload or payload[key] is None:
            continue
        raw = payload[key]
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            epoch = float(raw)
            if epoch > 1e11:  # milliseconds, which is what Pine's `time` is
                epoch /= 1000.0
            return datetime.fromtimestamp(epoch, tz=ET)
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                continue
            try:
                epoch = float(text)
            except ValueError:
                pass
            else:
                if epoch > 1e11:
                    epoch /= 1000.0
                return datetime.fromtimestamp(epoch, tz=ET)
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise WebhookError(f"unrecognised bar time {raw!r}") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(ET)
    raise WebhookError("payload has no bar time ('time')")


@dataclass
class LivePosition:
    """An open position, so price marks can be turned into open P&L."""

    side: int          # +1 long, -1 short
    quantity: int
    entry: float

    def open_pnl(self, price: float, spec: ContractSpec) -> float:
        move = (price - self.entry) * self.side
        return spec.points_to_dollars(move, self.quantity)

    def as_dict(self) -> dict:
        return {
            "side": "long" if self.side > 0 else "short",
            "quantity": self.quantity,
            "entry": self.entry,
        }


@dataclass
class WebhookContext:
    """Everything a request handler needs, shared under one lock."""

    config: WebhookConfig
    spec: ContractSpec
    state: AccountState
    store: BarStore
    engine: RiskEngine | None = None
    playbook: PlaybookConfig | None = None
    indicators: IndicatorConfig | None = None
    on_directive: Callable[[Directive, dict], None] | None = None
    on_mark: Callable[[dict], None] | None = None

    position: LivePosition | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_directive: Directive | None = None
    last_meta: dict = field(default_factory=dict)
    last_price: float | None = None
    received: int = 0
    marks: int = 0
    rejected: int = 0

    # -- real-time marking -------------------------------------------------

    def mark_price(self, price: float) -> dict:
        """Update open P&L from a live price and let the threshold ratchet.

        The Apex intraday threshold follows peak equity in real time, including
        unrealised profit. Waiting for a bar to close before marking means the
        copilot reports room the account no longer has: a spike two minutes into
        a five-minute bar has already moved the threshold. This is the endpoint
        that keeps the two in step.
        """
        with self.lock:
            self.last_price = price
            open_pnl = (
                self.position.open_pnl(price, self.spec) if self.position else 0.0
            )
            self.state.mark(open_pnl)
            self.marks += 1
            snapshot = self.account_snapshot()
        if self.on_mark is not None:
            self.on_mark(snapshot)
        return snapshot

    def mark_pnl(self, open_pnl: float) -> dict:
        """Update open P&L directly, for feeds that report P&L not price."""
        with self.lock:
            self.state.mark(open_pnl)
            self.marks += 1
            snapshot = self.account_snapshot()
        if self.on_mark is not None:
            self.on_mark(snapshot)
        return snapshot

    def set_position(self, position: LivePosition | None) -> dict:
        with self.lock:
            self.position = position
            if position is None:
                self.state.mark(0.0)
            elif self.last_price is not None:
                self.state.mark(position.open_pnl(self.last_price, self.spec))
            return self.account_snapshot()

    def account_snapshot(self) -> dict:
        """Current account arithmetic. Caller holds the lock, or does not care."""
        state = self.state
        return {
            "equity": round(state.equity, 2),
            "balance": round(state.closed_balance, 2),
            "open_pnl": round(state.open_pnl, 2),
            "threshold": round(state.threshold, 2),
            "room": round(state.room, 2),
            "peak_equity": round(state.peak_equity, 2),
            "threshold_locked": state.threshold_is_locked,
            "breached": state.is_breached,
            "position": self.position.as_dict() if self.position else None,
            "last_price": self.last_price,
        }

    # -- bar ingestion -----------------------------------------------------

    def handle_bar(self, bar: Bar, meta: dict) -> tuple[str, Directive | None]:
        """Ingest a bar and re-evaluate. Returns (disposition, directive)."""
        disposition = self.store.ingest(bar)
        if disposition == "stale":
            return disposition, None

        bars = self.store.snapshot()
        with self.lock:
            self.last_price = bar.close
            if self.position is not None:
                # Mark the bar's favourable extreme before its close. The peak
                # inside the bar already ratcheted the threshold in reality, and
                # marking only the close would understate how much room is gone.
                peak = bar.high if self.position.side > 0 else bar.low
                self.state.mark(self.position.open_pnl(peak, self.spec))
                self.state.mark(self.position.open_pnl(bar.close, self.spec))
            else:
                self.state.mark(0.0)

            directive = evaluate(
                bars,
                self.spec,
                self.state,
                risk=self.engine,
                config=self.playbook,
                indicators=self.indicators,
                in_position=self.position is not None,
            )
            self.last_directive = directive
            self.last_meta = meta
            self.received += 1

        if self.on_directive is not None:
            self.on_directive(directive, meta)
        return disposition, directive


def _parse_position(payload: dict) -> LivePosition | None:
    """Read a position declaration. A flat/empty side clears it."""
    side_raw = str(payload.get("side", "")).strip().lower()
    if side_raw in ("", "flat", "none", "closed"):
        return None
    if side_raw in ("long", "buy", "1", "+1"):
        side = 1
    elif side_raw in ("short", "sell", "-1"):
        side = -1
    else:
        raise WebhookError(f"unknown side {payload.get('side')!r}")

    try:
        quantity = int(payload["quantity"])
        entry = float(payload["entry"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WebhookError("position needs numeric 'quantity' and 'entry'") from exc
    if quantity <= 0:
        raise WebhookError("quantity must be positive")
    return LivePosition(side=side, quantity=quantity, entry=entry)


def _authorised(context: WebhookContext, payload: dict, headers) -> bool:
    """Constant-time check of the shared secret, if one is configured."""
    expected = context.config.secret
    if not expected:
        return True
    supplied = payload.get("secret")
    if not isinstance(supplied, str):
        supplied = headers.get("X-Webhook-Secret", "")
    return hmac.compare_digest(str(supplied), expected)


def create_handler(context: WebhookContext) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to `context`."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "nqcopilot"

        def log_message(self, *args) -> None:  # noqa: A003 - silence stderr noise
            """Suppress the default per-request stderr logging."""

        def _respond(self, code: int, payload: dict, close: bool = False) -> None:
            """Send a JSON response.

            `close` ends the connection, which is required whenever we reply
            without having consumed the request body: on a keep-alive
            connection the unread bytes would be parsed as the next request.
            """
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/health":
                self._respond(
                    200,
                    {
                        "status": "ok",
                        "bars": len(context.store),
                        "received": context.received,
                        "rejected": context.rejected,
                        "symbol": context.spec.symbol,
                    },
                )
                return
            if path == "/decision":
                directive = context.last_directive
                if directive is None:
                    self._respond(404, {"error": "no decision yet"})
                    return
                from .cli import directive_to_dict

                self._respond(200, directive_to_dict(directive, context.state))
                return
            if path == "/account":
                # Cheap enough to poll every second for a live room readout.
                self._respond(200, context.account_snapshot())
                return
            self._respond(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            known = {context.config.path.rstrip("/"), "/mark", "/position"}
            if path not in known:
                self._respond(404, {"error": "not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "") or 0)
            except ValueError:
                self._respond(400, {"error": "bad Content-Length"}, close=True)
                return
            if length <= 0:
                self._respond(411, {"error": "Content-Length required"}, close=True)
                return
            if length > context.config.max_body_bytes:
                # Refuse without reading the body, so a large post cannot be
                # used to make the process allocate memory on demand.
                context.rejected += 1
                self._respond(413, {"error": "payload too large"}, close=True)
                return

            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                context.rejected += 1
                self._respond(400, {"error": f"invalid JSON: {exc}"})
                return

            if not _authorised(context, payload if isinstance(payload, dict) else {}, self.headers):
                context.rejected += 1
                # Deliberately vague: do not confirm whether a secret is set.
                self._respond(401, {"error": "unauthorised"})
                return

            # --- real-time mark: price or P&L between bar closes -----------
            if path == "/mark":
                try:
                    if "price" in payload and payload["price"] is not None:
                        snapshot = context.mark_price(float(payload["price"]))
                    elif "open_pnl" in payload and payload["open_pnl"] is not None:
                        snapshot = context.mark_pnl(float(payload["open_pnl"]))
                    else:
                        raise WebhookError("send either 'price' or 'open_pnl'")
                except (TypeError, ValueError) as exc:
                    context.rejected += 1
                    self._respond(400, {"error": str(exc)})
                    return
                self._respond(200, snapshot)
                return

            # --- declare or clear the open position ------------------------
            if path == "/position":
                try:
                    snapshot = context.set_position(_parse_position(payload))
                except WebhookError as exc:
                    context.rejected += 1
                    self._respond(400, {"error": str(exc)})
                    return
                self._respond(200, snapshot)
                return

            try:
                bar, meta = parse_alert(payload)
            except WebhookError as exc:
                context.rejected += 1
                self._respond(400, {"error": str(exc)})
                return

            disposition, directive = context.handle_bar(bar, meta)
            if directive is None:
                self._respond(200, {"status": disposition, "bars": len(context.store)})
                return

            self._respond(
                200,
                {
                    "status": disposition,
                    "bars": len(context.store),
                    "action": directive.action.name,
                    "headline": directive.headline,
                },
            )

    return Handler


class WebhookServer:
    """A small HTTP server that turns TradingView alerts into decisions."""

    def __init__(self, context: WebhookContext) -> None:
        self.context = context
        self._server = ThreadingHTTPServer(
            (context.config.host, context.config.port), create_handler(context)
        )
        self._server.daemon_threads = True

    @property
    def port(self) -> int:
        """The bound port, which matters when configured with port 0."""
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.context.config.host}:{self.port}{self.context.config.path}"

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
