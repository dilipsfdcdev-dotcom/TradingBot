"""Persistent log of every webhook request the bridge received (SQLite)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class SignalStore:
    def __init__(self, path: str = "data/bridge.db"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS signals ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, ip TEXT, action TEXT, symbol TEXT,"
                "status TEXT NOT NULL, detail TEXT, took_ms INTEGER)")
            self._db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")

    def add(self, ip: str, action, symbol, status: str, detail: str = "", took_ms: int | None = None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO signals (ts, ip, action, symbol, status, detail, took_ms) VALUES (?,?,?,?,?,?,?)",
                (time.time(), ip, str(action or "")[:40], str(symbol or "")[:40], status, detail[:300], took_ms))

    def recent(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def count_since(self, ts: float) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM signals WHERE ts >= ?", (ts,)).fetchone()[0]

    def get_state(self, key: str, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, json.dumps(value)))
