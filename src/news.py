"""
News & economic-calendar filter.

Two jobs:
  1. CALENDAR — pull this week's scheduled high-impact events (interest-rate
     decisions, NFP, CPI ...). Around those, volatility explodes and spreads
     blow out, so the bot stops opening NEW trades in a window before/after.
     Source: the free Forex Factory weekly JSON mirror (no key needed).
  2. HEADLINES — pull live market headlines for the dashboard. Uses NewsAPI if
     a key is provided, otherwise free RSS feeds.

Everything is best-effort: if a feed is down, the bot keeps trading (it just
loses the news filter rather than halting).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import requests

from .logger import get_logger

log = get_logger("news")

_FF_CALENDAR = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_RSS_FEEDS = [
    "https://www.investing.com/rss/news_301.rss",   # forex
    "https://www.investing.com/rss/commodities_Gold.rss",
]

# which currencies matter for each symbol (used to filter calendar events)
_SYMBOL_CCY = {
    "XAU": ["USD"], "GOLD": ["USD"],
    "BTC": ["USD"], "ETH": ["USD"],
}


def _ccy_for_symbol(symbol: str) -> list[str]:
    s = symbol.upper()
    for key, ccys in _SYMBOL_CCY.items():
        if key in s:
            return ccys
    return ["USD"]


class NewsService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ncfg = cfg["news"]
        self.events: list[dict] = []
        self.headlines: list[dict] = []
        self._last_refresh = 0.0

    # ── calendar ─────────────────────────────────────────────────────────
    def _fetch_calendar(self) -> None:
        try:
            r = requests.get(_FF_CALENDAR, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            raw = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("Calendar fetch failed: %s", exc)
            return

        events = []
        for e in raw:
            impact = str(e.get("impact", "")).lower()
            if self.ncfg["high_impact_only"] and impact != "high":
                continue
            ts = e.get("date")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                continue
            events.append({
                "title": e.get("title", ""),
                "country": e.get("country", ""),
                "impact": impact,
                "time": dt.astimezone(timezone.utc),
            })
        self.events = sorted(events, key=lambda x: x["time"])
        log.info("Loaded %d high-impact calendar events", len(self.events))

    # ── headlines ──────────────────────────────────────────────────────
    def _fetch_headlines(self) -> None:
        items: list[dict] = []
        if self.cfg.newsapi_key:
            try:
                r = requests.get(
                    "https://newsapi.org/v2/everything",
                    params={"q": "gold OR bitcoin OR forex OR \"federal reserve\"",
                            "sortBy": "publishedAt", "language": "en",
                            "pageSize": 20, "apiKey": self.cfg.newsapi_key},
                    timeout=15,
                )
                for a in r.json().get("articles", []):
                    items.append({"title": a["title"], "source": a["source"]["name"],
                                  "time": a["publishedAt"], "url": a["url"]})
            except Exception as exc:  # noqa: BLE001
                log.warning("NewsAPI fetch failed: %s", exc)

        if not items:
            try:
                import feedparser
                for feed in _RSS_FEEDS:
                    parsed = feedparser.parse(feed)
                    for entry in parsed.entries[:10]:
                        items.append({
                            "title": entry.get("title", ""),
                            "source": parsed.feed.get("title", "RSS"),
                            "time": entry.get("published", ""),
                            "url": entry.get("link", ""),
                        })
            except Exception as exc:  # noqa: BLE001
                log.warning("RSS fetch failed: %s", exc)

        self.headlines = items[:25]

    def refresh(self, force: bool = False) -> None:
        if not self.ncfg["enabled"]:
            return
        if not force and time.time() - self._last_refresh < self.ncfg["refresh_seconds"]:
            return
        self._fetch_calendar()
        self._fetch_headlines()
        self._last_refresh = time.time()

    # ── the actual filter ─────────────────────────────────────────────────
    def is_blocked(self, symbol: str, now: datetime | None = None) -> tuple[bool, str]:
        """True if we're inside the no-trade window of a relevant high-impact event."""
        if not self.ncfg["enabled"]:
            return False, ""
        now = now or datetime.now(timezone.utc)
        before = timedelta(minutes=self.ncfg["block_minutes_before"])
        after = timedelta(minutes=self.ncfg["block_minutes_after"])
        ccys = _ccy_for_symbol(symbol)

        for e in self.events:
            if e["country"] not in ccys:
                continue
            if e["time"] - before <= now <= e["time"] + after:
                return True, f"{e['title']} ({e['country']}) at {e['time']:%H:%M} UTC"
        return False, ""

    def upcoming(self, hours: int = 24) -> list[dict]:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=hours)
        return [e for e in self.events if now <= e["time"] <= horizon]
