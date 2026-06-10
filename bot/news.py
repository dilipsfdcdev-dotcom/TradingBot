"""Economic-calendar news guard + feed for the dashboard.

Uses the free Forex Factory weekly calendar JSON feed. Events are matched
to symbols by currency, and trading on a symbol is paused in a window
around high-impact events.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from .config import NewsConfig

log = logging.getLogger("news")

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
REFRESH_SECONDS = 1800  # refresh the calendar every 30 minutes


def _currencies_of(symbol: str) -> set[str]:
    s = symbol.upper()
    cur = {s[i : i + 3] for i in (0, 3) if len(s) >= i + 3}
    if "XAU" in cur:
        cur.discard("XAU")
        cur.add("USD")  # gold trades off USD news
    return cur


class NewsService:
    def __init__(self, cfg: NewsConfig):
        self.cfg = cfg
        self.events: list[dict] = []
        self.last_refresh: datetime | None = None

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                log.warning("Calendar refresh failed: %s", exc)
            await asyncio.sleep(REFRESH_SECONDS)

    async def refresh(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(FF_CALENDAR_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                raw = await resp.json(content_type=None)
        events = []
        for ev in raw:
            try:
                when = datetime.fromisoformat(ev["date"])
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            events.append(
                {
                    "title": ev.get("title", ""),
                    "country": ev.get("country", ""),
                    "impact": ev.get("impact", ""),
                    "time": when,
                    "forecast": ev.get("forecast", ""),
                    "previous": ev.get("previous", ""),
                }
            )
        self.events = sorted(events, key=lambda e: e["time"])
        self.last_refresh = datetime.now(timezone.utc)
        log.info("Calendar refreshed: %d events this week", len(self.events))

    def blocking_event(self, symbol: str) -> dict | None:
        """Return the event blocking this symbol right now, if any."""
        if not self.cfg.enabled:
            return None
        now = datetime.now(timezone.utc)
        before = timedelta(minutes=self.cfg.block_minutes_before)
        after = timedelta(minutes=self.cfg.block_minutes_after)
        currencies = _currencies_of(symbol)
        for ev in self.events:
            if ev["impact"] not in self.cfg.impacts:
                continue
            if ev["country"] not in currencies:
                continue
            if ev["time"] - before <= now <= ev["time"] + after:
                return ev
        return None

    def upcoming(self, hours: int = 24) -> list[dict]:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=hours)
        return [
            {**ev, "time": ev["time"].isoformat()}
            for ev in self.events
            if now - timedelta(hours=1) <= ev["time"] <= horizon
        ]


def is_forex_market_open(now: datetime | None = None) -> bool:
    """FX/gold trade ~Sun 22:00 UTC to Fri 22:00 UTC."""
    now = now or datetime.now(timezone.utc)
    wd = now.weekday()  # Mon=0 ... Sun=6
    if wd == 5:
        return False
    if wd == 4 and now.hour >= 22:
        return False
    if wd == 6 and now.hour < 22:
        return False
    return True
