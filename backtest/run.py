"""CLI for the backtest engine (steps 0.5.2/0.5.4).

    python -m backtest.run --start 2019-01-01 --end 2026-06-30

Runs entirely offline against cached bars in data/bars/ (BOT_MODE=backtest
means no broker connection and no Alpaca keys are required). Writes the
equity curve to data/backtest_equity.csv and the full report (expectancy,
win rate, max drawdown, trades/day, per-strategy/exit breakdowns, halt log,
signal funnel) to data/backtest_report.md, and prints a compact summary.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

os.environ.setdefault("BOT_MODE", "backtest")  # must precede any bot.* import

from backtest import report  # noqa: E402
from backtest.engine import STARTING_EQUITY, run_backtest  # noqa: E402
from bot import config, data  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--strict", action="store_true",
                        help="also gate the run on advisory data issues "
                             "(e.g. suspected splits), not just gating ones")
    parser.add_argument("--resume-after", type=int, default=None, metavar="N",
                        help="model the plan §5 human review: stay flat N "
                             "trading days after a drawdown halt, then rebase "
                             "the high-water mark and continue (default: the "
                             "halt ends the run, faithful to live)")
    parser.add_argument("--equity", type=float, default=STARTING_EQUITY, metavar="N",
                        help="starting equity (default: $%(default)s)")
    parser.add_argument("--report", default=None, metavar="PATH",
                        help="report path (default: data/backtest_report.md)")
    # --- 0.5.3 tuning knobs (default = live behaviour) ---
    parser.add_argument("--fractional", action="store_true",
                        help="allow sub-share sizing (backtest-only; live uses "
                             "whole-share Alpaca brackets)")
    parser.add_argument("--target-r", type=float, default=None, metavar="R",
                        help="momentum take-profit distance in R (default: 3.0)")
    parser.add_argument("--time-stop", type=int, default=None, metavar="N",
                        help="force-exit a position after N trading days if no "
                             "bracket leg fired (default: off)")
    parser.add_argument("--max-positions", type=int, default=None, metavar="N",
                        help="override MAX_OPEN_POSITIONS / MAX_TRADES_PER_DAY "
                             "(default: config = 3 / 3)")
    parser.add_argument("--no-meanrev", action="store_true",
                        help="run momentum only")
    args = parser.parse_args()

    kwargs: dict = {}
    if args.target_r is not None:
        kwargs["target_r"] = args.target_r
    if args.max_positions is not None:
        kwargs["max_open_positions"] = args.max_positions
        kwargs["max_trades_per_day"] = args.max_positions
    if args.no_meanrev:
        kwargs["strategies"] = ("momentum",)

    try:
        result = run_backtest(args.start, args.end, strict=args.strict,
                              resume_after_days=args.resume_after,
                              starting_equity=args.equity,
                              fractional=args.fractional,
                              time_stop_days=args.time_stop,
                              **kwargs)
    except RuntimeError as e:
        raise SystemExit(str(e))

    out_path = config.DATA_DIR / "backtest_equity.csv"
    result.equity_curve.to_csv(out_path, header=["equity"])
    print(f"Equity curve written to {out_path}")

    report_path = Path(args.report) if args.report else config.DATA_DIR / "backtest_report.md"
    bars = data.get_daily_bars(config.UNIVERSE, refresh=False)
    report.write_report(result, bars["SPY"], report_path)
    print(f"Report written to {report_path}")

    trades, equity_curve = result.trades, result.equity_curve
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

    if result.halts:
        last = result.halts[-1]
        outcome = "resumed" if last.resumed else "TERMINAL — run stopped early"
        print(f"Halt: {last.kind} on {last.date} (equity ${last.equity:,.2f}, "
             f"HWM ${last.hwm:,.2f}) — {outcome}")


if __name__ == "__main__":
    main()
