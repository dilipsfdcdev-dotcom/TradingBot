"""
SQLite state store.

The bot WRITES its live state here (equity snapshots, open positions, trade
events, signals, news, status). The Streamlit dashboard only READS this file,
so the dashboard never has to touch MT5 and the two processes stay decoupled.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT, balance REAL, equity REAL, margin REAL, profit REAL
);
CREATE TABLE IF NOT EXISTS trade_events (
    ts TEXT, event TEXT, symbol TEXT, direction TEXT, volume REAL,
    price REAL, sl REAL, tp REAL, profit REAL, ticket INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    ts TEXT, symbol TEXT, timeframe TEXT, direction TEXT, score INTEGER,
    max_score INTEGER, price REAL, reasons TEXT
);
CREATE TABLE IF NOT EXISTS positions_snapshot (
    ts TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS news (
    ts TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS status (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON trade_events(ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _exec(self, sql: str, params: tuple = ()):
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    # ── writes ──────────────────────────────────────────────────────────
    def record_equity(self, acc: dict) -> None:
        self._exec(
            "INSERT INTO equity VALUES (?,?,?,?,?)",
            (_now(), acc.get("balance"), acc.get("equity"),
             acc.get("margin"), acc.get("profit")),
        )

    def record_event(self, event, symbol="", direction="", volume=0.0, price=0.0,
                     sl=0.0, tp=0.0, profit=0.0, ticket=0, detail="") -> None:
        self._exec(
            "INSERT INTO trade_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), event, symbol, direction, volume, price, sl, tp,
             profit, ticket, detail),
        )

    def record_signal(self, sig, symbol, timeframe) -> None:
        self._exec(
            "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?)",
            (_now(), symbol, timeframe, sig.direction, sig.score,
             sig.max_score, sig.price, json.dumps(sig.reasons)),
        )

    def snapshot_positions(self, positions: list[dict]) -> None:
        self._exec("DELETE FROM positions_snapshot")
        self._exec("INSERT INTO positions_snapshot VALUES (?,?)",
                   (_now(), json.dumps(positions, default=str)))

    def store_news(self, items: list[dict]) -> None:
        self._exec("DELETE FROM news")
        self._exec("INSERT INTO news VALUES (?,?)",
                   (_now(), json.dumps(items, default=str)))

    def set_status(self, key: str, value) -> None:
        self._exec(
            "INSERT INTO status(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, default=str)),
        )

    # ── reads (used by dashboard) ─────────────────────────────────────────
    def fetch_all(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_status(self) -> dict:
        rows = self.fetch_all("SELECT key, value FROM status")
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except (json.JSONDecodeError, TypeError):
                out[r["key"]] = r["value"]
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()
