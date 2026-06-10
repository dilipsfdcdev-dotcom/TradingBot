"""Trade journal + logging setup. Every order/close/halt is appended to a
JSONL file so the dashboard (and you) can audit everything the fleet did.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

JOURNAL_PATH = Path("logs/journal.jsonl")


def setup_logging(level: int = logging.INFO) -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-10s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(level)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    file = logging.FileHandler("logs/bot.log")
    file.setFormatter(fmt)
    root.addHandler(stream)
    root.addHandler(file)


class Journal:
    def __init__(self, path: Path = JOURNAL_PATH):
        self.path = path
        self.path.parent.mkdir(exist_ok=True)

    def record(self, event: str, **data) -> None:
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **data,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def tail(self, n: int = 100) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().strip().splitlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(out))
