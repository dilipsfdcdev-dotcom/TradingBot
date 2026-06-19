"""
Trading engine — the autonomous 24/7 loop that ties everything together.

Each cycle:
  1. read account, snapshot equity, update the risk gate (+ kill switch)
  2. refresh news/calendar (throttled)
  3. manage existing positions (break-even / trailing / partial TP)
  4. for each enabled symbol, if all entry filters pass, look for a NEW trade
     that has multi-timeframe confluence agreement, then size & execute it.

The loop is defensive: any exception in one cycle is logged and the loop
continues, so a transient data glitch never kills the bot.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

from . import strategy
from .broker import Broker
from .logger import get_logger
from .news import NewsService
from .risk import RiskGate, compute_levels, position_size
from .store import Store
from .trade_manager import TradeManager

log = get_logger("engine")


def market_open(session: str, now: datetime | None = None) -> bool:
    """Lightweight session gate (broker still has final say)."""
    if session == "24/7":
        return True
    now = now or datetime.now(timezone.utc)
    wd, hour = now.weekday(), now.hour  # Mon=0 .. Sun=6
    if session == "metals":  # gold: Sun 22:00 UTC -> Fri 21:00 UTC
        if wd == 5:                      # Saturday
            return False
        if wd == 6 and hour < 22:        # Sunday before open
            return False
        if wd == 4 and hour >= 21:       # Friday after close
            return False
        return True
    return True


class TradingEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.broker = Broker(cfg)
        self.store = Store(cfg.db_path)
        self.news = NewsService(cfg)
        self.risk_gate = RiskGate(cfg["risk"])
        self.trade_mgr = TradeManager(self.broker, cfg["exits"], cfg["engine"])
        self.running = False
        self.magic = cfg["engine"]["magic_number"]

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        if not self.broker.connect():
            self.store.set_status("state", "ERROR: MT5 connection failed")
            raise SystemExit("Could not connect to MT5. See logs.")

        for s in self.cfg["symbols"]:
            if s["enabled"]:
                self.broker.ensure_symbol(s["name"])

        self.store.set_status("mode", "LIVE" if self.cfg.is_live else "DEMO")
        self.store.set_status("state", "RUNNING")
        self.news.refresh(force=True)
        self.running = True
        log.info("Engine started (%s).", "LIVE" if self.cfg.is_live else "DEMO")
        self._loop()

    def stop(self) -> None:
        self.running = False
        self.store.set_status("state", "STOPPED")
        self.broker.shutdown()
        log.info("Engine stopped.")

    # ── main loop ─────────────────────────────────────────────────────────
    def _loop(self) -> None:
        interval = self.cfg["engine"]["loop_interval_seconds"]
        while self.running:
            t0 = time.time()
            try:
                self._cycle()
            except Exception:  # noqa: BLE001
                log.exception("Cycle error (continuing)")
            elapsed = time.time() - t0
            time.sleep(max(0.0, interval - elapsed))

    def _cycle(self) -> None:
        now = datetime.now(timezone.utc)
        acc = self.broker.account()
        self.store.record_equity(acc)
        self.store.set_status("account", acc)
        self.store.set_status("heartbeat", now.isoformat())

        self.news.refresh()
        self.store.store_news(self.news.headlines)
        self.store.set_status("upcoming_news",
                              [{**e, "time": e["time"].isoformat()}
                               for e in self.news.upcoming()])

        positions = self.broker.positions(magic=self.magic)
        self.trade_mgr.manage(positions)
        self.store.snapshot_positions(positions)

        ok, reason = self.risk_gate.can_trade(now, acc["equity"], len(positions))
        self.store.set_status("can_trade", {"ok": ok, "reason": reason})
        if not ok:
            log.debug("Not opening trades: %s", reason)
            return

        for sym_cfg in self.cfg["symbols"]:
            if not sym_cfg["enabled"]:
                continue
            try:
                self._evaluate_symbol(sym_cfg, positions, acc, now)
            except Exception:  # noqa: BLE001
                log.exception("Error evaluating %s", sym_cfg["name"])

    # ── per-symbol logic ─────────────────────────────────────────────────
    def _evaluate_symbol(self, sym_cfg, positions, acc, now):
        symbol = sym_cfg["name"]

        if not market_open(sym_cfg.get("session", "24/7"), now):
            return

        # one position per symbol (configurable)
        held = [p for p in positions if p["symbol"] == symbol]
        if len(held) >= self.cfg["risk"]["max_trades_per_symbol"]:
            return

        # spread guard
        spread = self.broker.spread_points(symbol)
        if spread > sym_cfg.get("max_spread", 1e9):
            log.debug("%s spread %.0f > max %.0f — skip", symbol, spread,
                      sym_cfg["max_spread"])
            return

        # news guard
        blocked, why = self.news.is_blocked(symbol, now)
        if blocked:
            log.info("%s blocked by news: %s", symbol, why)
            self.store.set_status(f"news_block_{symbol}", why)
            return

        # multi-timeframe signals
        params = self.cfg["strategy"]
        trend_df = self.broker.get_rates(
            symbol, self.cfg["timeframes"]["trend"],
            self.cfg["engine"]["bars_to_fetch"])

        actionable = []
        latest_snapshot = {}
        for tf in self.cfg["timeframes"]["entry"]:
            df = self.broker.get_rates(symbol, tf, self.cfg["engine"]["bars_to_fetch"])
            sig = strategy.generate_signal(df, trend_df, params)
            latest_snapshot[tf] = {
                "direction": sig.direction, "score": sig.score,
                "reasons": sig.reasons, "snapshot": sig.snapshot,
            }
            if sig.actionable:
                actionable.append((tf, sig))

        self.store.set_status(f"signals_{symbol}", latest_snapshot)

        if not actionable:
            return

        # require multi-timeframe agreement
        buys = [s for tf, s in actionable if s.direction == "BUY"]
        sells = [s for tf, s in actionable if s.direction == "SELL"]
        n_entry = len(self.cfg["timeframes"]["entry"])
        need = 2 if n_entry >= 2 else 1

        if len(buys) >= need and len(buys) >= len(sells):
            chosen = max(buys, key=lambda s: s.score)
        elif len(sells) >= need and len(sells) > len(buys):
            chosen = max(sells, key=lambda s: s.score)
        else:
            log.debug("%s: no TF agreement (buys=%d sells=%d need=%d)",
                      symbol, len(buys), len(sells), need)
            return

        self._open_trade(sym_cfg, chosen, acc)

    def _open_trade(self, sym_cfg, sig, acc):
        symbol = sym_cfg["name"]
        spec = self.broker.symbol_spec(symbol)
        if spec is None:
            log.error("No symbol spec for %s", symbol)
            return

        ask, bid = self.broker.price(symbol)
        entry = ask if sig.direction == "BUY" else bid
        sl, tp = compute_levels(entry, sig.atr, sig.direction, self.cfg["exits"])

        risk_pct = sym_cfg.get("risk_per_trade", self.cfg["risk"]["risk_per_trade"])
        lot = position_size(acc["balance"], risk_pct, entry, sl, spec, self.cfg["risk"])

        sl = round(sl, spec.digits)
        tp = round(tp, spec.digits)

        ticket = self.broker.open_trade(
            symbol, sig.direction, lot, sl, tp, self.magic,
            self.cfg["engine"]["comment"], self.cfg["engine"]["slippage_points"])

        if ticket:
            self.store.record_event(
                "OPEN", symbol, sig.direction, lot, entry, sl, tp,
                ticket=ticket,
                detail=f"score={sig.score}/{sig.max_score} atr={sig.atr:.5f}")
            self.store.record_signal(sig, symbol, "multi")
            log.info("TRADE %s %s %.2f lots risk=%.1f%% score=%d",
                     sig.direction, symbol, lot, risk_pct * 100, sig.score)
