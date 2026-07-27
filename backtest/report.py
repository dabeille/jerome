"""Backtest report (step 0.5.4): renders data/backtest_report.md.

Turns a BacktestResult into markdown: params snapshot, headline numbers, a
SPY buy-and-hold benchmark, per-strategy/exit-reason breakdowns, the halt
log, and the signal-attrition funnel. No strategy or risk parameter is
touched here — this module only measures and reports.
"""

from __future__ import annotations

import pandas as pd

from backtest import metrics
from backtest.engine import BacktestResult, HaltEvent, Trade

PHASE1_TRADES_PER_DAY = (1.0, 3.0)
PHASE1_MAX_DRAWDOWN = 0.25

_PARAM_LABELS = [
    ("start", "Window start"), ("end", "Window end"),
    ("starting_equity", "Starting equity"),
    ("resume_after_days", "Resume after (days)"), ("strict", "Strict data gate"),
    ("fractional", "Fractional shares"), ("time_stop_days", "Time-stop (days)"),
    ("strategies", "Strategies"),
    ("risk_per_trade", "Risk per trade"), ("max_position_pct", "Max position %"),
    ("max_open_positions", "Max open positions"), ("max_per_sector", "Max per sector"),
    ("daily_loss_limit", "Daily loss limit"), ("drawdown_halt", "Drawdown halt"),
    ("max_trades_per_day", "Max trades/day"),
    ("relvol_mult", "RELVOL_MULT"), ("rs_top_pct", "RS_TOP_PCT"),
    ("target_r", "TARGET_R"), ("rsi_oversold", "RSI_OVERSOLD"), ("stop_pct", "STOP_PCT"),
]


def build_report(result: BacktestResult, spy_bars: pd.DataFrame) -> str:
    """Render the full markdown report for one backtest run."""
    trades, equity_curve = result.trades, result.equity_curve
    lines: list[str] = ["# Backtest report", ""]
    lines += _params_section(result.params)
    lines += _headline_section(trades, equity_curve)
    lines += _benchmark_section(equity_curve, spy_bars)
    lines += _breakdown_table("By strategy", metrics.breakdown_by(trades, "strategy"))
    lines += _breakdown_table("By exit reason", metrics.breakdown_by(trades, "exit_reason"))
    lines += _halt_section(result.halts)
    lines += _funnel_section(result.funnel, trades, equity_curve)
    return "\n".join(lines) + "\n"


def write_report(result: BacktestResult, spy_bars: pd.DataFrame, path) -> str:
    text = build_report(result, spy_bars)
    path.write_text(text)
    return text


def _params_section(params: dict) -> list[str]:
    lines = ["## Params", ""]
    if not params:
        return lines + ["(none)", ""]
    for key, label in _PARAM_LABELS:
        if key in params:
            lines.append(f"- **{label}:** {params[key]}")
    lines.append("")
    return lines


def _headline_section(trades: list[Trade], equity_curve: pd.Series) -> list[str]:
    ts = metrics.trade_stats(trades)
    cs = metrics.curve_stats(trades, equity_curve)
    return [
        "## Headline", "",
        f"- Total return: {cs.total_return:+.1%}",
        f"- CAGR: {cs.cagr:+.1%}",
        f"- Max drawdown: {cs.max_drawdown:.1%} ({cs.max_drawdown_date or 'n/a'})",
        f"- Trades: {ts.count} ({cs.trades_per_day:.3f}/day over {cs.trading_days} trading days)",
        f"- Win rate: {ts.win_rate:.1%}",
        f"- Expectancy: {ts.expectancy_r:+.2f}R",
        f"- Profit factor: {ts.profit_factor:.2f}",
        f"- Time in market: {cs.time_in_market:.1%}",
        "",
    ]


def _benchmark_section(equity_curve: pd.Series, spy_bars: pd.DataFrame) -> list[str]:
    lines = ["## vs SPY buy-and-hold", ""]
    if len(equity_curve) < 2:
        return lines + ["(not enough data)", ""]
    strat_return = equity_curve.iloc[-1] / equity_curve.iloc[0] - 1
    spy_return = metrics.spy_buy_hold_return(spy_bars, equity_curve)
    lines += [
        f"- Strategy: {strat_return:+.1%}",
        f"- SPY buy-and-hold: {spy_return:+.1%}",
        f"- Edge: {strat_return - spy_return:+.1%}",
        "",
    ]
    return lines


def _breakdown_table(title: str, groups: dict[str, metrics.TradeStats]) -> list[str]:
    lines = [f"## {title}", ""]
    if not groups:
        return lines + ["(no trades)", ""]
    lines.append("| Key | Count | Win rate | Expectancy (R) | Profit factor | Avg hold (days) |")
    lines.append("|---|---|---|---|---|---|")
    for key, ts in sorted(groups.items()):
        lines.append(
            f"| {key} | {ts.count} | {ts.win_rate:.1%} | {ts.expectancy_r:+.2f} | "
            f"{ts.profit_factor:.2f} | {ts.avg_hold_days:.1f} |"
        )
    lines.append("")
    return lines


def _halt_section(halts: list[HaltEvent]) -> list[str]:
    lines = ["## Halt log", ""]
    if not halts:
        return lines + ["(no halts)", ""]
    lines.append("| Date | Kind | Equity | HWM | Outcome |")
    lines.append("|---|---|---|---|---|")
    for h in halts:
        outcome = "resumed" if h.resumed else "stopped (terminal)"
        lines.append(f"| {h.date} | {h.kind} | ${h.equity:,.2f} | ${h.hwm:,.2f} | {outcome} |")
    lines.append("")
    return lines


def _funnel_section(funnel: dict[str, int], trades: list[Trade],
                    equity_curve: pd.Series) -> list[str]:
    lines = ["## Signal funnel", ""]
    if not funnel:
        return lines + ["(no signals generated)", ""]
    for key in sorted(funnel):
        lines.append(f"- {key}: {funnel[key]}")
    lines.append("")

    cs = metrics.curve_stats(trades, equity_curve)
    lo, hi = PHASE1_TRADES_PER_DAY
    gate_ok = lo <= cs.trades_per_day <= hi
    lines.append(
        f"- Trades/day {cs.trades_per_day:.3f} vs Phase-1 gate {lo:.0f}-{hi:.0f}/day: "
        f"{'PASS' if gate_ok else 'FAIL'}"
    )
    dd_ok = abs(cs.max_drawdown) < PHASE1_MAX_DRAWDOWN
    lines.append(
        f"- Max drawdown {abs(cs.max_drawdown):.1%} vs Phase-1 gate <{PHASE1_MAX_DRAWDOWN:.0%}: "
        f"{'PASS' if dd_ok else 'FAIL'}"
    )
    lines.append("")
    return lines
