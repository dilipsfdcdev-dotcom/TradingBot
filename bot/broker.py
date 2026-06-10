"""MT5 connectivity via MetaApi cloud bridge.

Wraps a streaming connection so the fleet gets real-time prices, positions
and account state from the local terminal-state replica, plus order methods.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from metaapi_cloud_sdk import MetaApi

log = logging.getLogger("broker")

# Silence MetaApi SDK's noisy internal retry logging
for name in logging.root.manager.loggerDict:
    if name.startswith("metaapi"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


class Broker:
    def __init__(self, token: str, account_id: str):
        self._token = token
        self._account_id = account_id
        self._api: MetaApi | None = None
        self._account = None
        self.connection = None
        self.terminal_state = None
        self.connected = False

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        self._api = MetaApi(self._token)
        self._account = await self._api.metatrader_account_api.get_account(
            self._account_id
        )

        if self._account.state != "DEPLOYED":
            log.info("Deploying MT5 account (state=%s)...", self._account.state)
            await self._account.deploy()
        log.info("Waiting for MT5 account connection to broker...")
        await self._account.wait_connected()

        self.connection = self._account.get_streaming_connection()
        await self.connection.connect()
        await self.connection.wait_synchronized()
        self.terminal_state = self.connection.terminal_state
        self.connected = True

        info = self.account_info()
        log.info(
            "Connected to MT5: balance=%.2f equity=%.2f currency=%s",
            info.get("balance", 0),
            info.get("equity", 0),
            info.get("currency", "?"),
        )

    async def ensure_connected(self) -> None:
        """Reconnect if synchronization was lost (called by the watchdog)."""
        try:
            if self.terminal_state and self.terminal_state.connected_to_broker:
                self.connected = True
                return
        except Exception:
            pass
        self.connected = False
        log.warning("Broker link lost - reconnecting...")
        try:
            await self.connection.connect()
            await self.connection.wait_synchronized()
            self.connected = True
            log.info("Reconnected to broker.")
        except Exception as exc:
            log.error("Reconnect failed: %s", exc)

    async def subscribe(self, symbol: str) -> None:
        await self.connection.subscribe_to_market_data(symbol)

    async def close(self) -> None:
        if self.connection:
            try:
                await self.connection.close()
            except Exception:
                pass
        self.connected = False

    # ------------------------------------------------------------------ #
    # Market & account state (served from the synchronized local replica)
    # ------------------------------------------------------------------ #
    def account_info(self) -> dict:
        return self.terminal_state.account_information or {}

    def positions(self, symbol: str | None = None, magic: int | None = None) -> list[dict]:
        pos = self.terminal_state.positions or []
        if symbol is not None:
            pos = [p for p in pos if p.get("symbol") == symbol]
        if magic is not None:
            pos = [p for p in pos if p.get("magic") == magic]
        return pos

    def price(self, symbol: str) -> dict | None:
        return self.terminal_state.price(symbol)

    def specification(self, symbol: str) -> dict:
        return self.terminal_state.specification(symbol) or {}

    async def candles(self, symbol: str, timeframe: str, count: int = 300) -> list[dict]:
        start = datetime.now(timezone.utc) - timedelta(days=14)
        return await self._account.get_historical_candles(
            symbol, timeframe, start, limit=count
        )

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #
    async def market_order(
        self,
        symbol: str,
        direction: str,  # "buy" | "sell"
        volume: float,
        stop_loss: float,
        take_profit: float,
        magic: int,
        comment: str = "scalper",
    ) -> dict:
        options = {"magic": magic, "comment": comment[:25]}
        if direction == "buy":
            return await self.connection.create_market_buy_order(
                symbol, volume, stop_loss, take_profit, options
            )
        return await self.connection.create_market_sell_order(
            symbol, volume, stop_loss, take_profit, options
        )

    async def modify_position(
        self, position_id: str, stop_loss: float | None, take_profit: float | None
    ) -> dict:
        return await self.connection.modify_position(position_id, stop_loss, take_profit)

    async def close_position(self, position_id: str) -> dict:
        return await self.connection.close_position(position_id, None)


async def with_retries(coro_factory, attempts: int = 3, base_delay: float = 2.0):
    """Run an async call with exponential-backoff retries."""
    for i in range(attempts):
        try:
            return await coro_factory()
        except Exception as exc:
            if i == attempts - 1:
                raise
            delay = base_delay * (2**i)
            log.warning("Call failed (%s); retry in %.0fs", exc, delay)
            await asyncio.sleep(delay)
