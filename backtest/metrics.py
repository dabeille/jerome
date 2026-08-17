"""Backtest metrics (step 0.5.4): expectancy, drawdown, benchmark comparison.

Pure functions over ``(trades, equity_curve)`` as produced by
``backtest.engine.run_backtest``. Pandas only, no new dependencies. This
module changes no strategy or risk behaviour — it only measures.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from backtest.engine import Trade

TRADING_DAYS_PER_YEAR = 252


def r_multiple(trade: Trade) -> float:
    """The trade's realized risk multiple: P&L per share over the risk per
    share the position was *sized* on. 0.0 if that risk is not positive.

    The denominator is the signal's intended ``entry - stop`` (recorded on the
    trade at fill time), not the realized ``fill - stop``. That distinction is
    load-bearing: the fill can land anywhere at or below the limit, and one
    that lands just above its own stop leaves a near-zero realized denominator
    — a single such trade scores hundreds of R and silently poisons
    ``expectancy_r``, which is the metric parameter sweeps rank on. It is also
    the economically right denominator, because the intended risk is what the
    position was sized against and what the 3%-of-equity risk budget bought.

    Falls back to ``entry - stop`` for trades with no recorded intended risk,
    and guards on ``> 0`` so a degenerate denominator can never leak through."""
    risk = trade.risk_per_share or (trade.entry - trade.stop)
    return (trade.exit - trade.entry) / risk if risk > 0 else 0.0


def hold_days(trade: Trade) -> int:
    return (pd.Timestamp(trade.exit_date) - pd.Timestamp(trade.entry_date)).days


@dataclass
class TradeStats:
    count: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy_r: float = 0.0
    profit_factor: float = 0.0
    avg_hold_days: float = 0.0


def trade_stats(trades: list[Trade]) -> TradeStats:
    """Count, win rate, avg win/loss, expectancy in R, profit factor, and
    average hold time. Zeros (not a crash) on an empty trade list."""
    if not trades:
        return TradeStats()
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    r_values = [r_multiple(t) for t in trades]
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = float("inf") if gross_win > 0 else 0.0
    return TradeStats(
        count=len(trades),
        win_rate=len(wins) / len(trades),
        avg_win=(gross_win / len(wins)) if wins else 0.0,
        avg_loss=(gross_loss / len(losses)) if losses else 0.0,
        expectancy_r=sum(r_values) / len(r_values),
        profit_factor=profit_factor,
        avg_hold_days=sum(hold_days(t) for t in trades) / len(trades),
    )


@dataclass
class CurveStats:
    total_return: float = 0.0
    cagr: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_date: str = ""
    trading_days: int = 0
    trades_per_day: float = 0.0
    time_in_market: float = 0.0


def _time_in_market(trades: list[Trade], equity_curve: pd.Series) -> float:
    """Fraction of trading days with at least one position open, inferred
    from each trade's [entry_date, exit_date] span against the curve index."""
    if not len(equity_curve):
        return 0.0
    idx = equity_curve.index
    in_market = pd.Series(False, index=idx)
    for t in trades:
        entry = pd.Timestamp(t.entry_date)
        exit_ = pd.Timestamp(t.exit_date) if t.exit_date else entry
        if idx.tz is not None:
            entry, exit_ = entry.tz_localize(idx.tz), exit_.tz_localize(idx.tz)
        in_market |= (idx >= entry) & (idx <= exit_)
    return float(in_market.mean())


def curve_stats(trades: list[Trade], equity_curve: pd.Series) -> CurveStats:
    """Total return, CAGR, max drawdown (+ its date), trading days,
    trades/day, and time-in-market over the equity curve."""
    trading_days = len(equity_curve)
    if trading_days < 2:
        return CurveStats(trading_days=trading_days)
    total_return = equity_curve.iloc[-1] / equity_curve.iloc[0] - 1
    years = trading_days / TRADING_DAYS_PER_YEAR
    cagr = (equity_curve.iloc[-1] / equity_curve.iloc[0]) ** (1 / years) - 1
    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    max_dd = float(drawdown.min())
    max_dd_date = str(drawdown.idxmin().date())
    return CurveStats(
        total_return=float(total_return),
        cagr=float(cagr),
        max_drawdown=max_dd,
        max_drawdown_date=max_dd_date,
        trading_days=trading_days,
        trades_per_day=len(trades) / trading_days,
        time_in_market=_time_in_market(trades, equity_curve),
    )


def breakdown_by(trades: list[Trade], key: str) -> dict[str, TradeStats]:
    """Per-value trade stats, grouped by ``key`` ('strategy' or
    'exit_reason'). Empty dict if there are no trades."""
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        groups.setdefault(getattr(t, key), []).append(t)
    return {k: trade_stats(v) for k, v in groups.items()}


def spy_buy_hold_return(spy_bars: pd.DataFrame, equity_curve: pd.Series) -> float:
    """Total return of buying SPY at the equity curve's first date and
    holding through its last — free benchmark, SPY bars are already loaded
    as the trading-day calendar."""
    if len(equity_curve) < 2:
        return 0.0
    idx = equity_curve.index
    start_close = float(spy_bars.loc[idx[0], "close"])
    end_close = float(spy_bars.loc[idx[-1], "close"])
    return end_close / start_close - 1
