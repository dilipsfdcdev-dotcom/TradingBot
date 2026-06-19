#!/usr/bin/env python3
"""
Entry point for the autonomous trading bot.

    python run_bot.py

It connects to MT5, then runs the 24/7 trading loop until you press Ctrl+C.
Configure behaviour in config/config.yaml and secrets in .env.
"""
from __future__ import annotations

import signal
import sys

from src.config import load_config
from src.engine import TradingEngine
from src.logger import get_logger, setup_logging


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_dir)
    log = get_logger("main")

    mode = "LIVE 💰" if cfg.is_live else "DEMO (paper)"
    log.info("=" * 60)
    log.info(" AUTONOMOUS TRADING BOT  |  mode=%s", mode)
    log.info(" symbols=%s  entry_tf=%s  trend_tf=%s",
             [s["name"] for s in cfg["symbols"] if s["enabled"]],
             cfg["timeframes"]["entry"], cfg["timeframes"]["trend"])
    log.info("=" * 60)

    if cfg.is_live:
        log.warning("LIVE MODE: the bot will trade REAL money. Ctrl+C to abort now.")

    engine = TradingEngine(cfg)

    def _shutdown(signum, frame):  # noqa: ARG001
        log.info("Shutdown signal received.")
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        engine.start()
    except KeyboardInterrupt:
        engine.stop()
    except Exception:  # noqa: BLE001
        log.exception("Fatal error")
        engine.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
