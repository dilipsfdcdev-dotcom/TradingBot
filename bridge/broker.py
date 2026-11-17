"""Thin, thread-safe wrapper around the MetaTrader5 terminal API."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger("bridge.broker")


class BrokerError(Exception):
    pass


@dataclass
class Position:
    ticket: int
    symbol: str
    side: str  # "buy" | "sell"
    volume: float
    open_price: float
    sl: float
    tp: float
    magic: int = 0
    profit: float = 0.0
    price_current: float = 0.0
    opened: int = 0


@dataclass
class SymbolSpec:
    name: str
    digits: int
    point: float
    stops_level: float  # minimum SL distance from price, in price units
    volume_min: float
    volume_step: float
    volume_max: float


@dataclass
class TradeResult:
    ok: bool
    retcode: int = 0
    comment: str = ""
    ticket: int = 0
    price: float = 0.0


class MT5Broker:
    """All MT5 calls go through one lock: the MetaTrader5 module is not thread-safe."""

    def __init__(self, path="", login=0, password="", server="", magic=770011, deviation=200, dry_run=False,
                 portable=False):
        import MetaTrader5 as mt5

        self.mt5 = mt5
        self.path, self.login, self.password, self.server = path, login, password, server
        self.magic, self.deviation, self.dry_run = magic, deviation, dry_run
        self.portable = portable
        self._lock = threading.RLock()
        self._specs: dict[str, SymbolSpec] = {}
        self._fillings: dict[str, int] = {}
        self._retry = {mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED, mt5.TRADE_RETCODE_PRICE_OFF}
        self._done = {mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED}

    # ---- connection -------------------------------------------------------
    def connect(self) -> None:
        with self._lock:
            kwargs = {}
            if self.path:
                kwargs["path"] = self.path
            if self.portable:
                kwargs["portable"] = True
            if self.login:
                kwargs.update(login=self.login, password=self.password, server=self.server)
            if not self.mt5.initialize(**kwargs):
                raise BrokerError(f"MT5 initialize failed: {self.mt5.last_error()}")
            acc = self.mt5.account_info()
            if acc is None:
                raise BrokerError(f"MT5 account not available: {self.mt5.last_error()}")
            log.info("Connected to MT5: account %s on %s (%s)", acc.login, acc.server,
                     "DEMO" if acc.trade_mode == 0 else "REAL")

    def _ensure(self) -> None:
        info = self.mt5.terminal_info()
        if info is None:
            log.warning("MT5 link lost, reconnecting")
            self.connect()
            info = self.mt5.terminal_info()
        if info is None or not info.connected:
            raise BrokerError("MT5 terminal is not connected to the broker")
        if not info.trade_allowed and not self.dry_run:
            raise BrokerError("Algo Trading is disabled in the MT5 terminal")

    def is_connected(self) -> bool:
        with self._lock:
            try:
                self._ensure()
                return True
            except BrokerError:
                return False

    def shutdown(self) -> None:
        with self._lock:
            self.mt5.shutdown()

    # ---- market data ------------------------------------------------------
    def symbol_spec(self, symbol: str) -> SymbolSpec:
        with self._lock:
            if symbol in self._specs:
                return self._specs[symbol]
            self._ensure()
            info = self.mt5.symbol_info(symbol)
            if info is None:
                raise BrokerError(f"Unknown symbol {symbol!r}")
            if not info.visible and not self.mt5.symbol_select(symbol, True):
                raise BrokerError(f"Cannot enable symbol {symbol!r} in Market Watch")
            m = self.mt5
            if info.filling_mode & 1:
                self._fillings[symbol] = m.ORDER_FILLING_FOK
            elif info.filling_mode & 2:
                self._fillings[symbol] = m.ORDER_FILLING_IOC
            else:
                self._fillings[symbol] = m.ORDER_FILLING_RETURN
            spec = SymbolSpec(symbol, info.digits, info.point, info.trade_stops_level * info.point,
                              info.volume_min, info.volume_step, info.volume_max)
            self._specs[symbol] = spec
            return spec

    def tick(self, symbol: str) -> tuple[float, float]:
        with self._lock:
            self._ensure()
            self.symbol_spec(symbol)  # makes sure the symbol is in Market Watch
            for _ in range(20):  # a freshly added symbol needs a moment for its first quote
                t = self.mt5.symbol_info_tick(symbol)
                if t is not None and t.bid > 0 and t.ask > 0:
                    return t.bid, t.ask
                time.sleep(0.1)
            raise BrokerError(f"No price for {symbol} (market closed or symbol not quoted)")

    def balance(self) -> float:
        with self._lock:
            self._ensure()
            return self.mt5.account_info().balance

    def equity(self) -> float:
        with self._lock:
            self._ensure()
            return self.mt5.account_info().equity

    def day_stats(self) -> tuple[float, int]:
        """(realized P/L of the whole account today, entries opened today by this bridge). Day = UTC."""
        with self._lock:
            self._ensure()
            now = datetime.now(timezone.utc)
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            deals = self.mt5.history_deals_get(start, now + timedelta(days=1))
            if deals is None:
                raise BrokerError(f"history_deals_get failed: {self.mt5.last_error()}")
            m = self.mt5
            trades = [d for d in deals if d.type in (m.DEAL_TYPE_BUY, m.DEAL_TYPE_SELL)]
            realized = sum(d.profit + d.commission + d.swap + d.fee for d in trades)
            entries = sum(1 for d in trades if d.magic == self.magic and d.entry == m.DEAL_ENTRY_IN)
            return realized, entries

    def account_summary(self) -> dict:
        with self._lock:
            self._ensure()
            a = self.mt5.account_info()
            return {"login": a.login, "server": a.server, "currency": a.currency, "balance": a.balance,
                    "equity": a.equity, "margin_free": a.margin_free, "floating": a.profit,
                    "demo": a.trade_mode == 0}

    def closed_trades(self, days: int = 30) -> list[dict]:
        """Fully closed positions that this bridge opened, oldest first."""
        with self._lock:
            self._ensure()
            m = self.mt5
            now = datetime.now(timezone.utc)
            deals = m.history_deals_get(now - timedelta(days=days), now + timedelta(days=1))
            if deals is None:
                raise BrokerError(f"history_deals_get failed: {m.last_error()}")
            by_position: dict[int, list] = {}
            for d in deals:
                if d.type in (m.DEAL_TYPE_BUY, m.DEAL_TYPE_SELL):
                    by_position.setdefault(d.position_id, []).append(d)
            reasons = {m.DEAL_REASON_SL: "stop-loss", m.DEAL_REASON_TP: "take-profit", m.DEAL_REASON_SO: "stop-out",
                       m.DEAL_REASON_EXPERT: "bridge"}
            trades = []
            for pid, ds in by_position.items():
                ins = [d for d in ds if d.entry == m.DEAL_ENTRY_IN]
                outs = [d for d in ds if d.entry != m.DEAL_ENTRY_IN]
                if not ins or not outs or ins[0].magic != self.magic:
                    continue
                volume = sum(d.volume for d in ins)
                if sum(d.volume for d in outs) + 1e-9 < volume:
                    continue  # still (partly) open
                last = max(outs, key=lambda d: d.time_msc)
                trades.append({
                    "position": pid, "symbol": ins[0].symbol,
                    "side": "buy" if ins[0].type == m.DEAL_TYPE_BUY else "sell",
                    "volume": round(volume, 2), "open_time": ins[0].time, "close_time": last.time,
                    "open_price": ins[0].price, "close_price": last.price,
                    # MT5/Exness list "profit" before costs; the net figure is what reaches the balance.
                    "gross": round(sum(d.profit for d in ds), 2),
                    "costs": round(sum(d.commission + d.swap + d.fee for d in ds), 2),
                    "profit": round(sum(d.profit + d.commission + d.swap + d.fee for d in ds), 2),
                    "reason": reasons.get(last.reason, "manual"),
                })
            trades.sort(key=lambda t: t["close_time"])
            return trades

    def loss_per_lot(self, symbol: str, side: str, distance: float) -> float:
        """Loss in account currency for 1.0 lot if price moves `distance` against us."""
        with self._lock:
            bid, ask = self.tick(symbol)
            buy = side == "buy"
            price = ask if buy else bid
            otype = self.mt5.ORDER_TYPE_BUY if buy else self.mt5.ORDER_TYPE_SELL
            pnl = self.mt5.order_calc_profit(otype, symbol, 1.0, price, price - distance if buy else price + distance)
            if pnl is None:
                raise BrokerError(f"order_calc_profit failed: {self.mt5.last_error()}")
            return abs(pnl)

    # ---- positions --------------------------------------------------------
    def positions(self, symbol: str | None = None) -> list[Position]:
        """Open positions opened by this bridge (matched by magic number)."""
        with self._lock:
            self._ensure()
            raw = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
            if raw is None:
                raise BrokerError(f"positions_get failed: {self.mt5.last_error()}")
            return [
                Position(p.ticket, p.symbol, "buy" if p.type == self.mt5.POSITION_TYPE_BUY else "sell",
                         p.volume, p.price_open, p.sl, p.tp, p.magic, p.profit + p.swap, p.price_current, p.time)
                for p in raw if p.magic == self.magic
            ]

    # ---- trading ----------------------------------------------------------
    def _send(self, request: dict) -> TradeResult:
        if self.dry_run:
            res = self.mt5.order_check(request)
            if res is None:
                return TradeResult(False, -1, f"order_check failed: {self.mt5.last_error()}")
            log.info("DRY RUN (not sent): %s -> retcode=%s %s", request, res.retcode, res.comment)
            return TradeResult(res.retcode == 0, res.retcode, f"dry-run: {res.comment}", 0, request.get("price", 0.0))
        res = self.mt5.order_send(request)
        if res is None:
            return TradeResult(False, -1, f"order_send failed: {self.mt5.last_error()}")
        return TradeResult(res.retcode in self._done, res.retcode, res.comment, res.order, res.price)

    def _market(self, symbol: str, side: str, volume: float, build) -> TradeResult:
        """Send a market order, re-pricing and retrying on requotes."""
        spec = self.symbol_spec(symbol)
        res = TradeResult(False, -1, "not sent")
        for _ in range(3):
            bid, ask = self.tick(symbol)
            buy = side == "buy"
            price = ask if buy else bid
            request = {
                "action": self.mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": self.mt5.ORDER_TYPE_BUY if buy else self.mt5.ORDER_TYPE_SELL,
                "price": price,
                "deviation": self.deviation,
                "magic": self.magic,
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": self._fillings[symbol],
            }
            request.update(build(price, spec))
            res = self._send(request)
            if res.ok or res.retcode not in self._retry:
                break
        return res

    def open(self, symbol: str, side: str, volume: float, sl_distance: float, tp_distance: float,
             comment: str = "tv-bridge") -> TradeResult:
        sign = 1 if side == "buy" else -1

        def build(price: float, spec: SymbolSpec) -> dict:
            return {
                "sl": round(price - sign * sl_distance, spec.digits) if sl_distance > 0 else 0.0,
                "tp": round(price + sign * tp_distance, spec.digits) if tp_distance > 0 else 0.0,
                "comment": comment,
            }

        with self._lock:
            return self._market(symbol, side, volume, build)

    def close(self, pos: Position) -> TradeResult:
        with self._lock:
            closing_side = "sell" if pos.side == "buy" else "buy"
            return self._market(pos.symbol, closing_side, pos.volume,
                                lambda price, spec: {"position": pos.ticket, "comment": "tv-bridge close"})

    def modify_sl(self, pos: Position, sl: float) -> TradeResult:
        with self._lock:
            self._ensure()
            return self._send({
                "action": self.mt5.TRADE_ACTION_SLTP,
                "symbol": pos.symbol,
                "position": pos.ticket,
                "sl": sl,
                "tp": pos.tp,
                "magic": self.magic,
            })
