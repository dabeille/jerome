"""Step 0.3.4/0.3.5: report data-quality issues over the cached bars.

Reads exactly what's on disk in data/bars/ (never triggers a download) and runs
bot.data_checks over it. Exits non-zero on any *gating* issue (ohlc_integrity,
missing_days, stale), so it can gate a backtest or run in CI. Advisory issues
(suspected_split — real 50%+ movers in high-beta names, not just unadjusted
splits) are printed but don't fail the run unless --strict is passed.

    python -m scripts.check_data                 # every cached symbol
    python -m scripts.check_data --symbols SPY,SQ
    python -m scripts.check_data --strict         # advisory issues fail too
"""

from __future__ import annotations  # py3.9 compat

import argparse
import sys

import pandas as pd

from bot import config, data_checks


def _load_cached(symbols: list[str] | None) -> dict[str, pd.DataFrame]:
    available = {p.stem: p for p in sorted(config.BARS_DIR.glob("*.csv"))}
    wanted = symbols or list(available)
    bars: dict[str, pd.DataFrame] = {}
    for sym in wanted:
        path = available.get(sym)
        if path is None:
            print(f"WARNING: no cached bars for {sym} (skipped)", file=sys.stderr)
            continue
        bars[sym] = pd.read_csv(path, index_col=0, parse_dates=True)
    return bars


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="",
                        help="comma-separated subset; default is every cached symbol")
    parser.add_argument("--benchmark", default="SPY",
                        help="trading-calendar reference for gap/stale checks")
    parser.add_argument("--strict", action="store_true",
                        help="also fail on advisory issues (e.g. suspected_split)")
    args = parser.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               or None)
    bars = _load_cached(symbols)
    if not bars:
        sys.exit("No cached bars found — run `python -m scripts.fetch_history` first.")

    issues = data_checks.run_checks(bars, benchmark=args.benchmark)

    print(f"Checked {len(bars)} symbols against {args.benchmark}.\n")
    if not issues:
        print("No data-quality issues found.")
        return

    gating = [i for i in issues if i.severity == "gating"]
    advisory = [i for i in issues if i.severity == "advisory"]

    if gating:
        print(f"Gating issues ({len(gating)}):")
        for issue in gating:
            print(f"  {issue}")
    if advisory:
        print(f"Advisory warnings ({len(advisory)}) — informational only unless --strict:")
        for issue in advisory:
            print(f"  {issue}")

    kinds = ", ".join(sorted({i.kind for i in issues}))
    print(f"\n{len(issues)} issue(s) across {len({i.symbol for i in issues})} "
          f"symbol(s): {kinds}")
    sys.exit(1 if data_checks.should_gate(issues, strict=args.strict) else 0)


if __name__ == "__main__":
    main()
