"""Backtest engine (step 0.5.2).

Design: simple daily-bar event loop (not vectorbt/numba — both are painful
on ARM, and at 1-3 trades/day a plain loop over 5y of dailies runs in
seconds even on a Pi).

- Walk-forward: tune on 2019-2023, validate untouched on 2024-mid-2026 (0.5.3)
- Costs: $0 commission, 0.05% slippage per side
- Reuses bot.risk.gate/position_size and the same Signal objects the live bot
  uses, so backtest and production share one code path
- Report: expectancy, win rate, max drawdown, trades/day, equity curve CSV (0.5.4)

Fill model: a signal generated off day t's bars is queued and filled at day
t+1's open — we can't fill on the close we used to generate the signal
without look-ahead. Bracket exits (stop/target) are checked against the same
day's high/low; if both are hit on one day we assume stop-first (the
conservative read, since daily bars can't tell us the intrabar order).

Starting equity mirrors the plan's initial funding target (docs/trading-bot-
plan.md: "$500-1,000" / the "$750" worked risk-per-trade example).
"""

from __future__ import annotations  # py3.9 compat

from dataclasses import dataclass

import pandas as pd

from bot import config, data, data_checks, risk
from bot.signals import Signal, meanrev, momentum

SLIPPAGE = 0.0005
STARTING_EQUITY = 1_000.0


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry: float
    exit_date: str = ""
    exit: float = 0.0
    qty: int = 0
    strategy: str = ""
    stop: float = 0.0
    target: float = 0.0
    exit_reason: str = ""  # "stop" | "target" | "eod" | "halt"

    @property
    def pnl(self) -> float:
        return (self.exit - self.entry) * self.qty


@dataclass
class Position:
    symbol: str
    entry_date: str
    entry: float
    qty: int
    stop: float
    target: float
    strategy: str


@dataclass
class _Pending:
    """A gated signal queued on day t, filled at day t+1's open."""
    signal: Signal
    qty: int


