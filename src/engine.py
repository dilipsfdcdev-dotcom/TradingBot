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
        self._cycle_count = 0

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
        # bank floating profit that hits the money target, then refresh so the
        # symbol can immediately re-enter this same cycle if the signal holds
        if self._take_profit_targets(positions):
            positions = self.broker.positions(magic=self.magic)
        self.trade_mgr.manage(positions)
        self.store.snapshot_positions(positions)

        ok, reason = self.risk_gate.can_trade(now, acc["equity"], len(positions))
        self.store.set_status("can_trade", {"ok": ok, "reason": reason})
        if not ok:
            log.debug("Not opening trades: %s", reason)
            self._heartbeat(acc, positions, {s["name"]: ("paused", reason)
                                             for s in self.cfg["symbols"]
                                             if s["enabled"]})
            return

        statuses: dict[str, tuple[str, str]] = {}
        for sym_cfg in self.cfg["symbols"]:
            if not sym_cfg["enabled"]:
                continue
            try:
                statuses[sym_cfg["name"]] = self._evaluate_symbol(
                    sym_cfg, positions, acc, now)
            except Exception:  # noqa: BLE001
                log.exception("Error evaluating %s", sym_cfg["name"])
                statuses[sym_cfg["name"]] = ("error", "see log")

        self._heartbeat(acc, positions, statuses)

    def _take_profit_targets(self, positions) -> int:
        """Backstop for the fixed money TP/SL (the order's TP/SL is primary).

        Closes a position whose floating profit reached +profit_target_money or
        fell to -stop_loss_money, in case the broker order didn't fill (gaps).
        After a profit close the entry logic re-opens next cycle if the signal
        still qualifies (bank-and-re-enter). Returns the number closed.
        """
        tp_money = self.cfg["exits"].get("profit_target_money", 0)
        sl_money = self.cfg["exits"].get("stop_loss_money", 0)
        if (not tp_money or tp_money <= 0) and (not sl_money or sl_money <= 0):
            return 0
        closed = 0
        for p in positions:
            hit_tp = tp_money and tp_money > 0 and p["profit"] >= tp_money
            hit_sl = sl_money and sl_money > 0 and p["profit"] <= -sl_money
            if not (hit_tp or hit_sl):
                continue
            reason = f"profit +{p['profit']:.0f}" if hit_tp else f"stop {p['profit']:.0f}"
            if self.broker.close_position(
                    p, self.cfg["engine"]["slippage_points"], reason):
                self.store.record_event(
                    "CLOSE", p["symbol"], p["type"], p["volume"],
                    p["price_current"], profit=p["profit"], ticket=p["ticket"],
                    detail=f"money {'TP' if hit_tp else 'SL'} hit")
                log.info("%s %.0f on %s %s",
                         "BANKED" if hit_tp else "STOPPED", p["profit"],
                         p["symbol"], p["type"])
                closed += 1
        return closed

    def _heartbeat(self, acc, positions, statuses: dict[str, tuple[str, str]]) -> None:
        """Persist each symbol's state and log a compact summary periodically."""
        for sym, (state, detail) in statuses.items():
            self.store.set_status(f"state_{sym}",
                                  {"state": state, "detail": detail,
                                   "ts": datetime.now(timezone.utc).isoformat()})
        self._cycle_count += 1
        every = self.cfg["engine"].get("heartbeat_cycles", 12)
        if self._cycle_count % every == 1:
            summary = "  ".join(
                f"{s}={st}" + (f"[{d}]" if d else "")
                for s, (st, d) in statuses.items())
            log.info("♥ equity=%.2f open=%d | %s",
                     acc.get("equity", 0), len(positions), summary or "(idle)")

    # ── per-symbol logic ─────────────────────────────────────────────────
    def _evaluate_symbol(self, sym_cfg, positions, acc, now) -> tuple[str, str]:
        symbol = sym_cfg["name"]

        if not market_open(sym_cfg.get("session", "24/7"), now):
            return "market_closed", ""

        held = [p for p in positions if p["symbol"] == symbol]

        # signals are computed every cycle (needed for stop-and-reverse, not
        # just for new entries)
        chosen, (state, detail) = self._pick_signal(symbol)

        # ── already in a trade ──
        if held:
            held_dir = held[0]["type"]
            reverse_on = self.cfg["exits"].get("close_on_opposite_signal", True)
            if chosen is not None and reverse_on and chosen.direction != held_dir:
                # opposite signal => close the existing position(s) now
                closed = 0
                for p in held:
                    if p["type"] != chosen.direction and self.broker.close_position(
                            p, self.cfg["engine"]["slippage_points"],
                            f"reverse->{chosen.direction}"):
                        self.store.record_event(
                            "CLOSE", symbol, p["type"], p["volume"],
                            p["price_current"], profit=p["profit"], ticket=p["ticket"],
                            detail=f"opposite {chosen.direction} signal (score {chosen.score})")
                        closed += 1
                return "reversed", f"closed {held_dir} on {chosen.direction} signal"
            if len(held) >= self.cfg["risk"]["max_trades_per_symbol"]:
                pnl = sum(p["profit"] for p in held)
                return "holding", f"{len(held)} pos, pnl={pnl:.2f}"

        # ── flat: apply entry gates, then open if there is a signal ──
        spread = self.broker.spread_points(symbol)
        if spread > sym_cfg.get("max_spread", 1e9):
            return "spread_wide", f"{spread:.0f}>{sym_cfg['max_spread']}"

        blocked, why = self.news.is_blocked(symbol, now)
        if blocked:
            self.store.set_status(f"news_block_{symbol}", why)
            return "news_blocked", why

        if chosen is None:
            return state, detail

        self._open_trade(sym_cfg, chosen, acc)
        return "TRADE", f"{chosen.direction} score={chosen.score}"

    def _pick_signal(self, symbol):
        """Evaluate all entry timeframes and return (chosen Signal | None, status).

        Stores the per-timeframe snapshot for the dashboard. A signal is only
        'chosen' when >= `need` entry timeframes agree on direction.
        """
        params = self.cfg["strategy"]
        trend_df = self.broker.get_rates(
            symbol, self.cfg["timeframes"]["trend"],
            self.cfg["engine"]["bars_to_fetch"])

        actionable = []
        latest_snapshot = {}
        for tf in self.cfg["timeframes"]["entry"]:
            df = self.broker.get_rates(symbol, tf, self.cfg["engine"]["bars_to_fetch"])
            if df is None or len(df) < 30:
                latest_snapshot[tf] = {"direction": "NODATA", "score": 0,
                                       "reasons": ["no bars from broker"], "snapshot": {}}
                continue
            sig = strategy.generate_signal(df, trend_df, params)
            latest_snapshot[tf] = {
                "direction": sig.direction, "score": sig.score,
                "reasons": sig.reasons, "snapshot": sig.snapshot,
            }
            if sig.actionable:
                actionable.append((tf, sig))

        self.store.set_status(f"signals_{symbol}", latest_snapshot)

        if all(v["direction"] == "NODATA" for v in latest_snapshot.values()):
            return None, ("no_data", "broker returned no bars")

        if not actionable:
            best = max((v["score"] for v in latest_snapshot.values()
                        if v["direction"] != "NODATA"), default=0)
            return None, ("no_signal",
                          f"best score {best}/6, need {params['min_confluence_score']}")

        buys = [s for tf, s in actionable if s.direction == "BUY"]
        sells = [s for tf, s in actionable if s.direction == "SELL"]
        n_entry = len(self.cfg["timeframes"]["entry"])
        need = 2 if n_entry >= 2 else 1

        if len(buys) >= need and len(buys) >= len(sells):
            return max(buys, key=lambda s: s.score), ("ok", "")
        if len(sells) >= need and len(sells) > len(buys):
            return max(sells, key=lambda s: s.score), ("ok", "")
        return None, ("no_agreement", f"buys={len(buys)} sells={len(sells)} need={need}")

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

        # Money-based TP/SL: place them at the exact prices that yield the
        # configured profit/loss for this lot size, using the broker's own
        # profit calculator (handles contract size + currency conversion).
        money_per_price = self.broker.money_per_price_unit(symbol, lot, sig.direction)
        target = self.cfg["exits"].get("profit_target_money", 0)
        sl_money = self.cfg["exits"].get("stop_loss_money", 0)
        if money_per_price > 0:
            if target and target > 0:
                tp_dist = target / money_per_price
                tp = entry + tp_dist if sig.direction == "BUY" else entry - tp_dist
            if sl_money and sl_money > 0:
                sl_dist = sl_money / money_per_price
                sl = entry - sl_dist if sig.direction == "BUY" else entry + sl_dist
        elif target or sl_money:
            log.warning("%s: couldn't compute money/price — using ATR SL/TP", symbol)

        # respect the broker's minimum stop distance (else 'invalid stops')
        min_dist = self.broker.stops_level_price(symbol)
        if min_dist > 0:
            if sig.direction == "BUY":
                sl, tp = min(sl, entry - min_dist), max(tp, entry + min_dist)
            else:
                sl, tp = max(sl, entry + min_dist), min(tp, entry - min_dist)

        sl = round(sl, spec.digits)
        tp = round(tp, spec.digits)
        log.info("%s %s sizing lot=%.2f entry=%.3f SL=%.3f TP=%.3f "
                 "(money/price=%.1f, min_stop=%.3f)",
                 symbol, sig.direction, lot, entry, sl, tp, money_per_price, min_dist)

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
