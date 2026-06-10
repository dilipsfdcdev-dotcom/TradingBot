"""Entrypoint: starts the autonomous trading fleet and the web dashboard.

    python main.py            # trade + dashboard at http://localhost:8000
"""
from __future__ import annotations

import asyncio
import logging

import uvicorn

from bot.config import load_settings
from bot.journal import setup_logging
from bot.orchestrator import Orchestrator
from dashboard.server import create_app

log = logging.getLogger("main")


async def amain() -> None:
    setup_logging()
    settings = load_settings()

    if not settings.metaapi_token or not settings.metaapi_account_id:
        log.error(
            "Missing credentials. Copy .env.example to .env and set "
            "METAAPI_TOKEN and METAAPI_ACCOUNT_ID."
        )
        return
    if not settings.agents:
        log.error("No agents configured in config.yaml.")
        return

    fleet = Orchestrator(settings)
    await fleet.start()

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(fleet),
            host=settings.dashboard_host,
            port=settings.dashboard_port,
            log_level="warning",
        )
    )
    log.info(
        "Dashboard: http://%s:%d", settings.dashboard_host, settings.dashboard_port
    )
    try:
        await server.serve()
    finally:
        await fleet.stop()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("Stopped by user.")
