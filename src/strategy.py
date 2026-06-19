"""
Trading strategy — "Confluence Scalper".

Philosophy (this is the expert design choice):
  Scalping 1m-5m is noisy. The edge comes from only trading when MULTIPLE
  independent signals agree AND the higher timeframe is on our side AND the
  market is actually trending (not chopping). Each cycle we score up to 6
  independent conditions; we only fire a trade when the aligned score clears
  `min_confluence_score`. This filters out the bulk of low-quality setups.

Direction is decided by the net vote; entry is gated by:
  - ADX  >= adx_min            (trend strength — skip chop)
  - ATR% >= min_atr_pct        (volatility — skip dead markets)
  - RSI not at the wrong extreme (don't buy blow-off tops / sell capitulation)
  - higher-timeframe trend agrees
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from . import indicators as ind


@dataclass
class Signal:
    direction: str = "NONE"          # "BUY" | "SELL" | "NONE"
    score: int = 0                   # confluence score achieved
    max_score: int = 6
    atr: float = 0.0
    price: float = 0.0
    reasons: list[str] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.direction in ("BUY", "SELL")


def _trend_bias(trend_df: pd.DataFrame, p: dict) -> int:
    """Higher-timeframe bias from EMA(trend): +1 up, -1 down, 0 flat."""
    if trend_df is None or len(trend_df) < p["ema_trend"] + 2:
        return 0
    ema_trend = ind.ema(trend_df["close"], p["ema_trend"])
    price = trend_df["close"].iloc[-1]
    if price > ema_trend.iloc[-1]:
        return 1
    if price < ema_trend.iloc[-1]:
        return -1
    return 0


def compute_features(entry_df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Compute every indicator ONCE over the full series (vectorised).

    The backtester computes this a single time and then reads rows cheaply,
    instead of recomputing all indicators on every bar (which was O(n^2)).
    """
    p = params
    close = entry_df["close"]
    macd = ind.macd(close, p["macd_fast"], p["macd_slow"], p["macd_signal"])
    feat = pd.DataFrame(index=entry_df.index)
    feat["price"] = close
    feat["ema_fast"] = ind.ema(close, p["ema_fast"])
    feat["ema_slow"] = ind.ema(close, p["ema_slow"])
    feat["rsi"] = ind.rsi(close, p["rsi_period"])
    feat["adx"] = ind.adx(entry_df, p["adx_period"])
    feat["macd_hist"] = macd["hist"]
    feat["macd_hist_prev"] = macd["hist"].shift(1)
    feat["bb_mid"] = ind.bollinger(close, p["bb_period"], p["bb_std"])["bb_mid"]
    feat["atr"] = ind.atr(entry_df, p.get("atr_period", 14))
    return feat


def evaluate(f: dict, htf: int, params: dict) -> Signal:
    """Score a single bar from pre-computed feature values `f` (a dict/row).

    Shared by the live engine and the backtester so the logic is identical.
    """
    p = params
    sig = Signal(max_score=6)
    price = f["price"]
    atr_val = f["atr"]

    # bail on warmup / NaN rows
    vals = [f["ema_fast"], f["ema_slow"], f["rsi"], f["adx"],
            f["macd_hist"], f["macd_hist_prev"], atr_val, price]
    if any(v is None or (isinstance(v, float) and v != v) for v in vals):
        sig.reasons.append("warmup / NaN")
        return sig

    sig.price = price
    sig.atr = atr_val
    rsi_v, adx_v = f["rsi"], f["adx"]
    macd_hist, macd_hist_prev = f["macd_hist"], f["macd_hist_prev"]

    sig.snapshot = {
        "rsi": round(rsi_v, 1), "adx": round(adx_v, 1),
        "macd_hist": round(macd_hist, 6), "atr": round(atr_val, 5),
        "htf_bias": htf, "price": round(price, 5),
    }

    # --- hard gates ---
    if adx_v < p["adx_min"]:
        sig.reasons.append(f"ADX {adx_v:.1f} < {p['adx_min']} (chop)")
        return sig
    if price > 0 and (atr_val / price) < p["min_atr_pct"]:
        sig.reasons.append("ATR too low (dead market)")
        return sig

    # --- confluence votes (+1 buy, -1 sell) ---
    votes = [
        ("ema_cross", 1 if f["ema_fast"] > f["ema_slow"] else -1),
        ("price_vs_emaslow", 1 if price > f["ema_slow"] else -1),
        ("macd", 1 if macd_hist > 0 else -1),
        ("macd_momentum", 1 if macd_hist > macd_hist_prev else -1),
        ("rsi_mid", 1 if rsi_v >= 50 else -1),
        ("htf_trend", htf if htf != 0 else (1 if price > f["ema_slow"] else -1)),
    ]
    net = sum(v for _, v in votes)
    direction = "BUY" if net > 0 else "SELL" if net < 0 else "NONE"
    if direction == "NONE":
        sig.reasons.append("no net bias")
        return sig

    want = 1 if direction == "BUY" else -1
    sig.score = sum(1 for _, v in votes if v == want)
    sig.reasons = [f"{name}:{'+' if v > 0 else ''}{v}" for name, v in votes]

    # --- vetoes ---
    if direction == "BUY" and rsi_v >= p["rsi_overbought"]:
        sig.reasons.append(f"veto: RSI {rsi_v:.0f} overbought")
        return sig
    if direction == "SELL" and rsi_v <= p["rsi_oversold"]:
        sig.reasons.append(f"veto: RSI {rsi_v:.0f} oversold")
        return sig
    if htf != 0 and htf != want:
        sig.reasons.append("veto: against HTF trend")
        return sig

    if sig.score >= p["min_confluence_score"]:
        sig.direction = direction
    else:
        sig.reasons.append(f"score {sig.score} < {p['min_confluence_score']}")
    return sig


def generate_signal(
    entry_df: pd.DataFrame,
    trend_df: pd.DataFrame | None,
    params: dict,
    htf_bias: int | None = None,
) -> Signal:
    """Generate a signal for the LATEST bar of `entry_df`.

    `htf_bias` lets the backtester inject a pre-computed, look-ahead-free
    higher-timeframe bias (+1/0/-1). When None, it is derived from `trend_df`.
    """
    p = params
    if entry_df is None or len(entry_df) < max(p["ema_slow"], p["adx_period"]) + 5:
        s = Signal(max_score=6)
        s.reasons.append("not enough bars")
        return s

    feat = compute_features(entry_df, p)
    row = feat.iloc[-1].to_dict()
    htf = htf_bias if htf_bias is not None else _trend_bias(trend_df, p)
    return evaluate(row, htf, p)
