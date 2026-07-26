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

from collections import defaultdict
from dataclasses import dataclass, field

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


@dataclass
class HaltEvent:
    date: str
    kind: str  # "drawdown" | "daily_loss"
    equity: float
    hwm: float
    resumed: bool  # False for a terminal drawdown halt (resume_after_days=None)


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: pd.Series
    funnel: dict[str, int]
    halts: list[HaltEvent]
    params: dict = field(default_factory=dict)


def run_backtest(start: str, end: str, strict: bool = False,
                 resume_after_days: int | None = None,
                 starting_equity: float = STARTING_EQUITY) -> BacktestResult:
    """Replay the live per-run ordering (signals -> risk gate -> fills) day by
    day over cached bars in ``[start, end]``. No network, no LLM veto layer.

    ``strict`` is forwarded to the check_data gate (see data_checks.should_gate):
    advisory issues like suspected_split normally only warn, but fail the run
    when set.

    A daily-loss halt flattens and skips only that trading day (plan §5:
    "stops until the next day"). A drawdown halt is terminal by default,
    faithful to live — the run ends the day it's flattened. Set
    ``resume_after_days=N`` to model the plan §5 human review: stay flat for
    N trading days, then rebase the high-water mark to that day's equity and
    keep going, mirroring what ``journal.mark_resume()`` does live.

    Returns a BacktestResult (trades, equity_curve, funnel, halts, params).
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
    cash = starting_equity
    hwm = starting_equity
    equity_dates: list[pd.Timestamp] = []
    equity_values: list[float] = []
    funnel: dict[str, int] = defaultdict(int)
    halts: list[HaltEvent] = []
    flat_days_remaining = 0  # counts down a resume_after_days wait

    for t in calendar:
        # "Start of day" equity = yesterday's closing mark (matches main.py,
        # which snapshots equity once at session start from the broker —
        # i.e. last night's close, before today's fills move the needle).
        day_start_equity = equity_values[-1] if equity_values else starting_equity
        trades_today = 0

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

        # 3. Still waiting out an earlier drawdown halt's resume_after_days
        # countdown — stay flat, no breach checks, no fills, no signals.
        if flat_days_remaining > 0:
            flat_days_remaining -= 1
            if flat_days_remaining == 0:
                hwm = equity  # rebase, mirrors journal.mark_resume() live
            equity_dates.append(t)
            equity_values.append(equity)
            continue

        # 4. Circuit breakers (inline, against the in-memory equity curve).
        # Drawdown takes priority over daily-loss, same as live risk.py.
        drawdown_breach = hwm > 0 and (equity - hwm) / hwm <= config.DRAWDOWN_HALT
        daily_loss_breach = (
            not drawdown_breach and day_start_equity > 0
            and (equity - day_start_equity) / day_start_equity <= config.DAILY_LOSS_LIMIT
        )
        if drawdown_breach or daily_loss_breach:
            funnel["fill_dropped_halt"] += len(pending)
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
            equity_dates.append(t)
            equity_values.append(equity)

            if drawdown_breach:
                resumed = resume_after_days is not None
                halts.append(HaltEvent(date=str(t.date()), kind="drawdown",
                                       equity=equity, hwm=hwm, resumed=resumed))
                if not resumed:
                    break  # terminal, faithful to live — no resume mechanism
                if resume_after_days == 0:
                    hwm = equity  # nothing to wait out, rebase immediately
                else:
                    flat_days_remaining = resume_after_days
            else:
                halts.append(HaltEvent(date=str(t.date()), kind="daily_loss",
                                       equity=equity, hwm=hwm, resumed=True))
            continue

        # 5. Fill pending entries queued from the prior day at today's open.
        # Anything that can't fill today (already held, no bar, insufficient
        # cash) is dropped rather than retried — fresh signals get generated
        # every day regardless (step 6-7).
        for p in pending:
            sym = p.signal.symbol
            df = bars[sym]
            if sym in positions:
                funnel["fill_already_held"] += 1
                continue
            if t not in df.index:
                funnel["fill_no_bar"] += 1
                continue
            fill = df.loc[t, "open"] * (1 + SLIPPAGE)
            cost = fill * p.qty
            if cost > cash:
                funnel["fill_insufficient_cash"] += 1
                continue
            cash -= cost
            positions[sym] = Position(
                symbol=sym, entry_date=str(t.date()), entry=fill,
                qty=p.qty, stop=p.signal.stop, target=p.signal.target,
                strategy=p.signal.strategy,
            )
            trades_today += 1
            funnel["filled"] += 1

        # 6-7. Generate signals off history up to and including today, then
        # gate them and queue approved entries for tomorrow's open.
        sliced = {sym: df.loc[:t] for sym, df in bars.items()}
        signals: list[Signal] = momentum.scan(sliced) + meanrev.scan(sliced)
        funnel["signals_generated"] += len(signals)
        approved = risk.gate(signals, equity, list(positions), trades_today,
                             reject_counts=funnel)
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
    params = dict(
        start=start, end=end, strict=strict,
        starting_equity=starting_equity, resume_after_days=resume_after_days,
        risk_per_trade=config.RISK_PER_TRADE, max_position_pct=config.MAX_POSITION_PCT,
        max_open_positions=config.MAX_OPEN_POSITIONS, max_per_sector=config.MAX_PER_SECTOR,
        daily_loss_limit=config.DAILY_LOSS_LIMIT, drawdown_halt=config.DRAWDOWN_HALT,
        max_trades_per_day=config.MAX_TRADES_PER_DAY,
        relvol_mult=momentum.RELVOL_MULT, rs_top_pct=momentum.RS_TOP_PCT,
        target_r=momentum.TARGET_R, rsi_oversold=meanrev.RSI_OVERSOLD,
        stop_pct=meanrev.STOP_PCT,
    )
    return BacktestResult(trades=trades, equity_curve=equity_curve,
                          funnel=dict(funnel), halts=halts, params=params)


def _close_on(df: pd.DataFrame, t: pd.Timestamp, default: float) -> float:
    """Today's close for mark-to-market, or ``default`` if the symbol has no
    bar today (thin/gappy history) so a single missing row can't crash a run."""
    if t in df.index:
        return float(df.loc[t, "close"])
    return default
