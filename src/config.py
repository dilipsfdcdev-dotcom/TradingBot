"""
Configuration loader.

Merges the YAML config (strategy / risk / engine settings) with secrets and
mode flags pulled from the environment (.env). Everything downstream reads
from the single `Config` object returned by `load_config()`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)

    # secrets / mode
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_path: str = ""
    trading_mode: str = "DEMO"
    newsapi_key: str = ""
    dashboard_port: int = 8501

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def is_live(self) -> bool:
        return self.trading_mode.upper() == "LIVE"

    @property
    def db_path(self) -> str:
        return str(ROOT / self.raw["storage"]["db_path"])

    @property
    def log_dir(self) -> str:
        return str(ROOT / self.raw["storage"]["log_dir"])


def load_config(path: str | None = None) -> Config:
    """Load config.yaml + .env into a single Config object."""
    load_dotenv(ROOT / ".env")

    cfg_path = Path(path) if path else ROOT / "config" / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    def _int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, default))
        except (TypeError, ValueError):
            return default

    return Config(
        raw=raw,
        mt5_login=_int("MT5_LOGIN", 0),
        mt5_password=os.getenv("MT5_PASSWORD", ""),
        mt5_server=os.getenv("MT5_SERVER", ""),
        mt5_path=os.getenv("MT5_PATH", ""),
        trading_mode=os.getenv("TRADING_MODE", "DEMO"),
        newsapi_key=os.getenv("NEWSAPI_KEY", ""),
        dashboard_port=_int("DASHBOARD_PORT", 8501),
    )
