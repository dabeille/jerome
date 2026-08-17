"""Journal: every decision, order, and equity snapshot goes through here.

SQLite (stdlib, zero deps, fine on a Pi's SD card) + a human-readable
dashboard.md regenerated after each run.
"""

import html
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from bot import config

RESUME_NOTE = "resume-after-halt"

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
CREATE TABLE IF NOT EXISTS funnel (
    ts TEXT, run TEXT, payload TEXT
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


def update_order(broker_order_id: str, status: str,
                 fill_price: float = 0) -> int:
    """Update a previously-logged order with its terminal status and fill
    price. Returns the number of rows changed (0 if we never logged it — e.g.
    a bracket child leg, or an order placed by hand in the Alpaca UI).

    Called by broker.reconcile_orders() at the close. Without it the orders
    table keeps only submission-time state and no fills, which is why the
    first week of paper trading produced no measurable slippage data."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE orders SET status = ?, fill_price = ? "
            "WHERE broker_order_id = ?",
            (status, fill_price, broker_order_id),
        )
        return cur.rowcount


def naked_history(symbol: str, within_days: int = 30) -> tuple[int, str]:
    """(runs, first_ts) for NAKED-POSITION rows naming ``symbol`` in the last
    ``within_days``. Feeds the alert's "unprotected since" line.

    Deliberately a simple window rather than true streak detection: a position
    cannot be reported naked before it is opened, so within a 30-day window the
    earliest row is the start of the current episode in every realistic case.
    Returns (0, "") if the symbol has never been reported."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*), MIN(ts) FROM decisions "
            "WHERE action = 'NAKED-POSITION' AND ts >= ? AND symbol = ?",
            (cutoff, symbol),
        ).fetchone()
    return (row[0], row[1] or "") if row else (0, "")


def log_equity(equity: float, cash: float, note: str = "") -> None:
    with _conn() as c:
        c.execute("INSERT INTO equity VALUES (?,?,?,?)",
                  (_now(), equity, cash, note))


def log_funnel(run: str, payload: dict) -> None:
    """One signal-attrition row per live run (plan 1.1.5), so the live funnel
    is directly comparable to the backtest's. ``payload`` is the run's
    signals-in / drops-by-reason / approved counts plus the active strategy
    set, stored as JSON."""
    with _conn() as c:
        c.execute("INSERT INTO funnel VALUES (?,?,?)",
                  (_now(), run, json.dumps(payload, sort_keys=True)))


def last_funnel() -> tuple[str, str, dict] | None:
    """Most recent (ts, run, payload_dict) funnel row, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT ts, run, payload FROM funnel ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return row[0], row[1], json.loads(row[2])


def equity_series(limit: int = 60) -> list[tuple[str, float]]:
    """Last ``limit`` (ts, equity) snapshots in chronological order, excluding
    the resume markers (which repeat the resume-day equity as bookkeeping)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, equity FROM equity WHERE note != ? "
            "ORDER BY ts DESC LIMIT ?",
            (RESUME_NOTE, limit),
        ).fetchall()
    return list(reversed(rows))


