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
