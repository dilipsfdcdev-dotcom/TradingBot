"""Web dashboard: live account info, open trades, agent status, equity curve,
economic calendar and the trade journal. Read-only view of the fleet plus an
emergency stop endpoint.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from bot.orchestrator import Orchestrator

STATIC_DIR = Path(__file__).parent / "static"


def create_app(fleet: Orchestrator) -> FastAPI:
    app = FastAPI(title="Scalping Fleet Dashboard")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    async def api_state():
        s = fleet.state
        return JSONResponse(
            {
                "uptime_seconds": int(time.time() - s.started_at),
                "market_open": s.market_open,
                "connected": fleet.broker.connected,
                "account": {
                    k: s.account.get(k)
                    for k in ("balance", "equity", "margin", "freeMargin", "currency", "leverage", "broker", "name")
                },
                "risk": s.risk,
                "positions": [
                    {
                        "id": p.get("id"),
                        "symbol": p.get("symbol"),
                        "type": "BUY" if p.get("type") == "POSITION_TYPE_BUY" else "SELL",
                        "volume": p.get("volume"),
                        "openPrice": p.get("openPrice"),
                        "currentPrice": p.get("currentPrice"),
                        "stopLoss": p.get("stopLoss"),
                        "takeProfit": p.get("takeProfit"),
                        "profit": p.get("profit"),
                        "time": str(p.get("time", "")),
                    }
                    for p in s.positions
                ],
                "agents": {k: asdict(v) for k, v in s.agents.items()},
                "equity_curve": s.equity_curve[-720:],
            }
        )

    @app.get("/api/news")
    async def api_news():
        return JSONResponse({"events": fleet.news.upcoming(48)})

    @app.get("/api/journal")
    async def api_journal():
        return JSONResponse({"entries": fleet.journal.tail(200)})

    @app.post("/api/emergency-stop")
    async def emergency_stop():
        """Manual kill-switch: halt trading and flatten all bot positions."""
        fleet.risk.kill_switch = True
        fleet.risk.halt_reason = "manual emergency stop"
        await fleet._flatten_all("manual emergency stop")
        fleet.journal.record("halt", reason="manual emergency stop")
        return JSONResponse({"ok": True})

    return app
