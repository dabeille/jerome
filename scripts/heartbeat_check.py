"""Dead-man switch (plan 1.1.4): alert if the morning run never journaled an
equity snapshot on a trading day. Covers the "cron never fired" failure that
no in-process alert could catch. Wire to a ~10:00 ET cron line.

    python -m scripts.heartbeat_check
"""

import sys
from datetime import datetime

from bot import alerts, broker, config, journal


def main() -> None:
    config.validate()
    if not broker.is_trading_day():
        print("Not a trading day — heartbeat check skipped.")
        return

    today = datetime.now(broker.ET).date().isoformat()
    if journal.equity_at_day_start(today) > 0:
        print(f"Heartbeat OK — equity snapshot present for {today}.")
        return

    alerts.alert(
        "bot HEARTBEAT MISSED",
        f"No equity snapshot was journaled on trading day {today} by heartbeat "
        f"time. The morning run may not have fired — check the Pi and cron.log.",
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
