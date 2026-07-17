"""Signal memory — lightweight SQLite log of every published AI call.

Pure stdlib (sqlite3), so it stays Termux-friendly. Used to give the AI
analyst a look at how similar setups on the same symbol/direction played
out recently, and as a durable audit trail of what was actually published.

This intentionally does not try to auto-grade outcomes (that needs price
history after the fact, which is a separate follow-up); `result` starts as
"pending" and can be updated later by an external process if desired.
"""
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.path.join(os.path.dirname(__file__), "signal_history.db")
_lock = threading.Lock()

_COLUMNS = (
    "symbol", "timestamp", "setup_type", "direction", "entry", "stop",
    "target", "market_condition", "trade_quality", "ai_reasoning", "result",
)


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            setup_type TEXT,
            direction TEXT,
            entry REAL,
            stop REAL,
            target REAL,
            market_condition TEXT,
            trade_quality TEXT,
            ai_reasoning TEXT,
            result TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol_dir ON signal_history(symbol, direction)")
    return conn


def record(entry):
    """Persist a published (non-WAIT) or gated signal for future context.
    Never raises — memory is a nice-to-have, not a hard dependency."""
    try:
        with _lock:
            conn = _conn()
            conn.execute(
                f"INSERT INTO signal_history ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                tuple(entry.get(c) for c in _COLUMNS),
            )
            conn.commit()
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def recent_similar(symbol, direction=None, limit=3):
    """Most recent entries for this symbol (optionally filtered by
    direction), most recent first. Returns [] on any error."""
    try:
        with _lock:
            conn = _conn()
            cur = conn.cursor()
            if direction:
                cur.execute(
                    "SELECT timestamp, direction, setup_type, entry, stop, target, "
                    "market_condition, trade_quality, result FROM signal_history "
                    "WHERE symbol=? AND direction=? ORDER BY timestamp DESC LIMIT ?",
                    (symbol, direction, limit),
                )
            else:
                cur.execute(
                    "SELECT timestamp, direction, setup_type, entry, stop, target, "
                    "market_condition, trade_quality, result FROM signal_history "
                    "WHERE symbol=? ORDER BY timestamp DESC LIMIT ?",
                    (symbol, limit),
                )
            rows = cur.fetchall()
            conn.close()
    except Exception:  # noqa: BLE001
        return []
    cols = ("timestamp", "direction", "setup_type", "entry", "stop", "target",
            "market_condition", "trade_quality", "result")
    return [dict(zip(cols, row)) for row in rows]