def recent_decision_rows(limit: int = 15) -> list[tuple]:
    """Most recent (ts, run, symbol, strategy, action, reasoning) rows,
    newest first, for the dashboard."""
    with _conn() as c:
        return c.execute(
            "SELECT ts, run, symbol, strategy, action, reasoning "
            "FROM decisions ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()


def high_water_mark() -> float:
    """Peak equity since the most recent resume marker, or over all history
    if the bot has never been halted and resumed (plan §5)."""
    with _conn() as c:
        row = c.execute(
            "SELECT MAX(equity) FROM equity WHERE ts >= "
            "(SELECT COALESCE(MAX(ts), '') FROM equity WHERE note = ?)",
            (RESUME_NOTE,),
        ).fetchone()
    return row[0] or 0.0


def mark_resume(equity: float, cash: float) -> None:
    """Record operator-approved resumption after a drawdown halt (plan §5:
    'we review the journal together before resuming'). Rebases
    high_water_mark() to ignore pre-halt peaks."""
    log_equity(equity, cash, RESUME_NOTE)


def recent_decisions(action: str, limit: int = 5) -> list[tuple[str, str, str]]:
    """Most recent (ts, symbol, reasoning) rows logged with the given action,
    newest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, symbol, reasoning FROM decisions WHERE action = ? "
            "ORDER BY ts DESC LIMIT ?",
            (action, limit),
        ).fetchall()
    return rows


def last_entry_levels(symbol: str) -> tuple[float, float]:
    """(stop, target) from the most recent 'entered' decision for ``symbol``,
    or (0, 0) if we never logged one. Used by ``bot.main reattach-stops`` to
    re-arm a position at the levels its original bracket carried."""
    with _conn() as c:
        row = c.execute(
            "SELECT stop, target FROM decisions WHERE action = 'entered' "
            "AND symbol = ? ORDER BY ts DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    return (row[0], row[1]) if row else (0.0, 0.0)


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


def _sparkline_svg(points: list[tuple[str, float]], width: int = 640,
                   height: int = 120) -> str:
    """Inline SVG equity sparkline from (ts, equity) points. Stdlib only —
    no plotting dependency (the Pi serves this as a static file)."""
    values = [v for _, v in points]
    if len(values) < 2:
        return '<p class="muted">(not enough equity history yet)</p>'
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    pad = 6
    n = len(values)
    coords = []
    for i, v in enumerate(values):
        x = pad + i * (width - 2 * pad) / (n - 1)
        y = height - pad - (v - lo) / span * (height - 2 * pad)
        coords.append(f"{x:.1f},{y:.1f}")
    up = values[-1] >= values[0]
    color = "#2e7d32" if up else "#c62828"
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'preserveAspectRatio="none" role="img" aria-label="equity curve">'
        f'<polyline fill="none" stroke="{color}" stroke-width="2" '
        f'points="{" ".join(coords)}" /></svg>'
    )


def _html_table(headers: list[str], rows: list[tuple]) -> str:
    if not rows:
        return '<p class="muted">(none)</p>'
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def write_dashboard_html(account_summary: str,
                         positions: list[tuple]) -> None:
    """Render data/public/dashboard.html for the LAN dashboard (plan 2d).

    ``positions`` is a list of (symbol, qty, avg_price, unrealized_pl). The
    equity sparkline, recent decisions, and last funnel row are read from the
    journal here. Only data/public/ is ever served — never data/ itself."""
    spark = _sparkline_svg(equity_series())
    pos_table = _html_table(
        ["Symbol", "Qty", "Avg entry", "Unrealized P&L"], positions)
    dec_table = _html_table(
        ["Time (UTC)", "Run", "Symbol", "Strategy", "Action", "Reasoning"],
        recent_decision_rows(),
    )
    funnel = last_funnel()
    if funnel:
        f_ts, f_run, f_payload = funnel
        items = "".join(
            f"<li>{html.escape(str(k))}: {html.escape(str(v))}</li>"
            for k, v in sorted(f_payload.items())
        )
        funnel_html = (
            f'<p class="muted">{html.escape(f_ts)} · {html.escape(f_run)}</p>'
            f"<ul>{items}</ul>"
        )
    else:
        funnel_html = '<p class="muted">(no funnel row yet)</p>'

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Bot dashboard</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 900px;
          padding: 0 1rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1rem; margin-top: 1.8rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
  th, td {{ text-align: left; padding: 0.3rem 0.5rem; border-bottom: 1px solid #eee; }}
  .muted {{ color: #888; font-size: 0.85rem; }}
  .card {{ border: 1px solid #eee; border-radius: 8px; padding: 1rem; }}
</style></head><body>
<h1>Bot dashboard <span class="muted">— {html.escape(_now())} · mode {html.escape(config.MODE)}</span></h1>
<h2>Account</h2>
<p>{html.escape(account_summary)}</p>
<h2>Equity</h2>
<div class="card">{spark}</div>
<h2>Open positions</h2>
{pos_table}
<h2>Signal funnel (last run)</h2>
{funnel_html}
<h2>Recent decisions</h2>
{dec_table}
</body></html>
"""
    config.DASHBOARD_HTML.write_text(doc)
