"""Scalping signal engine.

Confluence model (per symbol):
  1. Higher-timeframe trend filter: EMA50 vs EMA200 on the trend timeframe.
  2. Entry trigger: EMA9/EMA21 cross on the entry timeframe, in trend direction.
  3. Momentum filter: RSI(14) confirms but is not overextended.
  4. Volatility filter: ATR must be meaningful (dead markets are skipped).
  5. Spread filter: handled by the agent before submitting.

Stops/targets are ATR-multiples so they self-adapt to current volatility.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .indicators import atr, ema, rsi


@dataclass
class Signal:
    direction: str        # "buy" | "sell"
    atr_value: float      # current ATR in price units
    sl_distance: float    # price units
    tp_distance: float    # price units
    reason: str


class ScalpStrategy:
    def __init__(
        self,
        atr_sl_mult: float = 1.5,
        atr_tp_mult: float = 2.2,
        rsi_long_min: float = 50.0,
        rsi_long_max: float = 72.0,
        rsi_short_min: float = 28.0,
        rsi_short_max: float = 50.0,
    ):
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult
        self.rsi_long = (rsi_long_min, rsi_long_max)
        self.rsi_short = (rsi_short_min, rsi_short_max)

    @staticmethod
    def trend_direction(trend_df: pd.DataFrame) -> str:
        """'up' | 'down' | 'flat' from the higher timeframe."""
        if len(trend_df) < 210:
            return "flat"
        close = trend_df["close"]
        fast, slow = ema(close, 50), ema(close, 200)
        f, s = fast.iloc[-1], slow.iloc[-1]
        price = close.iloc[-1]
        if f > s and price > f:
            return "up"
        if f < s and price < f:
            return "down"
        return "flat"

    def evaluate(self, entry_df: pd.DataFrame, trend_df: pd.DataFrame) -> Signal | None:
        if len(entry_df) < 60:
            return None

        trend = self.trend_direction(trend_df)
        if trend == "flat":
            return None

        close = entry_df["close"]
        fast = ema(close, 9)
        slow = ema(close, 21)
        momentum = rsi(close, 14)
        vol = atr(entry_df, 14)

        a = float(vol.iloc[-1])
        if a <= 0 or a < float(vol.rolling(100, min_periods=20).mean().iloc[-1]) * 0.5:
            return None  # volatility too low for a scalp to clear the spread

        cross_up = fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
        cross_down = fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]
        m = float(momentum.iloc[-1])

        if trend == "up" and cross_up and self.rsi_long[0] <= m <= self.rsi_long[1]:
            return Signal(
                direction="buy",
                atr_value=a,
                sl_distance=a * self.atr_sl_mult,
                tp_distance=a * self.atr_tp_mult,
                reason=f"M15 uptrend + EMA9/21 cross up, RSI={m:.0f}",
            )
        if trend == "down" and cross_down and self.rsi_short[0] <= m <= self.rsi_short[1]:
            return Signal(
                direction="sell",
                atr_value=a,
                sl_distance=a * self.atr_sl_mult,
                tp_distance=a * self.atr_tp_mult,
                reason=f"M15 downtrend + EMA9/21 cross down, RSI={m:.0f}",
            )
        return None

    def exit_signal(self, entry_df: pd.DataFrame, position_type: str) -> bool:
        """Momentum-flip exit: close if the fast EMA crosses against the trade."""
        if len(entry_df) < 30:
            return False
        close = entry_df["close"]
        fast, slow = ema(close, 9), ema(close, 21)
        if position_type == "buy":
            return fast.iloc[-1] < slow.iloc[-1] and fast.iloc[-2] >= slow.iloc[-2]
        return fast.iloc[-1] > slow.iloc[-1] and fast.iloc[-2] <= slow.iloc[-2]
