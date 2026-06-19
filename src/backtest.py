"""
Backtester — replays the live strategy over historical bars.

It reuses the SAME `strategy.generate_signal` and the SAME profit-protection
rules (break-even, ATR trailing, partial take-profit) as the live engine, so
the backtest reflects how the bot would actually behave — not a different,
prettier model.

Accounting is done in R-multiples and compounded as a fraction of equity:
  a full position moving 1R  == risk_per_trade of equity.
  closing fraction `w` at `m` R  ==  equity * risk_per_trade * w * m.

Entries are taken at the close of the signal bar (no look-ahead); exits and
stop management are evaluated bar-by-bar against each bar's high/low.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from . import indicators as ind
from . import strategy

_TF_MIN = {"M1": 1, "M3": 3, "M5": 5, "M15": 15, "M30": 30,
           "H1": 60, "H4": 240, "D1": 1440}
_TF_RULE = {"M1": "1min", "M3": "3min", "M5": "5min", "M15": "15min",
            "M30": "30min", "H1": "60min", "H4": "240min", "D1": "1D"}


@dataclass
class Trade:
    time_in: pd.Timestamp
    direction: str
    entry: float
    init_sl: float
    risk: float
    time_out: pd.Timestamp = None
    realized_r: float = 0.0      # fraction-weighted R captured
    exit_reason: str = ""


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = None

    def stats(self, start_equity: float = 10_000.0) -> dict:
        n = len(self.trades)
        if n == 0:
            return {"trades": 0}
        rs = np.array([t.realized_r for t in self.trades])
        wins = rs[rs > 0]
        losses = rs[rs < 0]
        gross_win = wins.sum()
        gross_loss = -losses.sum()
        return {
            "trades": n,
            "win_rate": round(100 * len(wins) / n, 1),
            "avg_R": round(float(rs.mean()), 3),
            "total_R": round(float(rs.sum()), 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
            "best_R": round(float(rs.max()), 2),
            "worst_R": round(float(rs.min()), 2),
        }


def resample_trend(df: pd.DataFrame, trend_tf: str) -> pd.DataFrame:
    rule = _TF_RULE[trend_tf]
    out = df.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min",
         "close": "last", "volume": "sum"}).dropna()
    return out


def _trend_bias_series(entry_df: pd.DataFrame, trend_tf: str, ema_trend: int) -> pd.Series:
    """Look-ahead-free HTF bias aligned to the entry index."""
    trend = resample_trend(entry_df, trend_tf)
    if len(trend) < ema_trend + 2:
        return pd.Series(0, index=entry_df.index)
    ema_t = ind.ema(trend["close"], ema_trend)
    bias = np.sign(trend["close"] - ema_t)          # +1/0/-1 per trend bar
    bias = bias.shift(1)                            # only use COMPLETED bars
    aligned = bias.reindex(entry_df.index, method="ffill").fillna(0)
    return aligned.astype(int)


class Backtester:
    def __init__(self, cfg):
        self.cfg = cfg
        self.p = cfg["strategy"]
        self.exits = cfg["exits"]

    def run(self, df: pd.DataFrame, symbol: str, timeframe: str,
            risk_per_trade: float, start_equity: float = 10_000.0) -> BacktestResult:
        df = df.copy()
        bias = _trend_bias_series(df, self.cfg["timeframes"]["trend"], self.p["ema_trend"])

        # compute ALL indicators once (vectorised) instead of per-bar — O(n) not O(n^2)
        feat = strategy.compute_features(df, self.p)
        cols = {c: feat[c].values for c in feat.columns}
        bias_arr = bias.values

        warmup = max(self.p["ema_slow"], self.p["adx_period"], self.exits["atr_period"]) + 5
        result = BacktestResult(symbol=symbol, timeframe=timeframe)

        equity = start_equity
        eq_times, eq_vals = [], []
        pos = None  # active simulated trade state

        index = df.index
        highs, lows, closes = df["high"].values, df["low"].values, df["close"].values

        for i in range(warmup, len(df)):
            bar_time = index[i]
            hi, lo = highs[i], lows[i]

            # ── manage open position against this bar ──
            if pos is not None:
                equity, pos, closed = self._manage(pos, hi, lo, equity, risk_per_trade, bar_time)
                if closed is not None:
                    result.trades.append(closed)

            # ── look for a new entry when flat ──
            if pos is None:
                row = {c: cols[c][i] for c in cols}
                sig = strategy.evaluate(row, int(bias_arr[i]), self.p)
                if sig.actionable and sig.atr > 0:
                    entry = float(closes[i])
                    sl, _ = self._levels(entry, sig.atr, sig.direction)
                    pos = {
                        "trade": Trade(time_in=bar_time, direction=sig.direction,
                                       entry=entry, init_sl=sl,
                                       risk=abs(entry - sl)),
                        "sl": sl, "best": entry, "remaining": 1.0,
                        "atr": sig.atr, "be_done": False, "partial_done": False,
                        "realized": 0.0,
                    }

            eq_times.append(bar_time)
            eq_vals.append(equity)

        # force-close any trade still open at the end (at last close)
        if pos is not None:
            last = float(df["close"].iloc[-1])
            r = self._r(pos, last)
            pos["realized"] += pos["remaining"] * r
            equity *= (1 + risk_per_trade * pos["remaining"] * r)
            tr = pos["trade"]
            tr.time_out = df.index[-1]
            tr.realized_r = pos["realized"]
            tr.exit_reason = "end"
            result.trades.append(tr)
            eq_vals[-1] = equity

        result.equity_curve = pd.Series(eq_vals, index=eq_times)
        return result

    # ── helpers ─────────────────────────────────────────────────────────
    def _levels(self, entry, atr, direction):
        sl_d = self.exits["sl_atr_mult"] * atr
        tp_d = self.exits["tp_atr_mult"] * atr
        if direction == "BUY":
            return entry - sl_d, entry + tp_d
        return entry + sl_d, entry - tp_d

    def _r(self, pos, price):
        tr = pos["trade"]
        if tr.direction == "BUY":
            return (price - tr.entry) / tr.risk
        return (tr.entry - price) / tr.risk

    def _manage(self, pos, hi, lo, equity, f, bar_time):
        """Process one bar for an open position. Returns (equity, pos|None, closed_trade|None)."""
        tr = pos["trade"]
        d = tr.direction
        tp_price = (tr.entry + self.exits["tp_atr_mult"] * pos["atr"] if d == "BUY"
                    else tr.entry - self.exits["tp_atr_mult"] * pos["atr"])

        # update best excursion (use bar extreme in trade direction)
        pos["best"] = max(pos["best"], hi) if d == "BUY" else min(pos["best"], lo)

        # 1) STOP hit? (checked first = conservative)
        stop_hit = (lo <= pos["sl"]) if d == "BUY" else (hi >= pos["sl"])
        if stop_hit:
            r = self._r(pos, pos["sl"])
            pos["realized"] += pos["remaining"] * r
            equity *= (1 + f * pos["remaining"] * r)
            tr.time_out, tr.realized_r, tr.exit_reason = bar_time, pos["realized"], "stop"
            return equity, None, tr

        # 2) partial take-profit
        if not pos["partial_done"]:
            target_r = self.exits["partial_close_at_r"]
            reached = (hi >= tr.entry + target_r * tr.risk) if d == "BUY" \
                else (lo <= tr.entry - target_r * tr.risk)
            if reached:
                w = self.exits["partial_close_pct"]
                pos["realized"] += w * target_r
                equity *= (1 + f * w * target_r)
                pos["remaining"] -= w
                pos["partial_done"] = True

        # 3) full TP hit?
        tp_hit = (hi >= tp_price) if d == "BUY" else (lo <= tp_price)
        if tp_hit:
            r = self.exits["tp_atr_mult"] / self.exits["sl_atr_mult"]
            pos["realized"] += pos["remaining"] * r
            equity *= (1 + f * pos["remaining"] * r)
            tr.time_out, tr.realized_r, tr.exit_reason = bar_time, pos["realized"], "tp"
            return equity, None, tr

        # 4) break-even + trailing (move stop only tighter)
        r_now = self._r(pos, pos["best"])
        if not pos["be_done"] and r_now >= self.exits["breakeven_at_r"]:
            off = self.exits["breakeven_offset_r"] * tr.risk
            be = tr.entry + off if d == "BUY" else tr.entry - off
            pos["sl"] = max(pos["sl"], be) if d == "BUY" else min(pos["sl"], be)
            pos["be_done"] = True
        if r_now >= self.exits["trail_start_r"]:
            td = self.exits["trail_atr_mult"] * pos["atr"]
            trail = pos["best"] - td if d == "BUY" else pos["best"] + td
            pos["sl"] = max(pos["sl"], trail) if d == "BUY" else min(pos["sl"], trail)

        return equity, pos, None


def windows_report(df: pd.DataFrame, bt: Backtester, symbol: str, timeframe: str,
                   risk_per_trade: float, days_list=(10, 20, 30)) -> dict:
    """Run the backtest over the last N days for several N and return stats."""
    out = {}
    end = df.index.max()
    for days in days_list:
        sub = df[df.index >= end - timedelta(days=days)]
        if len(sub) < 50:
            out[days] = {"trades": 0, "note": "not enough data"}
            continue
        res = bt.run(sub, symbol, timeframe, risk_per_trade)
        s = res.stats()
        if res.equity_curve is not None and len(res.equity_curve):
            curve = res.equity_curve
            ret = (curve.iloc[-1] / curve.iloc[0] - 1) * 100
            peak = curve.cummax()
            max_dd = ((curve - peak) / peak).min() * 100
            s["return_pct"] = round(float(ret), 2)
            s["max_drawdown_pct"] = round(float(max_dd), 2)
        out[days] = s
    return out
