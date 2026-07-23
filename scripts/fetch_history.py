"""Step 0.3.2: bulk-download ~5 years of daily bars for the whole universe.

Seeds data/bars/ so the backtester and signal modules have history to work
with. SPY/QQQ (and the other benchmarks) are already part of the configured
universe. Alpaca is primary; yfinance is the per-symbol fallback (see bot.data).

    python -m scripts.fetch_history                 # all UNIVERSE, 5y, fresh
    python -m scripts.fetch_history --years 3
    python -m scripts.fetch_history --no-refresh    # keep symbols already cached
    python -m scripts.fetch_history --symbols SPY,QQQ,AAPL

Exits non-zero if any requested symbol could not be fetched from either source.
"""

from __future__ import annotations  # py3.9 compat

import argparse
import sys

from bot import config, data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=5,
                        help="years of history to pull (default: 5)")
    parser.add_argument("--symbols", default="",
                        help="comma-separated override; default is config.UNIVERSE")
    parser.add_argument("--no-refresh", dest="refresh", action="store_false",
                        help="use cached CSVs where present instead of re-downloading")
    parser.set_defaults(refresh=True)
    args = parser.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else config.UNIVERSE)

    print(f"Fetching {args.years}y of daily bars for {len(symbols)} symbols "
          f"(refresh={args.refresh}) into {config.BARS_DIR} ...\n")
    bars = data.get_daily_bars(symbols, years=args.years, refresh=args.refresh)

    missing: list[str] = []
    for sym in symbols:
        df = bars.get(sym)
        if df is None or df.empty:
            missing.append(sym)
            print(f"  MISSING  {sym}")
            continue
        print(f"  {sym:6} {len(df):5,} bars  "
              f"{df.index.min().date()} → {df.index.max().date()}")

    ok = len(symbols) - len(missing)
    print(f"\n{ok}/{len(symbols)} symbols cached in {config.BARS_DIR}")
    if missing:
        print(f"Could not fetch: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
