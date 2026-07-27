"""Walk-forward tuning sweep (step 0.5.3), training window only.

Runs a staged set of parameter configurations over the 2019-01-01 → 2023-12-31
training window and ranks them, so a parameter set can be chosen *before* the
2024-01 → 2026-06 validation window is ever touched. A hard guard refuses any
window ending past the training boundary — the validation window is run once,
later, after the chosen params are approved (see docs/backtest-findings.md).

    python -m backtest.sweep                 # full staged sweep → data/sweep_results.csv

All runs use resume_after_days=5 so every config replays the whole training
window (a terminal drawdown halt would otherwise truncate configs to different
lengths and make the comparison meaningless). Each run is seconds; the whole
sweep is minutes.
"""

from __future__ import annotations

import argparse
import os

os.environ.setdefault("BOT_MODE", "backtest")  # must precede any bot.* import

import pandas as pd  # noqa: E402

from backtest import metrics  # noqa: E402
from backtest.engine import run_backtest  # noqa: E402
from bot import config, data  # noqa: E402

TRAIN_START = "2019-01-01"
TRAIN_END = "2023-12-31"
CONTROL_START = "2022-06-01"  # recent-regime control, still inside the guard
GUARD_END = pd.Timestamp("2023-12-31")

# Historical gate thresholds used by the banked 0.5.3 sweep; the current
# Phase-1 gate (restated per findings P4) lives in backtest.report constants.
GATE_TRADES_PER_DAY = (1.0, 3.0)
GATE_MAX_DRAWDOWN = 0.25

BASE = dict(resume_after_days=5)  # applied to every run for window comparability


def _guard(end: str) -> None:
    if pd.Timestamp(end) > GUARD_END:
        raise SystemExit(
            f"Refusing a window ending {end}: past the {GUARD_END.date()} training "
            "boundary. The validation window (2024-01 → 2026-06) is run once, later, "
            "after the tuned params are approved — see docs/backtest-findings.md."
        )


def _run_row(label: str, start: str, end: str, spy_bars: pd.DataFrame,
             **kwargs) -> dict:
    """One backtest → a flat metrics row."""
    _guard(end)
    result = run_backtest(start, end, **BASE, **kwargs)
    ts = metrics.trade_stats(result.trades)
    cs = metrics.curve_stats(result.trades, result.equity_curve)
    spy_ret = metrics.spy_buy_hold_return(spy_bars, result.equity_curve)
    total_ret = (result.equity_curve.iloc[-1] / result.equity_curve.iloc[0] - 1
                 if len(result.equity_curve) >= 2 else 0.0)
    lo, hi = GATE_TRADES_PER_DAY
    p = result.params
    return {
        "label": label,
        "window": f"{start}→{end}",
        "fractional": p["fractional"],
        "target_r": p["target_r"],
        "time_stop_days": p["time_stop_days"],
        "max_open_positions": p["max_open_positions"],
        "rsi_oversold": p["rsi_oversold"],
        "meanrev_stop_pct": p["stop_pct"],
        "strategies": p["strategies"],
        "trades": ts.count,
        "trades_per_day": round(cs.trades_per_day, 4),
        "win_rate": round(ts.win_rate, 4),
        "expectancy_r": round(ts.expectancy_r, 4),
        "profit_factor": round(ts.profit_factor, 3),
        "max_drawdown": round(cs.max_drawdown, 4),
        "total_return": round(float(total_ret), 4),
        "spy_return": round(spy_ret, 4),
        "edge_vs_spy": round(float(total_ret) - spy_ret, 4),
        "gate_trades_ok": lo <= cs.trades_per_day <= hi,
        "gate_dd_ok": abs(cs.max_drawdown) < GATE_MAX_DRAWDOWN,
    }


def _configs() -> list[tuple[str, str, str, dict]]:
    """(label, start, end, kwargs) for every run, in three stages."""
    runs: list[tuple[str, str, str, dict]] = []

    # Stage 1 — fractional A/B (the user's question), train + recent control.
    runs.append(("s1-baseline", TRAIN_START, TRAIN_END, {}))
    runs.append(("s1-fractional", TRAIN_START, TRAIN_END, dict(fractional=True)))
    runs.append(("s1-baseline-ctl", CONTROL_START, TRAIN_END, {}))
    runs.append(("s1-fractional-ctl", CONTROL_START, TRAIN_END, dict(fractional=True)))

    # Stage 2 — one-factor probes off the baseline, on the training window.
    for tr in (2.0, 2.5, 3.0):
        runs.append((f"s2-target_r={tr}", TRAIN_START, TRAIN_END, dict(target_r=tr)))
    for ts_days in (None, 10, 20):
        runs.append((f"s2-time_stop={ts_days}", TRAIN_START, TRAIN_END,
                     dict(time_stop_days=ts_days)))
    for slots in (3, 5, 6):
        runs.append((f"s2-slots={slots}", TRAIN_START, TRAIN_END,
                     dict(max_open_positions=slots, max_trades_per_day=slots)))
    runs.append(("s2-momentum-only", TRAIN_START, TRAIN_END,
                 dict(strategies=("momentum",))))
    runs.append(("s2-meanrev-only", TRAIN_START, TRAIN_END,
                 dict(strategies=("meanrev",))))

    # Stage 3 — combined grid over the levers (fractional on throughout).
    for tr in (2.0, 3.0):
        for ts_days in (None, 10, 20):
            for slots in (3, 5, 6):
                runs.append((
                    f"s3-tr{tr}-ts{ts_days}-sl{slots}",
                    TRAIN_START, TRAIN_END,
                    dict(fractional=True, target_r=tr, time_stop_days=ts_days,
                         max_open_positions=slots, max_trades_per_day=slots),
                ))
    return runs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=TRAIN_START, help="override train start")
    parser.add_argument("--end", default=TRAIN_END,
                        help="override train end (still guarded ≤ 2023-12-31)")
    parser.add_argument("--out", default=None, help="CSV path (default data/sweep_results.csv)")
    args = parser.parse_args()

    _guard(args.end)  # fail fast before any run if the override crosses the boundary

    spy_bars = data.get_daily_bars(config.UNIVERSE, refresh=False)["SPY"]

    rows: list[dict] = []
    for label, start, end, kwargs in _configs():
        # honour a global window override on the training-window stages
        if start == TRAIN_START:
            start, end = args.start, args.end
        row = _run_row(label, start, end, spy_bars, **kwargs)
        rows.append(row)
        print(f"  {label:26} {row['trades']:4d} trades  "
              f"{row['trades_per_day']:.3f}/day  exp {row['expectancy_r']:+.2f}R  "
              f"DD {row['max_drawdown']:+.1%}  ret {row['total_return']:+.1%}")

    df = pd.DataFrame(rows)
    out_path = args.out or (config.DATA_DIR / "sweep_results.csv")
    df.to_csv(out_path, index=False)
    print(f"\n{len(df)} runs → {out_path}")

    # Ranked summary: gate-DD-passing configs first, then by expectancy.
    ranked = df.sort_values(
        ["gate_dd_ok", "expectancy_r"], ascending=[False, False]
    )
    print("\nTop 10 by expectancy (drawdown-gate passers first):\n")
    cols = ["label", "trades_per_day", "expectancy_r", "profit_factor",
            "max_drawdown", "total_return", "gate_trades_ok", "gate_dd_ok"]
    print(ranked[cols].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
