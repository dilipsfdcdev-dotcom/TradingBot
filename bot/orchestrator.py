"""Orchestrator: connects to MT5, spawns one agent per symbol, and runs the
supporting loops (trailing stops, risk/equity guard, news refresh, watchdog).
Designed to run unattended 24/7 - every loop self-heals on errors.
"""
from __future__ import annotations

import asyncio
import logging

from .agent import SymbolAgent
from .broker import Broker
from .config import Settings
from .journal import Journal
from .news import NewsService, is_forex_market_open
from .notifier import Notifier
from .risk import RiskManager
from .state import FleetState
from .trailing import TrailingStopManager

log = logging.getLogger("fleet")


class Orchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.broker = Broker(settings.metaapi_token, settings.metaapi_account_id)
        self.risk = RiskManager(settings.risk)
        self.news = NewsService(settings.news)
        self.journal = Journal()
        self.notifier = Notifier(settings.telegram_bot_token, settings.telegram_chat_id)
        self.state = FleetState()
        self.trailing = TrailingStopManager(self.broker, settings.trading.magic)
        self.agent_cfgs = {a.symbol: a for a in settings.agents}
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        log.info("Connecting to MT5 via MetaApi...")
        await self.broker.connect()
        try:
            await self.news.refresh()
        except Exception as exc:
            log.warning("Initial news fetch failed: %s", exc)

        self.journal.record("startup", symbols=list(self.agent_cfgs))
        await self.notifier.send(
            f"🤖 Scalping fleet online: {', '.join(self.agent_cfgs)}"
        )

        for cfg in self.settings.agents:
            agent = SymbolAgent(
                cfg,
                self.settings.trading,
                self.broker,
                self.risk,
                self.news,
                self.trailing,
                self.state,
                self.journal,
                self.notifier,
            )
            self._tasks.append(asyncio.create_task(agent.run(), name=f"agent-{cfg.symbol}"))

        self._tasks += [
            asyncio.create_task(self._trailing_loop(), name="trailing"),
            asyncio.create_task(self._equity_guard_loop(), name="equity-guard"),
            asyncio.create_task(self._watchdog_loop(), name="watchdog"),
            asyncio.create_task(self.news.run(), name="news"),
        ]
        log.info("Fleet running: %d agents + 4 service loops", len(self.settings.agents))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.broker.close()
        self.journal.record("shutdown")

    # ------------------------------------------------------------------ #
    async def _trailing_loop(self) -> None:
        while True:
            try:
                if self.broker.connected:
                    await self.trailing.manage(self.agent_cfgs)
            except Exception as exc:
                log.warning("Trailing loop error: %s", exc)
            await asyncio.sleep(self.settings.trading.trail_seconds)

    async def _equity_guard_loop(self) -> None:
        """Tracks equity, enforces daily-loss/drawdown halts, feeds the dashboard."""
        was_allowed = True
        while True:
            try:
                if self.broker.connected:
                    account = self.broker.account_info()
                    equity = float(account.get("equity", 0) or 0)
                    if equity > 0:
                        self.risk.update_equity(equity)
                        self.state.push_equity(equity)
                    self.state.account = account
                    self.state.positions = self.broker.positions(magic=self.settings.trading.magic)
                    self.state.market_open = is_forex_market_open()
                    self.state.risk = {
                        "trading_allowed": self.risk.trading_allowed,
                        "halt_reason": self.risk.halt_reason,
                        "kill_switch": self.risk.kill_switch,
                        "day_start_equity": self.risk.day_start_equity,
                        "peak_equity": self.risk.peak_equity,
                    }

                    if was_allowed and not self.risk.trading_allowed:
                        self.journal.record("halt", reason=self.risk.halt_reason)
                        await self.notifier.send(f"⛔ Trading halted: {self.risk.halt_reason}")
                        if self.risk.kill_switch:
                            await self._flatten_all("kill-switch")
                    was_allowed = self.risk.trading_allowed
            except Exception as exc:
                log.warning("Equity guard error: %s", exc)
            await asyncio.sleep(5)

    async def _flatten_all(self, reason: str) -> None:
        """Close every bot position immediately (used by the kill-switch)."""
        for pos in self.broker.positions(magic=self.settings.trading.magic):
            try:
                await self.broker.close_position(pos["id"])
                self.journal.record(
                    "close", symbol=pos["symbol"], position_id=pos["id"],
                    profit=pos.get("profit"), reason=reason,
                )
            except Exception as exc:
                log.error("Failed to flatten %s: %s", pos.get("id"), exc)

    async def _watchdog_loop(self) -> None:
        while True:
            try:
                await self.broker.ensure_connected()
            except Exception as exc:
                log.error("Watchdog error: %s", exc)
            await asyncio.sleep(30)
