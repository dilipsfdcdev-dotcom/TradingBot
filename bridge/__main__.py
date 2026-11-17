"""Run with:  python -m bridge"""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from .broker import MT5Broker
from .config import load_settings
from .server import create_app


def main() -> None:
    logs = Path("logs")
    logs.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(),
                  RotatingFileHandler(logs / "bridge.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")],
    )
    s = load_settings()
    broker = MT5Broker(s.mt5_path, s.mt5_login, s.mt5_password, s.mt5_server, s.magic, s.deviation_points, s.dry_run,
                       s.mt5_portable)
    uvicorn.run(create_app(s, broker), host=s.host, port=s.port, log_level="warning")


if __name__ == "__main__":
    main()
