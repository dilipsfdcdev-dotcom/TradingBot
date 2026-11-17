"""Webhook server: receives TradingView alerts, runs the trailing-stop loop, serves the dashboard."""
from __future__ import annotations

import base64
import hmac
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from .broker import BrokerError
from .config import Settings
from .engine import Engine, SignalError
from .store import SignalStore

log = logging.getLogger("bridge.server")
MAX_BODY = 4096
IST = timezone(timedelta(hours=5, minutes=30))
DASHBOARD_HTML = Path(__file__).parent / "static" / "dashboard.html"


def _trail_loop(engine: Engine, stop: threading.Event, interval: float) -> None:
    last_error = ""
    while not stop.is_set():
        try:
            engine.trail_once()
            last_error = ""
        except Exception as e:  # keep the loop alive whatever happens
            if str(e) != last_error:
                log.error("Trailing loop error: %s", e)
                last_error = str(e)
            stop.wait(2.0)
        stop.wait(interval)


def _describe(result: dict) -> str:
    """One-line human summary of what the engine did with a signal."""
    parts = []
    if result.get("closed"):
        parts.append(f"closed {len(result['closed'])} position(s)")
    if result.get("failed"):
        parts.append(f"FAILED to close {len(result['failed'])}")
    if result.get("side"):
        parts.append(f"opened {result['side']} {result['volume']} lot @ {result['price']}")
    if result.get("reason"):
        parts.append(result["reason"])
    return "; ".join(parts) or "nothing to do"


