"""Journal: every decision, order, and equity snapshot goes through here.

SQLite (stdlib, zero deps, fine on a Pi's SD card) + a human-readable
dashboard.md regenerated after each run.
"""

import sqlite3
from datetime import datetime, timezone

from bot import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    ts TEXT, run TEXT, symbol TEXT, strategy TEXT, action TEXT,
    score REAL, entry REAL, stop REAL, target REAL, qty INTEGER,
    reasoning TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    ts TEXT, symbol TEXT, side TEXT, qty INTEGER, order_type TEXT,
    status TEXT, broker_order_id TEXT, fill_price REAL
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT, equity REAL, cash REAL, note TEXT
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.JOURNAL_DB)
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_decision(run: str, symbol: str, strategy: str, action: str,
                 score: float = 0, entry: float = 0, stop: float = 0,
                 target: float = 0, qty: int = 0, reasoning: str = "") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), run, symbol, strategy, action, score, entry, stop,
             target, qty, reasoning),
        )


def log_order(symbol: str, side: str, qty: int, order_type: str,
              status: str, broker_order_id: str = "",
              fill_price: float = 0) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
            (_now(), symbol, side, qty, order_type, status,
             broker_order_id, fill_price),
        )


def log_equity(equity: float, cash: float, note: str = "") -> None:
    with _conn() as c:
        c.execute("INSERT INTO equity VALUES (?,?,?,?)",
                  (_now(), equity, cash, note))


def high_water_mark() -> float:
    with _conn() as c:
        row = c.execute("SELECT MAX(equity) FROM equity").fetchone()
    return row[0] or 0.0


def equity_at_day_start(date_utc: str) -> float:
    """First equity snapshot on the given UTC date (YYYY-MM-DD)."""
    with _conn() as c:
        row = c.execute(
            "SELECT equity FROM equity WHERE ts >= ? ORDER BY ts LIMIT 1",
            (date_utc,),
        ).fetchone()
    return row[0] if row else 0.0


def write_dashboard(account_summary: str, positions_summary: str,
                    recent_decisions: str) -> None:
    config.DASHBOARD.write_text(
        f"# Bot dashboard — {_now()}\n\n"
        f"Mode: **{config.MODE}**\n\n"
        f"## Account\n\n{account_summary}\n\n"
        f"## Open positions\n\n{positions_summary}\n\n"
        f"## Recent decisions\n\n{recent_decisions}\n"
    )
