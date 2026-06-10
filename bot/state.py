"""Shared in-memory state bridging the trading fleet and the dashboard."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class AgentStatus:
    symbol: str
    status: str = "starting"        # starting | scanning | in_trade | paused | error
    detail: str = ""
    last_signal: str = ""
    last_price: float = 0.0
    spread_points: float = 0.0
    atr: float = 0.0
    trend: str = "?"
    updated_at: float = 0.0

    def set(self, status: str, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        self.updated_at = time.time()


@dataclass
class FleetState:
    started_at: float = field(default_factory=time.time)
    account: dict = field(default_factory=dict)
    positions: list = field(default_factory=list)
    agents: dict[str, AgentStatus] = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    market_open: bool = True
    equity_curve: list = field(default_factory=list)  # [(ts, equity), ...]

    def agent(self, symbol: str) -> AgentStatus:
        if symbol not in self.agents:
            self.agents[symbol] = AgentStatus(symbol=symbol)
        return self.agents[symbol]

    def push_equity(self, equity: float, max_points: int = 2880) -> None:
        self.equity_curve.append((time.time(), equity))
        if len(self.equity_curve) > max_points:
            self.equity_curve = self.equity_curve[-max_points:]
