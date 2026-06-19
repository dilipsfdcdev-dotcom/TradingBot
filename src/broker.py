"""
MetaTrader 5 broker connector.

Wraps every interaction with the MT5 terminal: connection, market data,
symbol specs, order execution, SL/TP modification and position closing.

NOTE: the `MetaTrader5` package only exists on Windows, so it is imported
lazily inside `connect()`. That lets the rest of the codebase (strategy,
risk, backtester, dashboard) be imported and unit-tested on any OS.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from .logger import get_logger
from .risk import SymbolSpec

log = get_logger("broker")

# filled in once MT5 is imported
mt5: Any = None

# string -> MT5 timeframe constant (resolved after import)
_TF_NAMES = {
    "M1": "TIMEFRAME_M1",
    "M3": "TIMEFRAME_M3",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}


class Broker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.connected = False
        self._tf = {}

    # ── connection ──────────────────────────────────────────────────────
    def connect(self) -> bool:
        global mt5
        try:
            import MetaTrader5 as _mt5  # noqa: N813
            mt5 = _mt5
        except ImportError:
            log.critical(
                "MetaTrader5 package not installed. It only runs on Windows. "
                "Install MT5 terminal + `pip install MetaTrader5`."
            )
            return False

        kwargs = {}
        if self.cfg.mt5_path:
            kwargs["path"] = self.cfg.mt5_path
        if self.cfg.mt5_login:
            kwargs.update(
                login=self.cfg.mt5_login,
                password=self.cfg.mt5_password,
                server=self.cfg.mt5_server,
            )

        if not mt5.initialize(**kwargs):
            log.critical("MT5 initialize() failed: %s", mt5.last_error())
            return False

        self._tf = {name: getattr(mt5, const) for name, const in _TF_NAMES.items()}
        info = mt5.account_info()
        if info is None:
            log.critical("Could not read account info: %s", mt5.last_error())
            return False

        self.connected = True
        log.info(
            "Connected to MT5 | account %s | %s | balance %.2f %s | %s",
            info.login, info.server, info.balance, info.currency,
            "LIVE" if self.cfg.is_live else "DEMO",
        )
        return True

    def shutdown(self) -> None:
        if mt5 and self.connected:
            mt5.shutdown()
            self.connected = False

    def ensure_symbol(self, symbol: str) -> bool:
        info = mt5.symbol_info(symbol)
        if info is None:
            log.error("Symbol %s not found at broker. Check the exact name!", symbol)
            return False
        if not info.visible and not mt5.symbol_select(symbol, True):
            log.error("Failed to add %s to Market Watch", symbol)
            return False
        return True

    # ── account / data ──────────────────────────────────────────────────
    def account(self) -> dict:
        a = mt5.account_info()
        return {
            "balance": a.balance, "equity": a.equity, "margin": a.margin,
            "free_margin": a.margin_free, "profit": a.profit,
            "currency": a.currency, "login": a.login, "server": a.server,
        }

    def get_rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame | None:
        tf = self._tf.get(timeframe)
        if tf is None:
            log.error("Unknown timeframe %s", timeframe)
            return None
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.rename(columns={"tick_volume": "volume"})
        return df[["time", "open", "high", "low", "close", "volume"]].set_index("time")

    def get_history(self, symbol: str, timeframe: str, days: int) -> pd.DataFrame | None:
        """Fetch roughly `days` worth of bars for backtesting."""
        tf_min = {"M1": 1, "M3": 3, "M5": 5, "M15": 15, "M30": 30,
                  "H1": 60, "H4": 240, "D1": 1440}.get(timeframe, 5)
        count = int(days * (1440 / tf_min) * 1.3) + 100
        count = min(count, 200_000)  # MT5 practical cap
        return self.get_rates(symbol, timeframe, count)

    def symbol_spec(self, symbol: str) -> SymbolSpec | None:
        s = mt5.symbol_info(symbol)
        if s is None:
            return None
        return SymbolSpec(
            tick_size=s.trade_tick_size or s.point,
            tick_value=s.trade_tick_value,
            point=s.point,
            digits=s.digits,
            volume_min=s.volume_min,
            volume_max=s.volume_max,
            volume_step=s.volume_step,
        )

    def spread_points(self, symbol: str) -> float:
        t = mt5.symbol_info_tick(symbol)
        s = mt5.symbol_info(symbol)
        if not t or not s or s.point == 0:
            return 1e9
        return (t.ask - t.bid) / s.point

    def price(self, symbol: str) -> tuple[float, float]:
        t = mt5.symbol_info_tick(symbol)
        return (t.ask, t.bid) if t else (0.0, 0.0)

    # ── positions ─────────────────────────────────────────────────────────
    def positions(self, magic: int | None = None) -> list[dict]:
        pos = mt5.positions_get()
        if pos is None:
            return []
        out = []
        for p in pos:
            if magic is not None and p.magic != magic:
                continue
            out.append({
                "ticket": p.ticket, "symbol": p.symbol,
                "type": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "volume": p.volume, "price_open": p.price_open,
                "sl": p.sl, "tp": p.tp, "price_current": p.price_current,
                "profit": p.profit, "magic": p.magic, "comment": p.comment,
                "time": p.time,
            })
        return out

    # ── orders ────────────────────────────────────────────────────────────
    def open_trade(self, symbol, direction, lot, sl, tp, magic, comment, slippage):
        ask, bid = self.price(symbol)
        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        price = ask if direction == "BUY" else bid
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(lot),
            "type": order_type, "price": price, "sl": float(sl), "tp": float(tp),
            "deviation": int(slippage), "magic": int(magic), "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(symbol),
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("OPEN %s %s failed: %s", direction, symbol,
                      res.comment if res else mt5.last_error())
            return None
        log.info("OPENED %s %s %.2f lots @ %.5f sl=%.5f tp=%.5f",
                 direction, symbol, lot, price, sl, tp)
        return res.order

    def modify_sltp(self, ticket, symbol, sl, tp) -> bool:
        req = {"action": mt5.TRADE_ACTION_SLTP, "symbol": symbol,
               "position": ticket, "sl": float(sl), "tp": float(tp)}
        res = mt5.order_send(req)
        ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
        if not ok:
            log.warning("modify SL/TP on %s failed: %s", ticket,
                        res.comment if res else mt5.last_error())
        return ok

    def close_partial(self, pos: dict, volume: float, slippage: int) -> bool:
        symbol = pos["symbol"]
        ask, bid = self.price(symbol)
        if pos["type"] == "BUY":
            order_type, price = mt5.ORDER_TYPE_SELL, bid
        else:
            order_type, price = mt5.ORDER_TYPE_BUY, ask
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
            "volume": float(volume), "type": order_type, "position": pos["ticket"],
            "price": price, "deviation": int(slippage),
            "magic": pos["magic"], "comment": "partial-tp",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(symbol),
        }
        res = mt5.order_send(req)
        ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
        if ok:
            log.info("PARTIAL closed %.2f lots of %s", volume, symbol)
        return ok

    def _filling(self, symbol):
        """Pick a filling mode the symbol actually supports."""
        info = mt5.symbol_info(symbol)
        mode = getattr(info, "filling_mode", 0)
        if mode & 1:   # FOK
            return mt5.ORDER_FILLING_FOK
        if mode & 2:   # IOC
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN
