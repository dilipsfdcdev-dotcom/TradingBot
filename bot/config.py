"""Configuration loading: config.yaml + .env merged into typed objects."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.005
    max_daily_loss: float = 0.04
    max_drawdown: float = 0.15
    max_open_positions: int = 4
    max_positions_per_symbol: int = 1
    max_lots: float = 2.0
    min_free_margin_pct: float = 0.30


@dataclass
class NewsConfig:
    enabled: bool = True
    block_minutes_before: int = 15
    block_minutes_after: int = 15
    impacts: list[str] = field(default_factory=lambda: ["High"])


@dataclass
class TradingConfig:
    magic: int = 777001
    loop_seconds: int = 5
    trail_seconds: int = 2
    cooldown_minutes: int = 5
    max_trade_age_minutes: int = 240


@dataclass
class AgentConfig:
    symbol: str
    timeframe: str = "5m"
    trend_timeframe: str = "15m"
    max_spread_points: float = 20
    atr_sl_mult: float = 1.5
    atr_tp_mult: float = 2.2
    trail_activate_atr: float = 1.0
    trail_distance_atr: float = 0.8
    breakeven_atr: float = 0.6


@dataclass
class Settings:
    metaapi_token: str
    metaapi_account_id: str
    dashboard_host: str
    dashboard_port: int
    telegram_bot_token: str
    telegram_chat_id: str
    risk: RiskConfig
    news: NewsConfig
    trading: TradingConfig
    agents: list[AgentConfig]


def load_settings(config_path: str | Path = "config.yaml") -> Settings:
    raw = {}
    path = Path(config_path)
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}

    return Settings(
        metaapi_token=os.getenv("METAAPI_TOKEN", ""),
        metaapi_account_id=os.getenv("METAAPI_ACCOUNT_ID", ""),
        dashboard_host=os.getenv("DASHBOARD_HOST", "0.0.0.0"),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8000")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        risk=RiskConfig(**raw.get("risk", {})),
        news=NewsConfig(**raw.get("news", {})),
        trading=TradingConfig(**raw.get("trading", {})),
        agents=[AgentConfig(**a) for a in raw.get("agents", [])],
    )
