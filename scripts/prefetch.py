"""Step 0.3.3: populate/refresh the day's news + earnings caches.

The daily-refresh counterpart to scripts/fetch_history.py (which seeds the
long-lived bar cache). Bars change slowly and are cached per-symbol forever;
news and the earnings calendar are cached per-*day* (data/news/<date>.json,
data/earnings/<date>.json), so this is meant to run each morning before the
trading loop so every session that day shares one fetch.

    python -m scripts.prefetch                       # whole config.UNIVERSE
    python -m scripts.prefetch --symbols SPY,NVDA
    python -m scripts.prefetch --earnings-days 10    # widen the veto horizon

Best-effort by design: "no headlines today" and "nobody reports soon" are valid
outcomes the fetchers already cache, so those exit 0. Exits non-zero only if a
fetch hard-fails such that nothing could be cached (e.g. bad/missing keys) — see
the warnings the underlying fetchers print to stderr.
"""

from __future__ import annotations  # py3.9 compat

import argparse
import sys

from bot import config, data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="",
                        help="comma-separated override; default is config.UNIVERSE")
    parser.add_argument("--earnings-days", type=int, default=5,
                        help="earnings-veto horizon in days (default: 5)")
    args = parser.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else config.UNIVERSE)

    print(f"Prefetching news + earnings for {len(symbols)} symbols "
          f"(earnings horizon {args.earnings_days}d) ...\n")

    headlines = data.get_headlines(symbols)
    with_news = {s: h for s, h in headlines.items() if h}
    total_headlines = sum(len(h) for h in with_news.values())
    print(f"News:     {len(with_news)}/{len(symbols)} symbols with coverage, "
          f"{total_headlines} headlines "
          f"→ {config.NEWS_DIR}")

    reporting = data.earnings_within(symbols, days=args.earnings_days)
    soon = ", ".join(sorted(reporting)) if reporting else "(none)"
    print(f"Earnings: {len(reporting)} reporting within {args.earnings_days}d: {soon} "
          f"→ {config.EARNINGS_DIR}")

    # The fetchers fail *open* (warn + return empty) rather than raise, so an
    # empty result here is either a genuinely quiet day or a data hiccup already
    # logged to stderr — not a reason to exit non-zero and block the loop.
    if not with_news and not reporting:
        print("\nNothing cached — quiet day, or check the warnings above "
              "(keys/network).", file=sys.stderr)


if __name__ == "__main__":
    main()
