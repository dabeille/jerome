"""Offline tests for bot.data_checks (step 0.3.4).

Pure logic, no network/keys: synthetic OHLCV frames, each crafted to trip one
Issue.kind. SPY is the benchmark calendar throughout (data_checks uses the
benchmark's own trading days as ground truth for which days the market was open).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from bot import data_checks

# 10 business days; SPY spans all of them, symbols are carved out of this.
BENCH_DATES = pd.bdate_range("2024-01-02", periods=10)


def _bars(dates, closes=None) -> pd.DataFrame:
    """A structurally valid bar frame (high≥max(open,close), low≤min, etc.)."""
    dates = pd.DatetimeIndex(dates)
    if closes is None:
        closes = [100.0] * len(dates)
    closes = [float(c) for c in closes]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000] * len(dates),
            "trade_count": [1000] * len(dates),
            "vwap": closes,
        },
        index=pd.Index(dates, name="timestamp"),
    )


def _kinds(issues, symbol):
    return {i.kind for i in issues if i.symbol == symbol}


def test_clean_data_no_issues():
    bars = {"SPY": _bars(BENCH_DATES), "AAA": _bars(BENCH_DATES)}
    assert data_checks.run_checks(bars) == []


def test_missing_interior_day_flagged():
    sym = _bars(BENCH_DATES.delete(4))  # drop one day inside the range
    issues = data_checks.run_checks({"SPY": _bars(BENCH_DATES), "AAA": sym})
    assert "missing_days" in _kinds(issues, "AAA")


def test_later_ipo_not_flagged_for_pre_listing_gap():
    # Symbol starts at day 6: the missing days 1-5 predate its first bar and
    # must NOT be reported (the [first,last] bounding in _check_missing).
    sym = _bars(BENCH_DATES[5:])
    issues = data_checks.run_checks({"SPY": _bars(BENCH_DATES), "AAA": sym})
    assert not [i for i in issues if i.symbol == "AAA"]


def test_stale_last_bar_flagged():
    # Ends at day 6; benchmark runs to day 10 → 4 trading days behind (> STALE_MAX_LAG=3).
    sym = _bars(BENCH_DATES[:6])
    issues = data_checks.run_checks({"SPY": _bars(BENCH_DATES), "AAA": sym})
    assert "stale" in _kinds(issues, "AAA")


def test_stale_within_tolerance_not_flagged():
    sym = _bars(BENCH_DATES[:8])  # 2 behind, under the 3-day tolerance
    issues = data_checks.run_checks({"SPY": _bars(BENCH_DATES), "AAA": sym})
    assert "stale" not in _kinds(issues, "AAA")


def test_suspected_split_flagged():
    closes = [100.0] * 9 + [200.0]  # +100% close-to-close (> SPLIT_MOVE_THRESHOLD=0.5)
    issues = data_checks.run_checks(
        {"SPY": _bars(BENCH_DATES), "AAA": _bars(BENCH_DATES, closes)}
    )
    assert "suspected_split" in _kinds(issues, "AAA")


def test_ohlc_integrity_high_below_low():
    sym = _bars(BENCH_DATES)
    sym.iloc[3, sym.columns.get_loc("high")] = 1.0  # high below the day's low
    issues = data_checks.run_checks({"SPY": _bars(BENCH_DATES), "AAA": sym})
    assert "ohlc_integrity" in _kinds(issues, "AAA")


def test_ohlc_integrity_negative_volume():
    sym = _bars(BENCH_DATES)
    sym.iloc[2, sym.columns.get_loc("volume")] = -5
    issues = data_checks.run_checks({"SPY": _bars(BENCH_DATES), "AAA": sym})
    assert "ohlc_integrity" in _kinds(issues, "AAA")


def test_ohlc_integrity_nan_in_ohlcv():
    sym = _bars(BENCH_DATES)
    sym.iloc[1, sym.columns.get_loc("close")] = np.nan
    issues = data_checks.run_checks({"SPY": _bars(BENCH_DATES), "AAA": sym})
    assert "ohlc_integrity" in _kinds(issues, "AAA")


def test_missing_benchmark_reported_and_skips_calendar_checks():
    # No SPY in the dataset: the missing benchmark is reported, calendar-based
    # checks are skipped, but per-symbol OHLC/split checks still run.
    issues = data_checks.run_checks({"AAA": _bars(BENCH_DATES)}, benchmark="SPY")
    assert any(
        i.kind == "missing_days" and "not in dataset" in i.detail for i in issues
    )
    assert "stale" not in _kinds(issues, "AAA")


def test_suspected_split_is_advisory_other_kinds_are_gating():
    closes = [100.0] * 9 + [200.0]  # legit-or-not, this is advisory not gating
    sym = _bars(BENCH_DATES, closes)
    sym.iloc[2, sym.columns.get_loc("volume")] = -5  # also trip a gating issue
    issues = data_checks.run_checks({"SPY": _bars(BENCH_DATES), "AAA": sym})

    split_issues = [i for i in issues if i.kind == "suspected_split"]
    other_issues = [i for i in issues if i.kind != "suspected_split"]
    assert split_issues and all(i.severity == "advisory" for i in split_issues)
    assert other_issues and all(i.severity == "gating" for i in other_issues)


def test_should_gate_on_gating_issue():
    issues = [data_checks.Issue("AAA", "ohlc_integrity", "bad", severity="gating")]
    assert data_checks.should_gate(issues) is True
    assert data_checks.should_gate(issues, strict=True) is True


def test_should_gate_advisory_only_default_false_strict_true():
    issues = [data_checks.Issue("AAA", "suspected_split", "big move", severity="advisory")]
    assert data_checks.should_gate(issues) is False
    assert data_checks.should_gate(issues, strict=True) is True


def test_should_gate_empty_issues_false():
    assert data_checks.should_gate([]) is False
    assert data_checks.should_gate([], strict=True) is False
