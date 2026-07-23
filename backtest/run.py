"""CLI for the backtest engine (step 0.5.2).

    python -m backtest.run --start 2019-01-01 --end 2026-06-30

Runs entirely offline against cached bars in data/bars/ (BOT_MODE=backtest
means no broker connection and no Alpaca keys are required). Prints a thin
sanity summary and writes the equity curve to data/backtest_equity.csv. The
full expectancy/max-DD/trades-per-day report is step 0.5.4.
"""

from __future__ import annotations  # py3.9 compat

import argparse
import os

os.environ.setdefault("BOT_MODE", "backtest")  # must precede any bot.* import

from backtest.engine import run_backtest  # noqa: E402 -- after BOT_MODE is set
from bot import config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--strict", action="store_true",
                        help="also gate the run on advisory data issues "
                             "(e.g. suspected splits), not just gating ones")
    args = parser.parse_args()

    try:
        trades, equity_curve = run_backtest(args.start, args.end, strict=args.strict)
    except RuntimeError as e:
        raise SystemExit(str(e))

    out_path = config.DATA_DIR / "backtest_equity.csv"
    equity_curve.to_csv(out_path, header=["equity"])
    print(f"Equity curve written to {out_path}")

    if not trades or len(equity_curve) < 2:
        print(f"{args.start} -> {args.end}: {len(trades)} trade(s) — "
              f"not enough data for a summary.")
        return

    total_return = equity_curve.iloc[-1] / equity_curve.iloc[0] - 1
    wins = sum(1 for t in trades if t.pnl > 0)
    win_rate = wins / len(trades)
    days = len(equity_curve)
    print(f"{args.start} -> {args.end} ({days} trading days): "
         f"{len(trades)} trades, win rate {win_rate:.0%}, "
         f"total return {total_return:+.1%}")


if __name__ == "__main__":
    main()