def run_backtest(start: str, end: str, strict: bool = False) -> tuple[list[Trade], pd.Series]:
    """Replay the live per-run ordering (signals -> risk gate -> fills) day by
    day over cached bars in ``[start, end]``. No network, no LLM veto layer.

    ``strict`` is forwarded to the check_data gate (see data_checks.should_gate):
    advisory issues like suspected_split normally only warn, but fail the run
    when set.

    Returns (closed trades, equity curve indexed by date).
    """
    bars = data.get_daily_bars(config.UNIVERSE, refresh=False)

    issues = data_checks.run_checks(bars)
    if data_checks.should_gate(issues, strict=strict):
        raise RuntimeError(
            "check_data gate failed:\n" + "\n".join(str(i) for i in issues)
        )

    if "SPY" not in bars:
        raise RuntimeError("SPY missing from cached bars — required as the "
                           "trading-day calendar and momentum benchmark")
    calendar = bars["SPY"].index
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    if calendar.tz is not None:  # cached bars are tz-aware (UTC); synthetic test bars aren't
        start_ts, end_ts = start_ts.tz_localize(calendar.tz), end_ts.tz_localize(calendar.tz)
    calendar = calendar[(calendar >= start_ts) & (calendar <= end_ts)]

    trades: list[Trade] = []
    positions: dict[str, Position] = {}
    pending: list[_Pending] = []
    cash = STARTING_EQUITY
    hwm = STARTING_EQUITY
    equity_dates: list[pd.Timestamp] = []
    equity_values: list[float] = []
    trades_today = 0

    for t in calendar:
        # "Start of day" equity = yesterday's closing mark (matches main.py,
        # which snapshots equity once at session start from the broker —
        # i.e. last night's close, before today's fills move the needle).
        day_start_equity = equity_values[-1] if equity_values else STARTING_EQUITY
        trades_today = 0
        halted = False

        # 1. Manage open positions / bracket exits against today's bar.
        for sym in list(positions):
            df = bars[sym]
            if t not in df.index:
                continue
            row = df.loc[t]
            pos = positions[sym]
            exit_price, reason = None, ""
            if row["low"] <= pos.stop:
                exit_price, reason = pos.stop, "stop"
            elif row["high"] >= pos.target:
                exit_price, reason = pos.target, "target"
            if exit_price is not None:
                fill = exit_price * (1 - SLIPPAGE)
                cash += pos.qty * fill
                trades.append(Trade(
                    symbol=sym, entry_date=pos.entry_date, entry=pos.entry,
                    exit_date=str(t.date()), exit=fill, qty=pos.qty,
                    strategy=pos.strategy, stop=pos.stop, target=pos.target,
                    exit_reason=reason,
                ))
                del positions[sym]

        # 2. Mark-to-market & equity.
        equity = cash + sum(
            p.qty * _close_on(bars[p.symbol], t, default=p.entry) for p in positions.values()
        )
        hwm = max(hwm, equity)

        # 3. Circuit breakers (inline, against the in-memory equity curve).
        if hwm > 0 and (equity - hwm) / hwm <= config.DRAWDOWN_HALT:
            halted = True
        elif day_start_equity > 0 and (equity - day_start_equity) / day_start_equity <= config.DAILY_LOSS_LIMIT:
            halted = True
        if halted:
            for sym in list(positions):
                df = bars[sym]
                if t not in df.index:
                    continue
                pos = positions[sym]
                fill = df.loc[t, "close"] * (1 - SLIPPAGE)
                cash += pos.qty * fill
                trades.append(Trade(
                    symbol=sym, entry_date=pos.entry_date, entry=pos.entry,
                    exit_date=str(t.date()), exit=fill, qty=pos.qty,
                    strategy=pos.strategy, stop=pos.stop, target=pos.target,
                    exit_reason="halt",
                ))
                del positions[sym]
            # Any symbol without a bar today (thin/gappy history) can't be
            # flattened — mark it at its last-known price instead of losing
            # it from the equity total.
            equity = cash + sum(
                p.qty * _close_on(bars[p.symbol], t, default=p.entry) for p in positions.values()
            )
            pending = []

        # 4. Fill pending entries queued from the prior day at today's open.
        # Anything that can't fill today (already held, no bar, insufficient
        # cash) is dropped rather than retried — fresh signals get generated
        # every day regardless (step 5-6).
        if not halted:
            for p in pending:
                sym = p.signal.symbol
                df = bars[sym]
                if sym in positions or t not in df.index:
                    continue
                fill = df.loc[t, "open"] * (1 + SLIPPAGE)
                cost = fill * p.qty
                if cost > cash:
                    continue
                cash -= cost
                positions[sym] = Position(
                    symbol=sym, entry_date=str(t.date()), entry=fill,
                    qty=p.qty, stop=p.signal.stop, target=p.signal.target,
                    strategy=p.signal.strategy,
                )
                trades_today += 1

        # 5-6. Generate signals off history up to and including today, then
        # gate them and queue approved entries for tomorrow's open.
        if not halted:
            sliced = {sym: df.loc[:t] for sym, df in bars.items()}
            signals: list[Signal] = momentum.scan(sliced) + meanrev.scan(sliced)
            approved = risk.gate(signals, equity, list(positions), trades_today)
            pending = [_Pending(sig, qty) for sig, qty in approved]

        equity_dates.append(t)
        equity_values.append(equity)

    # 7. Realize any still-open positions at the final close for clean stats.
    if len(calendar):
        last = calendar[-1]
        for sym, pos in positions.items():
            df = bars[sym]
            close = _close_on(df, last, default=pos.entry)
            fill = close * (1 - SLIPPAGE)
            trades.append(Trade(
                symbol=sym, entry_date=pos.entry_date, entry=pos.entry,
                exit_date=str(last.date()), exit=fill, qty=pos.qty,
                strategy=pos.strategy, stop=pos.stop, target=pos.target,
                exit_reason="eod",
            ))

    equity_curve = pd.Series(equity_values, index=pd.DatetimeIndex(equity_dates, name="timestamp"))
    return trades, equity_curve


def _close_on(df: pd.DataFrame, t: pd.Timestamp, default: float) -> float:
    """Today's close for mark-to-market, or ``default`` if the symbol has no
    bar today (thin/gappy history) so a single missing row can't crash a run."""
    if t in df.index:
        return float(df.loc[t, "close"])
    return default
