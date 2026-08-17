"""Offline tests for the momentum (0.4.1) and mean-reversion (0.4.2) scanners.

Pure logic, no network/keys: synthetic OHLCV frames shaped to trip (or not
trip) each strategy's entry conditions. Follows the same pattern as
test_data_checks.py.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bot.signals import meanrev, momentum


def _bars(dates, closes=None, volumes=None) -> pd.DataFrame:
    """A structurally valid bar frame (high>=close>=low, etc.)."""
    dates = pd.DatetimeIndex(dates)
    n = len(dates)
    if closes is None:
        closes = [100.0] * n
    closes = [float(c) for c in closes]
    if volumes is None:
        volumes = [1_000_000] * n
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": volumes,
            "trade_count": [1000] * n,
            "vwap": closes,
        },
        index=pd.Index(dates, name="timestamp"),
    )


# ---------------------------------------------------------------------------
# Momentum (0.4.1)
# ---------------------------------------------------------------------------

DATES45 = pd.bdate_range("2024-01-02", periods=45)


def test_momentum_breakout_signal():
    spy = _bars(DATES45)  # flat benchmark -> ~0% bench return
    closes = [50.0] * 44 + [60.0]          # breaks above the 20d high on the last day
    volumes = [1_000_000] * 44 + [3_000_000]  # 3x relative volume
    mom = _bars(DATES45, closes, volumes)

    signals = momentum.scan({"SPY": spy, "MOM": mom})

    assert len(signals) == 1
    sig = signals[0]
    assert sig.symbol == "MOM"
    assert sig.side == "buy"
    assert sig.strategy == "momentum"
    assert sig.stop < sig.entry < sig.target


def test_momentum_entry_is_a_marketable_limit_above_the_breakout_close():
    """Entry priced *at* the breakout close only filled when price traded back
    down through it — so the breakout strategy systematically bought the
    breakouts that failed and missed the ones that ran (week of 2026-08-10:
    1 fill in 3, and that fill had gapped down 2.67%)."""
    spy = _bars(DATES45)
    closes = [50.0] * 44 + [60.0]
    volumes = [1_000_000] * 44 + [3_000_000]
    mom = _bars(DATES45, closes, volumes)

    sig = momentum.scan({"SPY": spy, "MOM": mom})[0]

    assert sig.entry == pytest.approx(60.0 * (1 + momentum.ENTRY_BUFFER))
    assert sig.entry > 60.0


def test_momentum_entry_buffer_is_tunable():
    spy = _bars(DATES45)
    closes = [50.0] * 44 + [60.0]
    volumes = [1_000_000] * 44 + [3_000_000]
    mom = _bars(DATES45, closes, volumes)

    sig = momentum.scan({"SPY": spy, "MOM": mom}, entry_buffer=0.0)[0]

    assert sig.entry == pytest.approx(60.0)


def test_momentum_no_breakout_no_signal():
    spy = _bars(DATES45)
    flat = _bars(DATES45)  # no breakout, no volume spike
    signals = momentum.scan({"SPY": spy, "FLAT": flat})
    assert signals == []


def test_momentum_too_short_history_no_raise():
    short_dates = pd.bdate_range("2024-01-02", periods=10)
    spy = _bars(short_dates)
    other = _bars(short_dates)
    assert momentum.scan({"SPY": spy, "AAA": other}) == []


# ---------------------------------------------------------------------------
# Mean-reversion (0.4.2)
# ---------------------------------------------------------------------------

DATES210 = pd.bdate_range("2024-01-02", periods=210)


def test_meanrev_oversold_dip_in_uptrend_signal():
    n = len(DATES210)
    # A steady uptrend keeps the 200-day SMA well below the recent price, then
    # a sharp two-day pullback pushes RSI(2) oversold while price stays above
    # its SMA200 -> a qualifying dip-buy in an uptrend.
    ramp = np.linspace(50.0, 140.0, n - 2).tolist()
    closes = ramp + [ramp[-1] * 0.93, ramp[-1] * 0.93 * 0.93]
    etf = _bars(DATES210, closes)

    signals = meanrev.scan({"ETF": etf})

    assert len(signals) == 1
    sig = signals[0]
    assert sig.symbol == "ETF"
    assert sig.side == "buy"
    assert sig.strategy == "meanrev"
    assert sig.stop < sig.entry < sig.target


def test_meanrev_dip_below_sma200_no_signal():
    n = len(DATES210)
    # Same sharp two-day dip shape, but flat (non-trending) history means the
    # dip pulls price below its own 200-day SMA -> uptrend filter rejects it.
    last = 140.0
    closes = [last] * (n - 2) + [last * 0.93, last * 0.93 * 0.93]
    etf = _bars(DATES210, closes)

    signals = meanrev.scan({"ETF": etf})
    assert signals == []


def test_meanrev_too_short_history_no_raise():
    short_dates = pd.bdate_range("2024-01-02", periods=50)
    etf = _bars(short_dates)
    assert meanrev.scan({"ETF": etf}) == []
