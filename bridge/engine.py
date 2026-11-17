"""Trading rules: turn signals into orders, and trail stop-losses."""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone

from .broker import BrokerError, Position, SymbolSpec
from .config import Settings

log = logging.getLogger("bridge.engine")

ALIASES = {
    "buy": "buy", "long": "buy",
    "sell": "sell", "short": "sell",
    "close": "close", "exit": "close", "flat": "close", "close_all": "close",
    "close_buy": "close_buy", "close_long": "close_buy",
    "close_sell": "close_sell", "close_short": "close_sell",
}


class SignalError(Exception):
    """The signal itself is invalid (bad action, unknown symbol, bad lot)."""


def compute_trailing_sl(pos: Position, bid: float, ask: float, s: Settings, spec: SymbolSpec) -> float | None:
    """New stop-loss for `pos`, or None if it should stay where it is.

    The stop only ever moves in the profitable direction.
    """
    buy = pos.side == "buy"
    sign = 1 if buy else -1
    price = bid if buy else ask  # the price this position would close at
    profit = (price - pos.open_price) * sign

    candidates = []
    if s.breakeven_at > 0 and profit >= s.breakeven_at:
        candidates.append(pos.open_price + sign * s.breakeven_offset)
    if s.trail_distance > 0 and profit >= s.trail_start:
        candidates.append(price - sign * s.trail_distance)
    if not candidates:
        return None

    new_sl = max(candidates) if buy else min(candidates)
    limit = price - sign * (spec.stops_level + spec.point)  # broker's minimum stop distance
    new_sl = round(min(new_sl, limit) if buy else max(new_sl, limit), spec.digits)

    if pos.sl:
        improvement = (new_sl - pos.sl) * sign
        if improvement < max(s.trail_step, spec.point) - 1e-9:
            return None
    return new_sl


