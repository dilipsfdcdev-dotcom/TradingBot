"""Optional Telegram notifications for trades and risk events."""
from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger("notifier")


class Notifier:
    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)

    async def send(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    url,
                    json={"chat_id": self.chat_id, "text": text},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
        except Exception as exc:
            log.warning("Telegram send failed: %s", exc)