def create_app(settings: Settings, broker, store: SignalStore | None = None) -> FastAPI:
    store = store or SignalStore()

    def on_event(action: str, symbol: str, result: dict) -> None:
        log.info("AUTO %s %s -> %s", action, symbol, result)
        store.add("bot", action, symbol, result["status"], _describe(result))

    engine = Engine(broker, settings, on_event, lambda: store.get_state("bias"),
                    lambda value: store.set_state("bias", value))
    cache: dict = {"at": 0.0, "trades": []}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        broker.connect()
        stop = threading.Event()
        thread = threading.Thread(target=_trail_loop, args=(engine, stop, settings.trail_poll_ms / 1000.0),
                                  name="trailing", daemon=True)
        thread.start()
        log.info("Bridge ready%s. Default symbol %s, lot %s, SL %s, trail start/distance/step %s/%s/%s",
                 " (DRY RUN - no orders are sent)" if settings.dry_run else "", settings.default_symbol,
                 f"{settings.risk_percent}% risk" if settings.risk_percent > 0 else settings.lot,
                 settings.sl_distance, settings.trail_start, settings.trail_distance, settings.trail_step)
        yield
        stop.set()
        thread.join(timeout=5)
        broker.shutdown()

    app = FastAPI(title="TradingView -> MT5 bridge", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.engine = engine

    @app.get("/health")
    async def health():
        return {"ok": True, "mt5_connected": await run_in_threadpool(broker.is_connected)}

    # ---- webhook ----------------------------------------------------------
    @app.post("/webhook")
    async def webhook(request: Request):
        ip = request.client.host if request.client else "?"
        if settings.allowed_ips and ip not in settings.allowed_ips:
            log.warning("Rejected request from %s (not in ALLOWED_IPS)", ip)
            store.add(ip, "", "", "rejected", "sender IP is not in ALLOWED_IPS")
            raise HTTPException(403, "forbidden")

        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, "payload too large")
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            store.add(ip, "", "", "rejected", "alert message is not valid JSON")
            raise HTTPException(400, "body must be a JSON object")
        action, symbol = data.get("action", ""), data.get("symbol")

        if not hmac.compare_digest(str(data.get("secret", "")).encode(), settings.webhook_secret.encode()):
            log.warning("Rejected request from %s (bad secret)", ip)
            store.add(ip, action, symbol, "rejected", "wrong or missing secret in the alert message")
            raise HTTPException(401, "bad secret")

        sent = data.get("time")
        if sent and settings.max_signal_age_sec > 0:
            try:
                ts = datetime.fromisoformat(str(sent).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - ts).total_seconds()
            except ValueError:
                store.add(ip, action, symbol, "rejected", "bad time field")
                raise HTTPException(400, "bad time field")
            if age > settings.max_signal_age_sec:
                log.warning("Ignored stale signal (%.1fs old): %s", age, action)
                store.add(ip, action, symbol, "ignored", f"stale signal ({age:.1f}s old)")
                return {"status": "ignored", "reason": f"stale signal ({age:.1f}s old)"}

        lot = data.get("lot")
        started = time.perf_counter()
        try:
            result = await run_in_threadpool(engine.handle_signal, action, symbol,
                                             float(lot) if lot not in (None, "") else None)
        except (SignalError, ValueError) as e:
            log.warning("Bad signal from %s: %s", ip, e)
            store.add(ip, action, symbol, "rejected", str(e))
            raise HTTPException(400, str(e))
        except BrokerError as e:
            log.error("Broker error handling %s: %s", action, e)
            store.add(ip, action, symbol, "error", str(e))
            raise HTTPException(503, str(e))
        result["took_ms"] = round((time.perf_counter() - started) * 1000)
        log.info("SIGNAL %s %s -> %s", action, symbol, result)
        store.add(ip, action, symbol, result["status"], _describe(result), result["took_ms"])
        return result

    # ---- dashboard --------------------------------------------------------
    def authorize(request: Request) -> None:
        """This machine may always look; other devices need DASHBOARD_PASSWORD (HTTP basic auth)."""
        ip = request.client.host if request.client else ""
        if ip in ("127.0.0.1", "::1"):
            return
        if not settings.dashboard_password:
            raise HTTPException(404)
        header = request.headers.get("authorization", "")
        try:
            scheme, _, token = header.partition(" ")
            password = base64.b64decode(token).decode().partition(":")[2] if scheme.lower() == "basic" else ""
        except Exception:
            password = ""
        if not password or not hmac.compare_digest(password.encode(), settings.dashboard_password.encode()):
            raise HTTPException(401, "password required", headers={"WWW-Authenticate": 'Basic realm="Aurum Bot"'})

    def build_state() -> dict:
        # "Today" on the dashboard is the IST calendar day.
        day_start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        state: dict = {
            "time": time.time(),
            "dry_run": settings.dry_run,
            "signals": store.recent(200),
            "signals_today": store.count_since(day_start),
            "bias": engine.bias_view(),
            "settings": {
                "symbol": settings.default_symbol, "risk_percent": settings.risk_percent, "lot": settings.lot,
                "max_lot": settings.max_lot, "sl_distance": settings.sl_distance, "tp_distance": settings.tp_distance,
                "trail_start": settings.trail_start, "trail_distance": settings.trail_distance,
                "max_open_positions": settings.max_open_positions,
                "max_positions_per_symbol": settings.max_positions_per_symbol,
                "max_trades_per_day": settings.max_trades_per_day,
                "max_daily_loss_percent": settings.max_daily_loss_percent,
                "breakeven_at": settings.breakeven_at, "reentry_max": settings.reentry_max,
                "reentry_buffer": settings.reentry_buffer, "reentry_cooldown_sec": settings.reentry_cooldown_sec,
            },
        }
        try:
            account = broker.account_summary()
            positions = broker.positions()
            if time.time() - cache["at"] > 5:
                cache["trades"], cache["at"] = broker.closed_trades(30), time.time()
            trades = cache["trades"]
            wins = sum(1 for t in trades if t["profit"] > 0)
            today = [t for t in trades if t["close_time"] >= day_start]
            state.update(
                mt5_connected=True,
                account=account,
                positions=[vars(p) for p in positions],
                trades=trades,
                stats={"trades": len(trades), "wins": wins, "pnl": round(sum(t["profit"] for t in trades), 2),
                       "win_rate": round(wins / len(trades) * 100, 1) if trades else None,
                       "today_trades": len(today), "today_pnl": round(sum(t["profit"] for t in today), 2),
                       "open_pnl": round(sum(p.profit for p in positions), 2)},
                risk={"daily_loss_percent": round(engine.daily_loss_percent(), 2),
                      "kill_switch": engine.kill_switch_active()},
            )
        except Exception as e:
            state.update(mt5_connected=False, error=str(e))
        return state

    @app.get("/api/state")
    async def api_state(request: Request):
        authorize(request)
        return await run_in_threadpool(build_state)

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        authorize(request)
        return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    return app