class Engine:
    def __init__(self, broker, settings: Settings, on_event=None, load_state=None, save_state=None):
        self.broker = broker
        self.on_event = on_event or (lambda action, symbol, result: None)
        self._save_state = save_state or (lambda value: None)
        # Per symbol: the direction of the last signal and where a re-entry would trigger.
        self.bias: dict[str, dict] = (load_state() if load_state else None) or {}
        self._bias_saved = 0.0
        self.s = settings
        self._lock = threading.Lock()  # one signal at a time
        self._last: dict[tuple[str, str], float] = {}
        self._killed_day = None      # UTC date on which the daily-loss kill switch tripped
        self._last_risk_check = 0.0

    # ---- signals ----------------------------------------------------------
    def resolve_symbol(self, raw: str | None) -> str:
        if not raw:
            return self.s.default_symbol
        key = raw.split(":")[-1].strip().upper()  # "OANDA:XAUUSD" -> "XAUUSD"
        if key not in self.s.symbol_map:
            raise SignalError(f"Symbol {raw!r} is not in SYMBOL_MAP")
        return self.s.symbol_map[key]

    def handle_signal(self, action: str, symbol: str | None = None, lot: float | None = None) -> dict:
        act = ALIASES.get(str(action).strip().lower())
        if act is None:
            raise SignalError(f"Unknown action {action!r}")
        sym = self.resolve_symbol(symbol)

        with self._lock:
            now = time.monotonic()
            last = self._last.get((act, sym))
            if last is not None and now - last < self.s.dedup_window_sec:
                return {"status": "ignored", "reason": "duplicate signal"}
            self._last[(act, sym)] = now

            positions = self.broker.positions(sym)
            if act in ("close", "close_buy", "close_sell"):
                side = None if act == "close" else act.split("_")[1]
                closed, failed = self._close([p for p in positions if side in (None, p.side)])
                if sym in self.bias and side in (None, self.bias[sym]["side"]):
                    del self.bias[sym]  # a close signal means "stay flat": no re-entry
                    self._save_bias()
                return {"status": "error" if failed else "executed", "closed": closed, "failed": failed}

            # 1) Close opposite positions first.
            closed, failed = self._close([p for p in positions if p.side != act])
            if failed:
                return {"status": "error", "reason": "could not close opposite positions; not opening",
                        "closed": closed, "failed": failed}

            # 2) Open in the signal direction, and remember that direction for re-entries.
            result = self._enter(sym, act, lot, closed)
            self._set_bias(sym, act, result)
            return result

    def _enter(self, sym: str, act: str, lot: float | None, closed: list[int]) -> dict:
        """Open one position in direction `act` if every risk check allows it. Caller holds the lock."""
        if True:
            positions = self.broker.positions(sym)
            if self.kill_switch_active():
                return {"status": "ignored", "reason": "daily loss limit reached; trading stopped for today",
                        "closed": closed}
            same = sum(1 for p in positions if p.side == act)
            if same >= self.s.max_positions_per_symbol:
                return {"status": "ignored", "closed": closed,
                        "reason": f"already {same} {act} position(s) on {sym} (MAX_POSITIONS_PER_SYMBOL)"}
            total = len(self.broker.positions())
            if total >= self.s.max_open_positions:
                return {"status": "ignored", "closed": closed,
                        "reason": f"already {total} open position(s) (MAX_OPEN_POSITIONS)"}
            if self.s.max_trades_per_day > 0 and self.broker.day_stats()[1] >= self.s.max_trades_per_day:
                return {"status": "ignored", "reason": "MAX_TRADES_PER_DAY reached", "closed": closed}

            spec = self.broker.symbol_spec(sym)
            bid, ask = self.broker.tick(sym)
            spread = ask - bid
            if self.s.max_spread > 0 and spread > self.s.max_spread:
                return {"status": "ignored", "reason": f"spread {spread:.3f} > MAX_SPREAD {self.s.max_spread}",
                        "closed": closed}

            volume = self._volume(sym, act, spec, lot)
            res = self.broker.open(sym, act, volume, self.s.sl_distance, self.s.tp_distance)
            if not res.ok:
                log.error("OPEN %s %s %.2f FAILED: %s %s", act, sym, volume, res.retcode, res.comment)
                return {"status": "error", "reason": f"open failed: {res.retcode} {res.comment}", "closed": closed}
            log.info("OPENED %s %s %.2f @ %.3f (order %s)", act, sym, volume, res.price, res.ticket)
            return {"status": "executed", "side": act, "symbol": sym, "volume": volume,
                    "price": round(res.price, 5), "order": res.ticket, "closed": closed}

    # ---- re-entry while the signal still stands ----------------------------
    def _save_bias(self) -> None:
        self._bias_saved = time.monotonic()
        try:
            self._save_state(self.bias)
        except Exception as e:
            log.warning("Could not save signal state: %s", e)

    def _set_bias(self, sym: str, side: str, result: dict) -> None:
        try:
            bid, ask = self.broker.tick(sym)
            held = any(p.side == side for p in self.broker.positions(sym))
        except BrokerError:
            return
        self.bias[sym] = {"side": side, "level": result.get("price") or (ask if side == "buy" else bid),
                          "reentries": 0, "had_position": held, "exit_time": None if held else time.time()}
        self._save_bias()

    def bias_view(self) -> list[dict]:
        out = []
        for sym, b in self.bias.items():
            sign = 1 if b["side"] == "buy" else -1
            out.append({"symbol": sym, "side": b["side"], "in_trade": b["had_position"],
                        "trigger": round(b["level"] + sign * self.s.reentry_buffer, 3),
                        "reentries": b["reentries"], "max": self.s.reentry_max})
        return out

    def check_reentries(self) -> None:
        """If a trade was stopped out but the last signal has not reversed, get back in when price
        pushes beyond the best price reached so far (a new high for a buy, a new low for a sell)."""
        if self.s.reentry_max <= 0 or not self.bias:
            return
        with self._lock:
            for sym, b in list(self.bias.items()):
                try:
                    buy = b["side"] == "buy"
                    bid, ask = self.broker.tick(sym)
                    if any(p.side == b["side"] for p in self.broker.positions(sym)):
                        best = max(b["level"], bid) if buy else min(b["level"], ask)
                        changed = not b["had_position"]
                        b.update(level=best, had_position=True, exit_time=None)
                        if changed or time.monotonic() - self._bias_saved > 5:
                            self._save_bias()
                        continue
                    if b["had_position"]:
                        b.update(had_position=False, exit_time=time.time())
                        self._save_bias()
                        log.info("Flat on %s while the last signal is still %s: re-entry armed beyond %.3f",
                                 sym, b["side"], b["level"])
                    if b["reentries"] >= self.s.reentry_max:
                        continue
                    if time.time() - (b["exit_time"] or 0) < self.s.reentry_cooldown_sec:
                        continue
                    trigger = b["level"] + (self.s.reentry_buffer if buy else -self.s.reentry_buffer)
                    if (ask < trigger) if buy else (bid > trigger):
                        continue
                    result = self._enter(sym, b["side"], None, [])
                    if result["status"] == "ignored":   # spread, limits or kill switch: wait and try again
                        b["exit_time"] = time.time()
                        continue
                    b["reentries"] += 1
                    b["exit_time"] = time.time()
                    if result["status"] == "executed":
                        b["had_position"] = True
                    result["reason"] = f"re-entry {b['reentries']} of {self.s.reentry_max}: price broke {trigger:.3f}"
                    self._save_bias()
                    self.on_event(f"re-entry {b['side']}", sym, result)
                except (BrokerError, SignalError) as e:
                    log.warning("Re-entry check on %s skipped: %s", sym, e)

    def _close(self, positions: list[Position]) -> tuple[list[int], list[int]]:
        closed, failed = [], []
        for p in positions:
            res = self.broker.close(p)
            if res.ok:
                log.info("CLOSED %s %s %.2f (ticket %s) @ %.3f", p.side, p.symbol, p.volume, p.ticket, res.price)
                closed.append(p.ticket)
            else:
                log.error("CLOSE ticket %s FAILED: %s %s", p.ticket, res.retcode, res.comment)
                failed.append(p.ticket)
        return closed, failed

    def _volume(self, symbol: str, side: str, spec: SymbolSpec, override: float | None) -> float:
        lot = override if override else self.s.lot
        if not override and self.s.risk_percent > 0 and self.s.sl_distance > 0:
            loss = self.broker.loss_per_lot(symbol, side, self.s.sl_distance)
            if loss > 0:
                lot = self.broker.equity() * self.s.risk_percent / 100.0 / loss
        lot = min(lot, self.s.max_lot, spec.volume_max)
        lot = round(math.floor(lot / spec.volume_step + 1e-9) * spec.volume_step, 8)
        if lot < spec.volume_min:
            raise SignalError(f"Lot size {lot} is below the broker minimum {spec.volume_min}")
        return lot

    # ---- daily loss kill switch -------------------------------------------
    def daily_loss_percent(self) -> float:
        """Today's loss (closed trades + open P/L) as % of the balance at the start of the UTC day."""
        realized, _ = self.broker.day_stats()
        start = self.broker.balance() - realized
        return (start - self.broker.equity()) / start * 100.0 if start > 0 else 0.0

    def kill_switch_active(self) -> bool:
        if self.s.max_daily_loss_percent <= 0:
            return False
        today = datetime.now(timezone.utc).date()
        if self._killed_day == today:
            return True
        loss = self.daily_loss_percent()
        if loss >= self.s.max_daily_loss_percent:
            self._killed_day = today
            log.critical("KILL SWITCH: daily loss %.1f%% >= %.1f%%. Closing positions; no new trades today (UTC).",
                         loss, self.s.max_daily_loss_percent)
            return True
        return False

    def enforce_risk(self) -> None:
        """Close everything this bridge holds while the kill switch is active."""
        with self._lock:
            if self.kill_switch_active():
                self._close(self.broker.positions())

    # ---- trailing stop ----------------------------------------------------
    def trail_once(self) -> int:
        """Check every open bridge position once; returns how many stops were moved."""
        moved = 0
        if time.monotonic() - self._last_risk_check >= 2.0:
            self._last_risk_check = time.monotonic()
            self.enforce_risk()
        self.check_reentries()
        for pos in self.broker.positions():
            try:
                spec = self.broker.symbol_spec(pos.symbol)
                bid, ask = self.broker.tick(pos.symbol)
                new_sl = compute_trailing_sl(pos, bid, ask, self.s, spec)
                if new_sl is None:
                    continue
                res = self.broker.modify_sl(pos, new_sl)
                if res.ok:
                    moved += 1
                    log.info("TRAIL %s %s ticket %s: SL %.3f -> %.3f", pos.side, pos.symbol, pos.ticket, pos.sl, new_sl)
                else:
                    log.warning("TRAIL ticket %s failed: %s %s", pos.ticket, res.retcode, res.comment)
            except BrokerError as e:
                log.warning("TRAIL ticket %s skipped: %s", pos.ticket, e)
        return moved
