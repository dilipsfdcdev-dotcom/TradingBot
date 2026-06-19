"""
Open-position manager — the "don't give back profit" engine.

For every position the bot owns it runs three escalating protections, all
measured in R (R = the initial risk distance, i.e. entry-to-stop):

  1. BREAK-EVEN   once the trade reaches `breakeven_at_r`, the stop is pulled
                  to entry (+ a small offset), so the trade can no longer lose.
  2. PARTIAL TP   at `partial_close_at_r`, close part of the position to bank
                  real cash, and let the remainder ride.
  3. TRAIL        beyond `trail_start_r`, the stop chases the best price seen
                  (peak for longs / trough for shorts) at `trail_atr_mult` ATRs
                  behind. A sudden reversal then exits in profit instead of
                  hitting the original stop.

Stops are only ever moved in the favourable direction — never loosened.
"""
from __future__ import annotations

from dataclasses import dataclass

from .logger import get_logger

log = get_logger("trademgr")


@dataclass
class _TradeState:
    entry: float
    init_sl: float
    risk: float          # |entry - init_sl|  (== 1R in price units)
    direction: str
    atr_est: float       # derived ATR (risk / sl_atr_mult)
    best: float          # best price seen since open (peak/trough)
    be_done: bool = False
    partial_done: bool = False


class TradeManager:
    def __init__(self, broker, exits_cfg: dict, engine_cfg: dict):
        self.broker = broker
        self.cfg = exits_cfg
        self.eng = engine_cfg
        self.states: dict[int, _TradeState] = {}

    def _register(self, pos: dict) -> _TradeState:
        entry = pos["price_open"]
        sl = pos["sl"] or entry
        risk = abs(entry - sl) or (entry * 0.001)  # guard against missing SL
        atr_est = risk / max(self.cfg["sl_atr_mult"], 1e-9)
        st = _TradeState(
            entry=entry, init_sl=sl, risk=risk, direction=pos["type"],
            atr_est=atr_est, best=pos["price_current"],
        )
        self.states[pos["ticket"]] = st
        return st

    def _r_multiple(self, st: _TradeState, price: float) -> float:
        if st.direction == "BUY":
            return (price - st.entry) / st.risk
        return (st.entry - price) / st.risk

    def manage(self, positions: list[dict]) -> None:
        live_tickets = {p["ticket"] for p in positions}
        # forget closed trades
        for t in list(self.states):
            if t not in live_tickets:
                self.states.pop(t, None)

        for pos in positions:
            st = self.states.get(pos["ticket"]) or self._register(pos)
            price = pos["price_current"]

            # track best excursion
            if st.direction == "BUY":
                st.best = max(st.best, price)
            else:
                st.best = min(st.best, price)

            r = self._r_multiple(st, price)

            # 2) partial take-profit
            if not st.partial_done and r >= self.cfg["partial_close_at_r"]:
                vol = self._round_vol(pos["volume"] * self.cfg["partial_close_pct"])
                if 0 < vol < pos["volume"]:
                    if self.broker.close_partial(pos, vol, self.eng["slippage_points"]):
                        st.partial_done = True

            new_sl = pos["sl"]

            # 1) break-even
            if not st.be_done and r >= self.cfg["breakeven_at_r"]:
                offset = self.cfg["breakeven_offset_r"] * st.risk
                be = st.entry + offset if st.direction == "BUY" else st.entry - offset
                new_sl = self._tighten(st.direction, new_sl, be)
                st.be_done = True

            # 3) trailing stop
            if r >= self.cfg["trail_start_r"]:
                trail_dist = self.cfg["trail_atr_mult"] * st.atr_est
                trail = (st.best - trail_dist if st.direction == "BUY"
                         else st.best + trail_dist)
                new_sl = self._tighten(st.direction, new_sl, trail)

            if new_sl != pos["sl"] and new_sl > 0:
                if self.broker.modify_sltp(pos["ticket"], pos["symbol"],
                                           new_sl, pos["tp"]):
                    log.info("Trail/BE %s %s: SL %.5f -> %.5f (R=%.2f)",
                             pos["symbol"], pos["type"], pos["sl"], new_sl, r)

    @staticmethod
    def _tighten(direction: str, current_sl: float, candidate: float) -> float:
        """Return candidate only if it is a TIGHTER (safer) stop than current."""
        if current_sl in (0, None):
            return candidate
        if direction == "BUY":
            return max(current_sl, candidate)
        return min(current_sl, candidate)

    def _round_vol(self, vol: float) -> float:
        step = self.eng.get("lot_step", 0.01) if isinstance(self.eng, dict) else 0.01
        return round(round(vol / 0.01) * 0.01, 2)
