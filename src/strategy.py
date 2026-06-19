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


def generate_signal(
    entry_df: pd.DataFrame,
    trend_df: pd.DataFrame | None,
    params: dict,
    htf_bias: int | None = None,
) -> Signal:
    """Generate a trading signal.

    `htf_bias` lets the backtester inject a pre-computed, look-ahead-free
    higher-timeframe bias (+1/0/-1). When None, it is derived from `trend_df`.
    """
    p = params
    sig = Signal(max_score=6)

    if entry_df is None or len(entry_df) < max(p["ema_slow"], p["adx_period"]) + 5:
        sig.reasons.append("not enough bars")
        return sig

    close = entry_df["close"]
    price = float(close.iloc[-1])
    sig.price = price

    # --- indicators ---
    ema_fast = ind.ema(close, p["ema_fast"])
    ema_slow = ind.ema(close, p["ema_slow"])
    rsi = ind.rsi(close, p["rsi_period"])
    adx = ind.adx(entry_df, p["adx_period"])
    macd = ind.macd(close, p["macd_fast"], p["macd_slow"], p["macd_signal"])
    bb = ind.bollinger(close, p["bb_period"], p["bb_std"])
    atr = ind.atr(entry_df, p.get("atr_period", 14))
    atr_val = float(atr.iloc[-1])
    sig.atr = atr_val

    rsi_v = float(rsi.iloc[-1])
    adx_v = float(adx.iloc[-1])
    macd_hist = float(macd["hist"].iloc[-1])
    macd_hist_prev = float(macd["hist"].iloc[-2])
    htf = htf_bias if htf_bias is not None else _trend_bias(trend_df, p)

    sig.snapshot = {
        "ema_fast": round(float(ema_fast.iloc[-1]), 5),
        "ema_slow": round(float(ema_slow.iloc[-1]), 5),
        "rsi": round(rsi_v, 1),
        "adx": round(adx_v, 1),
        "macd_hist": round(macd_hist, 6),
        "bb_mid": round(float(bb["bb_mid"].iloc[-1]), 5),
        "atr": round(atr_val, 5),
        "htf_bias": htf,
        "price": round(price, 5),
    }

    # --- hard gates -------------------------------------------------------
    if adx_v < p["adx_min"]:
        sig.reasons.append(f"ADX {adx_v:.1f} < {p['adx_min']} (chop)")
        return sig
    if price > 0 and (atr_val / price) < p["min_atr_pct"]:
        sig.reasons.append("ATR too low (dead market)")
        return sig

    # --- confluence votes (+1 buy, -1 sell) ------------------------------
    votes: list[tuple[str, int]] = []
    votes.append(("ema_cross", 1 if ema_fast.iloc[-1] > ema_slow.iloc[-1] else -1))
    votes.append(("price_vs_emaslow", 1 if price > ema_slow.iloc[-1] else -1))
    votes.append(("macd", 1 if macd_hist > 0 else -1))
    votes.append(("macd_momentum", 1 if macd_hist > macd_hist_prev else -1))
    votes.append(("rsi_mid", 1 if rsi_v >= 50 else -1))
    votes.append(("htf_trend", htf if htf != 0 else (1 if price > ema_slow.iloc[-1] else -1)))

    net = sum(v for _, v in votes)
    direction = "BUY" if net > 0 else "SELL" if net < 0 else "NONE"
    if direction == "NONE":
        sig.reasons.append("no net bias")
        return sig

    want = 1 if direction == "BUY" else -1
    score = sum(1 for _, v in votes if v == want)
    sig.score = score
    sig.reasons = [f"{name}:{'+' if v > 0 else ''}{v}" for name, v in votes]

    # --- RSI extreme veto (don't chase) ----------------------------------
    if direction == "BUY" and rsi_v >= p["rsi_overbought"]:
        sig.direction = "NONE"
        sig.reasons.append(f"veto: RSI {rsi_v:.0f} overbought")
        return sig
    if direction == "SELL" and rsi_v <= p["rsi_oversold"]:
        sig.direction = "NONE"
        sig.reasons.append(f"veto: RSI {rsi_v:.0f} oversold")
        return sig

    # --- higher-timeframe veto: never fight a clear HTF trend ------------
    if htf != 0 and htf != want:
        sig.direction = "NONE"
        sig.reasons.append("veto: against HTF trend")
        return sig

    if score >= p["min_confluence_score"]:
        sig.direction = direction
    else:
        sig.reasons.append(f"score {score} < {p['min_confluence_score']}")

    return sig
