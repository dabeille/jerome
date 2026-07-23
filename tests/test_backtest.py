"""Offline tests for the backtest engine (0.5.2): fills, exits, slippage,
and the check_data gate. No network/keys — synthetic bars built the same
way as tests/test_signals.py, engineered to trip the momentum breakout
scanner on a known day so the fill/exit mechanics can be pinned down exactly.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest import engine
from bot import data

WARMUP = 44  # bars before the breakout day; momentum needs MIN_BARS=40


def _flat(dates, price=50.0, volume=1_000_000) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame(
        {
            "open": [price] * n,
            "high": [price * 1.01] * n,
            "low": [price * 0.99] * n,
            "close": [price] * n,
            "volume": [volume] * n,
            "trade_count": [1000] * n,
            "vwap": [price] * n,
        },
        index=pd.Index(dates, name="timestamp"),
    )


def _momentum_setup(tail_rows: list[dict]) -> tuple[dict[str, pd.DataFrame], pd.DatetimeIndex]:
    """Flat 44-bar warmup (no breakout, no volume spike) followed by a
    breakout day (row 44: closes at 60, well above the 20d high, on 3x
    volume) and whatever ``tail_rows`` (fill day, exit day, ...) the caller
    supplies. SPY stays flat throughout — the calendar and a ~0% benchmark
    return.
    """
    dates = pd.bdate_range("2024-01-02", periods=WARMUP + 1 + len(tail_rows))
    spy = _flat(dates)

    warmup_rows = [dict(open=50.0, high=50.5, low=49.5, close=50.0, volume=1_000_000)] * WARMUP
    breakout_row = dict(open=58.0, high=60.5, low=57.5, close=60.0, volume=3_000_000)
    rows = warmup_rows + [breakout_row] + tail_rows

    mom = pd.DataFrame(
        {
            "open": [r["open"] for r in rows],
            "high": [r["high"] for r in rows],
            "low": [r["low"] for r in rows],
            "close": [r["close"] for r in rows],
            "volume": [r["volume"] for r in rows],
            "trade_count": [1000] * len(rows),
            "vwap": [r["close"] for r in rows],
        },
        index=pd.Index(dates, name="timestamp"),
    )
    return {"SPY": spy, "MOM": mom}, dates


# Breakout day: entry=60.0, stop=swing_low=49.5, target=entry+3*(entry-stop)=91.5
STOP = 49.5
TARGET = 91.5
FILL_DAY = dict(open=62.0, high=63.0, low=61.0, close=62.0, volume=1_000_000)


def _run(monkeypatch, bars, dates):
    monkeypatch.setattr(data, "get_daily_bars", lambda *a, **k: bars)
    start, end = dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d")
    return engine.run_backtest(start, end)


def _mom_trade(trades):
    mom_trades = [t for t in trades if t.symbol == "MOM"]
    assert len(mom_trades) == 1
    return mom_trades[0]


def test_target_hit_is_a_winning_trade(monkeypatch):
    exit_day = dict(open=90.0, high=95.0, low=85.0, close=93.0, volume=1_000_000)
    bars, dates = _momentum_setup([FILL_DAY, exit_day])

    trades, _ = _run(monkeypatch, bars, dates)

    trade = _mom_trade(trades)
    assert trade.exit_reason == "target"
    assert trade.pnl > 0


def test_stop_hit_is_a_losing_trade(monkeypatch):
    exit_day = dict(open=55.0, high=56.0, low=40.0, close=45.0, volume=1_000_000)
    bars, dates = _momentum_setup([FILL_DAY, exit_day])

    trades, _ = _run(monkeypatch, bars, dates)

    trade = _mom_trade(trades)
    assert trade.exit_reason == "stop"
    assert trade.pnl < 0


def test_entry_fills_next_day_open_not_signal_day_close(monkeypatch):
    exit_day = dict(open=90.0, high=95.0, low=85.0, close=93.0, volume=1_000_000)
    bars, dates = _momentum_setup([FILL_DAY, exit_day])

    trades, _ = _run(monkeypatch, bars, dates)

    trade = _mom_trade(trades)
    assert trade.entry == pytest.approx(62.0 * (1 + engine.SLIPPAGE))
    assert trade.entry != pytest.approx(60.0)  # not the breakout day's close


def test_slippage_applied_on_both_sides(monkeypatch):
    exit_day = dict(open=90.0, high=95.0, low=85.0, close=93.0, volume=1_000_000)
    bars, dates = _momentum_setup([FILL_DAY, exit_day])

    trades, _ = _run(monkeypatch, bars, dates)

    trade = _mom_trade(trades)
    assert trade.entry == pytest.approx(62.0 * (1 + engine.SLIPPAGE))
    assert trade.exit == pytest.approx(TARGET * (1 - engine.SLIPPAGE))


def test_same_day_stop_and_target_is_stop_first(monkeypatch):
    # Both legs are inside the day's range: low pierces the stop, high
    # clears the target. Daily bars can't tell us which happened first, so
    # the engine assumes the conservative outcome — stop.
    exit_day = dict(open=70.0, high=95.0, low=40.0, close=80.0, volume=1_000_000)
    bars, dates = _momentum_setup([FILL_DAY, exit_day])

    trades, _ = _run(monkeypatch, bars, dates)

    trade = _mom_trade(trades)
    assert trade.exit_reason == "stop"
    assert trade.exit == pytest.approx(STOP * (1 - engine.SLIPPAGE))


def test_gating_issue_aborts_run(monkeypatch):
    dates = pd.bdate_range("2024-01-02", periods=5)
    bad = _flat(dates)
    bad.loc[dates[2], "high"] = 1.0
    bad.loc[dates[2], "low"] = 5.0  # high < low -> ohlc_integrity (gating)

    monkeypatch.setattr(data, "get_daily_bars", lambda *a, **k: {"SPY": bad})

    with pytest.raises(RuntimeError):
        engine.run_backtest(dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d"))
