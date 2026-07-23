"""Refresh the DYNAMIC universe tier (run weekly, pre-market Monday):

    python -m scripts.refresh_universe

Cron on the Pi:
    30 8 * * 1  cd ~/jerome && .venv/bin/python -m scripts.refresh_universe
"""

import sys

from bot import config, universe


def main() -> None:
    if not (config.ALPACA_KEY_ID and config.ALPACA_SECRET):
        sys.exit("Missing Alpaca keys — check .env")
    payload = universe.refresh()
    print(
        f"Dynamic universe -> {config.DYNAMIC_UNIVERSE_FILE} "
        f"({len(payload['symbols'])} symbols)\n"
    )
    print(f"{'sym':<6} {'close':>9} {'$vol(20d)':>10} {'ATR%':>6}")
    for sym in payload["symbols"]:
        m = payload["metrics"][sym]
        print(
            f"{sym:<6} {m['close']:>9,.2f} {m['dollar_vol'] / 1e6:>9,.0f}M "
            f"{m['atr_pct']:>6.2f}"
        )


if __name__ == "__main__":
    main()
