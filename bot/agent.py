"""SymbolAgent: one fully autonomous trader per symbol.

Each loop iteration it:
  1. Checks market hours, news blackout, broker link.
  2. Refreshes candles and publishes ATR to the trailing engine.
  3. Manages its open position (momentum-flip exit, max-age exit).
  4. Hunts for a new entry; if every risk gate passes, sizes the lot
     from current equity and submits a market order with SL + TP.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from .broker import Broker
from .config import AgentConfig, TradingConfig
from .indicators import candles_to_df
from .journal import Journal
from .news import NewsService, is_forex_market_open
from .notifier import Notifier
from .risk import RiskManager
from .state import FleetState
from .strategy import ScalpStrategy
from .trailing import TrailingStopManager


class SymbolAgent:
    def __init__(
        self,
        cfg: AgentConfig,
        trading: TradingConfig,
        broker: Broker,
        risk: RiskManager,
        news: NewsService,
        trailing: TrailingStopManager,
        state: FleetState,
        journal: Journal,
        notifier: Notifier,
    ):
        self.cfg = cfg
        self.trading = trading
        self.broker = broker
        self.risk = risk
        self.news = news
        self.trailing = trailing
        self.state = state
        self.journal = journal
        self.notifier = notifier
        self.log = logging.getLogger(f"agent.{cfg.symbol}")
        self.status = state.agent(cfg.symbol)
        self.cooldown_until: float = 0.0
        self._known_position_ids: set[str] = set()

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        self.log.info("Agent online (%s / trend %s)", self.cfg.timeframe, self.cfg.trend_timeframe)
        try:
            await self.broker.subscribe(self.cfg.symbol)
        except Exception as exc:
            self.log.warning("Market-data subscribe failed (will rely on RPC): %s", exc)

        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.set("error", str(exc))
                self.log.error("Step failed: %s", exc)
            await asyncio.sleep(self.trading.loop_seconds)

    # ------------------------------------------------------------------ #
    async def step(self) -> None:
        symbol = self.cfg.symbol

        if not is_forex_market_open():
            self.status.set("paused", "market closed (weekend)")
            return
        if not self.broker.connected:
            self.status.set("paused", "broker reconnecting")
            return

        # --- market snapshot -------------------------------------------------
        price = self.broker.price(symbol)
        spec = self.broker.specification(symbol)
        if not price or not spec:
            self.status.set("paused", "waiting for market data")
            return
        digits = int(spec.get("digits", 5))
        point = 10**-digits
        spread_points = (float(price["ask"]) - float(price["bid"])) / point
        self.status.last_price = float(price["bid"])
        self.status.spread_points = round(spread_points, 1)

        entry_df = candles_to_df(
            await self.broker.candles(symbol, self.cfg.timeframe, 300)
        )
        trend_df = candles_to_df(
            await self.broker.candles(symbol, self.cfg.trend_timeframe, 300)
        )
        if entry_df.empty or trend_df.empty:
            self.status.set("paused", "no candle data")
            return

        strategy = ScalpStrategy(self.cfg.atr_sl_mult, self.cfg.atr_tp_mult)
        self.status.trend = strategy.trend_direction(trend_df)

        from .indicators import atr as atr_fn

        current_atr = float(atr_fn(entry_df, 14).iloc[-1])
        self.status.atr = round(current_atr, digits)
        self.trailing.update_atr(symbol, current_atr)

        # --- manage open position -------------------------------------------
        my_positions = self.broker.positions(symbol=symbol, magic=self.trading.magic)
        self._detect_closes(my_positions)

        if my_positions:
            await self._manage_open(my_positions, entry_df, strategy)
            return

        # --- look for a new entry --------------------------------------------
        blocking = self.news.blocking_event(symbol)
        if blocking:
            self.status.set("paused", f"news blackout: {blocking['country']} {blocking['title']}")
            return
        if time.time() < self.cooldown_until:
            self.status.set("scanning", "cooldown after last trade")
            return
        if spread_points > self.cfg.max_spread_points:
            self.status.set("scanning", f"spread too wide ({spread_points:.0f}pts)")
            return

        signal = strategy.evaluate(entry_df, trend_df)
        if signal is None:
            self.status.set("scanning", f"no setup (trend={self.status.trend})")
            return

        await self._open_trade(signal, price, spec, digits)

    # ------------------------------------------------------------------ #
    async def _open_trade(self, signal, price: dict, spec: dict, digits: int) -> None:
        symbol = self.cfg.symbol
        account = self.broker.account_info()
        total = len(self.broker.positions(magic=self.trading.magic))
        mine = len(self.broker.positions(symbol=symbol, magic=self.trading.magic))

        ok, reason = self.risk.can_open(symbol, total, mine, account)
        if not ok:
            self.status.set("scanning", f"blocked: {reason}")
            return

        equity = float(account.get("equity", 0))
        lots = self.risk.position_size(equity, signal.sl_distance, spec)
        if lots <= 0:
            self.status.set("scanning", "equity too small for min lot at this risk")
            return

        if signal.direction == "buy":
            entry = float(price["ask"])
            sl = round(entry - signal.sl_distance, digits)
            tp = round(entry + signal.tp_distance, digits)
        else:
            entry = float(price["bid"])
            sl = round(entry + signal.sl_distance, digits)
            tp = round(entry - signal.tp_distance, digits)

        self.log.info(
            "ENTRY %s %s %.2f lots @ ~%s SL=%s TP=%s | %s",
            signal.direction.upper(), symbol, lots, round(entry, digits), sl, tp, signal.reason,
        )
        result = await self.broker.market_order(
            symbol, signal.direction, lots, sl, tp, self.trading.magic
        )
        self.status.set("in_trade", f"{signal.direction} {lots} lots")
        self.status.last_signal = f"{signal.direction.upper()} | {signal.reason}"
        self.journal.record(
            "open",
            symbol=symbol,
            direction=signal.direction,
            lots=lots,
            entry=entry,
            sl=sl,
            tp=tp,
            reason=signal.reason,
            order_id=result.get("orderId"),
        )
        await self.notifier.send(
            f"🟢 {symbol} {signal.direction.upper()} {lots} lots @ {entry}\n"
            f"SL {sl} | TP {tp}\n{signal.reason}"
        )

    # ------------------------------------------------------------------ #
    async def _manage_open(self, positions: list[dict], entry_df, strategy: ScalpStrategy) -> None:
        for pos in positions:
            self._known_position_ids.add(pos["id"])
            pos_type = "buy" if pos.get("type") == "POSITION_TYPE_BUY" else "sell"
            self.status.set(
                "in_trade",
                f"{pos_type} {pos.get('volume')} lots, P/L {pos.get('profit', 0):.2f}",
            )

            # Time-based exit: scalps shouldn't become investments
            opened = pos.get("time")
            if opened is not None:
                if isinstance(opened, str):
                    opened = datetime.fromisoformat(opened.replace("Z", "+00:00"))
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                age_min = (datetime.now(timezone.utc) - opened).total_seconds() / 60
                if age_min > self.trading.max_trade_age_minutes:
                    await self._close(pos, f"max age {age_min:.0f}min")
                    continue

            # Momentum-flip exit
            if strategy.exit_signal(entry_df, pos_type):
                await self._close(pos, "momentum flipped")

    async def _close(self, pos: dict, reason: str) -> None:
        self.log.info("EXIT %s (%s): %s", pos["symbol"], pos["id"], reason)
        await self.broker.close_position(pos["id"])
        self.cooldown_until = time.time() + self.trading.cooldown_minutes * 60
        self.journal.record(
            "close",
            symbol=pos["symbol"],
            position_id=pos["id"],
            profit=pos.get("profit"),
            reason=reason,
        )
        await self.notifier.send(
            f"🔻 Closed {pos['symbol']} ({reason}) P/L: {pos.get('profit', 0):.2f}"
        )

    def _detect_closes(self, current_positions: list[dict]) -> None:
        """Notice positions that closed via SL/TP and start the cooldown."""
        current_ids = {p["id"] for p in current_positions}
        gone = self._known_position_ids - current_ids
        if gone:
            self.cooldown_until = time.time() + self.trading.cooldown_minutes * 60
            for pid in gone:
                self.journal.record("closed_by_broker", symbol=self.cfg.symbol, position_id=pid)
            self._known_position_ids = current_ids
