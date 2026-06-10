"""Trailing-stop engine.

Runs on a fast loop independent of the agents' decision loops:
  1. Break-even: once profit >= breakeven_atr * ATR, lift SL to entry (+buffer).
  2. Trailing: once profit >= trail_activate_atr * ATR, trail the SL
     trail_distance_atr * ATR behind price. The stop only ever tightens.
"""
from __future__ import annotations

import logging

from .broker import Broker
from .config import AgentConfig

log = logging.getLogger("trailing")


class TrailingStopManager:
    def __init__(self, broker: Broker, magic: int):
        self.broker = broker
        self.magic = magic
        # symbol -> latest ATR published by that symbol's agent
        self.atr_by_symbol: dict[str, float] = {}

    def update_atr(self, symbol: str, atr_value: float) -> None:
        self.atr_by_symbol[symbol] = atr_value

    async def manage(self, agent_cfgs: dict[str, AgentConfig]) -> None:
        for pos in self.broker.positions(magic=self.magic):
            symbol = pos.get("symbol", "")
            cfg = agent_cfgs.get(symbol)
            a = self.atr_by_symbol.get(symbol, 0.0)
            if not cfg or a <= 0:
                continue
            try:
                await self._manage_position(pos, cfg, a)
            except Exception as exc:
                log.warning("Trailing update failed for %s: %s", pos.get("id"), exc)

    async def _manage_position(self, pos: dict, cfg: AgentConfig, a: float) -> None:
        price_info = self.broker.price(pos["symbol"])
        if not price_info:
            return
        spec = self.broker.specification(pos["symbol"])
        digits = int(spec.get("digits", 5))
        tick = float(spec.get("tickSize") or 10**-digits)

        is_buy = pos.get("type") == "POSITION_TYPE_BUY"
        entry = float(pos.get("openPrice", 0))
        current_sl = pos.get("stopLoss")
        price = float(price_info["bid"] if is_buy else price_info["ask"])
        profit_dist = (price - entry) if is_buy else (entry - price)

        new_sl = None

        # Stage 1: break-even
        if profit_dist >= cfg.breakeven_atr * a:
            be = entry + (2 * tick if is_buy else -2 * tick)
            if current_sl is None or (is_buy and be > current_sl) or (
                not is_buy and be < current_sl
            ):
                new_sl = be

        # Stage 2: ATR trail (overrides break-even when tighter)
        if profit_dist >= cfg.trail_activate_atr * a:
            trail = price - cfg.trail_distance_atr * a if is_buy else price + cfg.trail_distance_atr * a
            ref = new_sl if new_sl is not None else current_sl
            if ref is None or (is_buy and trail > ref) or (not is_buy and trail < ref):
                new_sl = trail

        if new_sl is None:
            return
        new_sl = round(new_sl, digits)
        # Skip no-op updates (broker rejects modifications smaller than a tick)
        if current_sl is not None and abs(new_sl - float(current_sl)) < tick:
            return

        await self.broker.modify_position(pos["id"], new_sl, pos.get("takeProfit"))
        log.info(
            "Trailing %s %s: SL %s -> %s",
            pos["symbol"],
            "BUY" if is_buy else "SELL",
            current_sl,
            new_sl,
        )
