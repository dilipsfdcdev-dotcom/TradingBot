"""Settings, loaded from environment variables / the .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


def _s(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _f(name: str, default: float) -> float:
    raw = _s(name)
    return float(raw) if raw else default


def _i(name: str, default: int) -> int:
    raw = _s(name)
    return int(raw) if raw else default


def _b(name: str, default: bool) -> bool:
    raw = _s(name).lower()
    return raw in ("1", "true", "yes", "on") if raw else default


@dataclass(frozen=True)
class Settings:
    webhook_secret: str = "test-secret"
    host: str = "0.0.0.0"
    port: int = 80
    allowed_ips: tuple[str, ...] = ()
    dashboard_password: str = ""

    mt5_path: str = ""
    mt5_portable: bool = False
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    dry_run: bool = False

    default_symbol: str = "XAUUSD"
    symbol_map: dict[str, str] = field(default_factory=lambda: {"XAUUSD": "XAUUSD", "GOLD": "XAUUSD"})
    magic: int = 770011

    # Position sizing
    lot: float = 0.01
    max_lot: float = 1.0
    risk_percent: float = 0.0

    # All distances are in price units (for gold: US dollars of price movement).
    sl_distance: float = 5.0
    tp_distance: float = 0.0
    trail_start: float = 2.0
    trail_distance: float = 1.5
    trail_step: float = 0.2
    breakeven_at: float = 0.0
    breakeven_offset: float = 0.1

    # Re-entry while the last signal still stands (0 = off)
    reentry_max: int = 0
    reentry_buffer: float = 0.5
    reentry_cooldown_sec: float = 60.0

    max_spread: float = 0.5
    deviation_points: int = 200
    # Risk limits (0 = no limit for the per-day ones)
    max_open_positions: int = 1
    max_positions_per_symbol: int = 1
    max_trades_per_day: int = 0
    max_daily_loss_percent: float = 0.0
    max_signal_age_sec: float = 30.0
    dedup_window_sec: float = 3.0
    trail_poll_ms: int = 200


def _parse_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip().upper()] = v.strip()
    return out


def load_settings() -> Settings:
    load_dotenv()
    secret = _s("WEBHOOK_SECRET")
    if len(secret) < 16 or secret.lower().startswith("change"):
        raise SystemExit("WEBHOOK_SECRET must be set in .env to a random string of at least 16 characters.")
    d = Settings()
    default_symbol = _s("DEFAULT_SYMBOL", d.default_symbol)
    symbol_map = _parse_map(_s("SYMBOL_MAP")) or {default_symbol.upper(): default_symbol, "GOLD": default_symbol}
    return Settings(
        webhook_secret=secret,
        host=_s("HOST", d.host),
        port=_i("PORT", d.port),
        allowed_ips=tuple(ip.strip() for ip in _s("ALLOWED_IPS").split(",") if ip.strip()),
        dashboard_password=_s("DASHBOARD_PASSWORD"),
        mt5_path=_s("MT5_PATH"),
        mt5_portable=_b("MT5_PORTABLE", False),
        mt5_login=_i("MT5_LOGIN", 0),
        mt5_password=_s("MT5_PASSWORD"),
        mt5_server=_s("MT5_SERVER"),
        dry_run=_b("DRY_RUN", d.dry_run),
        default_symbol=default_symbol,
        symbol_map=symbol_map,
        magic=_i("MAGIC", d.magic),
        lot=_f("LOT", d.lot),
        max_lot=_f("MAX_LOT", d.max_lot),
        risk_percent=_f("RISK_PERCENT", d.risk_percent),
        sl_distance=_f("SL_DISTANCE", d.sl_distance),
        tp_distance=_f("TP_DISTANCE", d.tp_distance),
        trail_start=_f("TRAIL_START", d.trail_start),
        trail_distance=_f("TRAIL_DISTANCE", d.trail_distance),
        trail_step=_f("TRAIL_STEP", d.trail_step),
        breakeven_at=_f("BREAKEVEN_AT", d.breakeven_at),
        breakeven_offset=_f("BREAKEVEN_OFFSET", d.breakeven_offset),
        reentry_max=_i("REENTRY_MAX", d.reentry_max),
        reentry_buffer=_f("REENTRY_BUFFER", d.reentry_buffer),
        reentry_cooldown_sec=_f("REENTRY_COOLDOWN_SEC", d.reentry_cooldown_sec),
        max_spread=_f("MAX_SPREAD", d.max_spread),
        deviation_points=_i("DEVIATION_POINTS", d.deviation_points),
        max_open_positions=_i("MAX_OPEN_POSITIONS", d.max_open_positions),
        max_positions_per_symbol=_i("MAX_POSITIONS_PER_SYMBOL", d.max_positions_per_symbol),
        max_trades_per_day=_i("MAX_TRADES_PER_DAY", d.max_trades_per_day),
        max_daily_loss_percent=_f("MAX_DAILY_LOSS_PERCENT", d.max_daily_loss_percent),
        max_signal_age_sec=_f("MAX_SIGNAL_AGE_SEC", d.max_signal_age_sec),
        dedup_window_sec=_f("DEDUP_WINDOW_SEC", d.dedup_window_sec),
        trail_poll_ms=_i("TRAIL_POLL_MS", d.trail_poll_ms),
    )
