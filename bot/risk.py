"""Risk management: auto lot sizing from equity, daily loss limit,
drawdown kill-switch, exposure caps. The agents must pass every gate
here before an order is allowed out.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from .config import RiskConfig

log = logging.getLogger("risk")


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.day_start_equity: float | None = None
        self.day_key: str | None = None
        self.peak_equity: float = 0.0
        self.kill_switch = False
        self.halt_reason = ""

    # ------------------------------------------------------------------ #
    # Equity guards
    # ------------------------------------------------------------------ #
    def update_equity(self, equity: float) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.day_key != today:
            self.day_key = today
            self.day_start_equity = equity
            if self.halt_reason == "daily loss limit":
                self.halt_reason = ""  # new day, daily halt resets
        self.peak_equity = max(self.peak_equity, equity)

        if self.day_start_equity and equity < self.day_start_equity * (
            1 - self.cfg.max_daily_loss
        ):
            if self.halt_reason != "daily loss limit":
                log.warning(
                    "DAILY LOSS LIMIT hit (%.2f -> %.2f). Trading paused until tomorrow.",
                    self.day_start_equity,
                    equity,
                )
            self.halt_reason = "daily loss limit"

        if self.peak_equity and equity < self.peak_equity * (1 - self.cfg.max_drawdown):
            if not self.kill_switch:
                log.error(
                    "MAX DRAWDOWN breached (peak %.2f -> %.2f). KILL SWITCH ON.",
                    self.peak_equity,
                    equity,
                )
            self.kill_switch = True
            self.halt_reason = "max drawdown kill-switch"

    @property
    def trading_allowed(self) -> bool:
        return not self.kill_switch and self.halt_reason == ""

    # ------------------------------------------------------------------ #
    # Pre-trade gates
    # ------------------------------------------------------------------ #
    def can_open(
        self,
        symbol: str,
        open_positions_total: int,
        open_positions_symbol: int,
        account: dict,
    ) -> tuple[bool, str]:
        if not self.trading_allowed:
            return False, self.halt_reason
        if open_positions_total >= self.cfg.max_open_positions:
            return False, "max open positions reached"
        if open_positions_symbol >= self.cfg.max_positions_per_symbol:
            return False, f"max positions on {symbol} reached"
        equity = float(account.get("equity", 0) or 0)
        free = float(account.get("freeMargin", 0) or 0)
        if equity <= 0:
            return False, "no equity"
        if free < equity * self.cfg.min_free_margin_pct:
            return False, "free margin too low"
        return True, "ok"

    # ------------------------------------------------------------------ #
    # Position sizing
    # ------------------------------------------------------------------ #
    def position_size(self, equity: float, sl_distance: float, spec: dict) -> float:
        """Lots such that hitting the SL loses ~risk_per_trade of equity."""
        digits = int(spec.get("digits", 5))
        tick_size = float(spec.get("tickSize") or 10**-digits)
        tick_value = float(spec.get("tickValue") or 0)
        contract_size = float(spec.get("contractSize") or 100_000)
        min_vol = float(spec.get("minVolume") or 0.01)
        max_vol = float(spec.get("maxVolume") or 100.0)
        step = float(spec.get("volumeStep") or 0.01)

        if sl_distance <= 0 or tick_size <= 0:
            return 0.0

        # Loss per lot if SL is hit
        if tick_value > 0:
            loss_per_lot = (sl_distance / tick_size) * tick_value
        else:
            # Fallback: assumes account currency == quote currency
            loss_per_lot = sl_distance * contract_size
        if loss_per_lot <= 0:
            return 0.0

        lots = (equity * self.cfg.risk_per_trade) / loss_per_lot
        lots = min(lots, self.cfg.max_lots, max_vol)
        lots = math.floor(lots / step) * step
        lots = round(lots, 2)
        if lots < min_vol:
            return 0.0
        return lots
