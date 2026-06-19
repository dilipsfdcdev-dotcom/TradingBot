"""
Risk management.

Responsibilities:
  - size each position so the loss-at-stop equals `risk_per_trade` x balance
  - compute initial SL / TP from ATR
  - enforce daily loss limit, daily profit target, and the global drawdown
    kill-switch.

Lot sizing maths:
  risk_money       = balance * risk_per_trade
  sl_distance       = sl_atr_mult * ATR          (price units)
  loss_per_lot      = (sl_distance / tick_size) * tick_value
  lot               = risk_money / loss_per_lot
then snapped to the broker's lot step and clamped to [min_lot, max_lot].
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .logger import get_logger

log = get_logger("risk")


@dataclass
class SymbolSpec:
    """The broker-specific numbers we need to size a trade correctly."""
    tick_size: float        # smallest price increment
    tick_value: float       # account-currency value of one tick per 1.0 lot
    point: float            # value of 1 point
    digits: int
    volume_min: float
    volume_max: float
    volume_step: float


def compute_levels(entry: float, atr: float, direction: str, exits_cfg: dict):
    """Return (sl_price, tp_price) from ATR multiples."""
    sl_dist = exits_cfg["sl_atr_mult"] * atr
    tp_dist = exits_cfg["tp_atr_mult"] * atr
    if direction == "BUY":
        return entry - sl_dist, entry + tp_dist
    return entry + sl_dist, entry - tp_dist


def position_size(
    balance: float,
    risk_per_trade: float,
    entry: float,
    sl: float,
    spec: SymbolSpec,
    risk_cfg: dict,
) -> float:
    """Compute lot size so worst-case loss ~= balance * risk_per_trade."""
    risk_money = balance * risk_per_trade
    sl_distance = abs(entry - sl)
    if sl_distance <= 0 or spec.tick_size <= 0 or spec.tick_value <= 0:
        log.warning("Cannot size position: bad inputs; using min lot")
        return max(spec.volume_min, risk_cfg["min_lot"])

    loss_per_lot = (sl_distance / spec.tick_size) * spec.tick_value
    if loss_per_lot <= 0:
        return max(spec.volume_min, risk_cfg["min_lot"])

    raw_lot = risk_money / loss_per_lot

    step = spec.volume_step or risk_cfg["lot_step"]
    lot = math.floor(raw_lot / step) * step
    lot = max(lot, spec.volume_min, risk_cfg["min_lot"])
    lot = min(lot, spec.volume_max, risk_cfg["max_lot"])
    return round(lot, 2)


class RiskGate:
    """Tracks daily/equity state and decides whether new trades are allowed."""

    def __init__(self, risk_cfg: dict):
        self.cfg = risk_cfg
        self.day = None
        self.day_start_equity = None
        self.peak_equity = None
        self.halted = False
        self.halt_reason = ""

    def _roll_day(self, now, equity: float):
        d = now.date()
        if self.day != d:
            self.day = d
            self.day_start_equity = equity
            log.info("New trading day %s — start equity %.2f", d, equity)

    def update(self, now, equity: float) -> None:
        self._roll_day(now, equity)
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity

    def can_trade(self, now, equity: float, open_count: int) -> tuple[bool, str]:
        self.update(now, equity)

        if self.halted:
            return False, self.halt_reason

        # global drawdown kill-switch (persists until manual restart)
        if self.peak_equity and self.cfg["max_drawdown_stop"] > 0:
            dd = (self.peak_equity - equity) / self.peak_equity
            if dd >= self.cfg["max_drawdown_stop"]:
                self.halted = True
                self.halt_reason = f"KILL SWITCH: drawdown {dd:.1%} from peak"
                log.critical(self.halt_reason)
                return False, self.halt_reason

        if open_count >= self.cfg["max_open_trades"]:
            return False, "max open trades reached"

        if self.day_start_equity:
            change = (equity - self.day_start_equity) / self.day_start_equity
            if change <= -self.cfg["daily_loss_limit"]:
                return False, f"daily loss limit hit ({change:.1%})"
            if (
                self.cfg["daily_profit_target"] > 0
                and change >= self.cfg["daily_profit_target"]
            ):
                return False, f"daily profit target hit ({change:.1%}) — resting"

        return True, "ok"
