"""Failure alerts via Resend email (plan 1.1.4).

One job: get a message to the operator when something is wrong (a crash, a
naked position, a missed heartbeat). Everything here fails *open* — an alert
outage must never crash or block a trading run, so a missing key or a failed
POST degrades to a stderr warning and a no-op, never an exception.
"""

from __future__ import annotations  # py3.9 compat

import sys

import requests

from bot import config

_RESEND_URL = "https://api.resend.com/emails"
_TIMEOUT = 10


def alert(subject: str, body: str = "") -> bool:
    """Email ``subject``/``body`` to ALERT_EMAIL_TO. Returns True if the send
    was accepted, False otherwise. Never raises."""
    if not (config.RESEND_API_KEY and config.ALERT_EMAIL_TO):
        print(f"ALERT (unconfigured, not sent): {subject}", file=sys.stderr)
        return False
    try:
        resp = requests.post(
            _RESEND_URL,
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            json={
                "from": config.ALERT_EMAIL_FROM,
                "to": [config.ALERT_EMAIL_TO],
                "subject": subject,
                "text": body or subject,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:  # network, auth, timeout — all non-fatal here
        print(f"ALERT send failed ({exc}): {subject}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Message rendering
#
# An alert exists to make someone act. The first version of the naked-position
# alert named the symbols and stopped there; read four days running it looked
# like a lint warning, and the account carried $92,712 of unstopped exposure
# through it. These builders say what is at stake, how long it has been true,
# what will and won't save you, and what to type.
# ---------------------------------------------------------------------------

_KILL_HINT = "ssh pi@jerome.local 'touch jerome/KILL'"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def naked_position(rows: list[tuple[str, float, float, float]],
                   equity: float,
                   history: dict[str, tuple[int, str]]) -> tuple[str, str]:
    """Build (subject, body) for the unprotected-position page.

    ``rows`` is (symbol, qty, avg_entry, market_value); ``history`` maps symbol
    to (runs_reported, first_ts) from journal.naked_history()."""
    exposure = sum(mv for _, _, _, mv in rows)
    pct = (exposure / equity * 100) if equity > 0 else 0.0
    repeats = max((h[0] for h in history.values()), default=0)

    # Escalate rather than repeat: an unresolved condition on its 13th run
    # should not look identical to its first.
    headline = f"STILL UNPROTECTED (run {repeats})" if repeats > 1 else "UNPROTECTED"
    subject = (f"[JEROME] {headline} — {_plural(len(rows), 'position')}, "
               f"${exposure:,.0f} with no stop")

    lines = []
    for sym, qty, avg, mv in sorted(rows, key=lambda r: -r[3]):
        runs, first = history.get(sym, (0, ""))
        since = f"since {first[:10]}" if first else "newly detected"
        lines.append(f"  {sym:<6} {qty:>8,.0f} @ {avg:>10,.2f}   ${mv:>12,.2f}   {since}")

    # A 20% adverse gap is the number that makes the risk concrete: it is
    # ordinary for a single name on bad news, and it is roughly the drawdown
    # halt the account is supposed to never reach.
    gap_loss = exposure * 0.20

    body = f"""{_plural(len(rows), 'open position')} have NO broker-side stop loss right now.

{chr(10).join(lines)}

  Total exposure: ${exposure:,.2f} = {pct:.1f}% of ${equity:,.2f} equity.

WHAT THIS MEANS
  There is nothing between these positions and a full loss. The -6% daily
  and -20% drawdown breakers only evaluate at the next scheduled run
  (09:00 / 12:30 / 15:45 ET) — they cannot act on an intraday or overnight
  gap. A 20% adverse gap on this book costs ~${gap_loss:,.0f} and no code
  in this bot would prevent it.

  These positions also cannot exit by take-profit, so they will sit in the
  book consuming open slots until you act — new signals are being dropped
  as slots_full while they do.

WHAT TO DO NOW
  Flatten:  {_KILL_HINT}
            (next run cancels all orders and liquidates)
  Re-arm:   cd jerome && .venv/bin/python -m bot.main reattach-stops

  Ignoring this is a decision to hold ${exposure:,.0f} naked until you act."""
    return subject, body


def crashed(session: str, traceback_text: str) -> tuple[str, str]:
    """Build (subject, body) for an unhandled exception in a run."""
    subject = f"[JEROME] CRASH in the {session} run — trading may be halted"
    body = f"""The {session} run raised an unhandled exception and did not complete.

WHAT THIS MEANS
  Everything after the failure point was skipped. Depending on where it
  died that can mean: signals never scanned, the risk gate never ran, or
  orders submitted but never journaled. Open positions keep their
  broker-side brackets (those live at Alpaca, not here), but nothing new
  will be managed until the next scheduled run succeeds.

WHAT TO DO NOW
  Check:    ssh pi@jerome.local 'tail -50 jerome/data/cron.log'
  Flatten:  {_KILL_HINT}   (if you want to be flat while diagnosing)

  If the next scheduled run also fails, the bot is down, not glitching.

--- traceback (last 1500 chars) ---
{traceback_text}"""
    return subject, body
