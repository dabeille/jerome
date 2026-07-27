"""Offline tests for backtest metrics (0.5.4): expectancy, win rate, profit
factor, max drawdown, against hand-built trade lists and curves with
arithmetic answers. No network/keys.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest import metrics
from backtest.engine import Trade

# Four trades, hand-picked so every stat has a clean closed-form answer:
#   A: entry=100 stop=95 exit=110 qty=10 -> pnl=+100, risk=5,  r=+2.0,  1/1-3 hold=2d
#   B: entry=100 stop=95 exit=95  qty=10 -> pnl=-50,  risk=5,  r=-1.0,  hold=1d
#   C: entry=50  stop=48 exit=55  qty=20 -> pnl=+100, risk=2,  r=+2.5,  hold=4d
#   D: entry=50  stop=48 exit=48  qty=20 -> pnl=-40,  risk=2,  r=-1.0,  hold=1d
TRADE_A = Trade(symbol="A", entry_date="2024-01-01", entry=100.0,
                exit_date="2024-01-03", exit=110.0, qty=10,
                strategy="momentum", stop=95.0, target=115.0, exit_reason="target")
TRADE_B = Trade(symbol="B", entry_date="2024-01-01", entry=100.0,
                exit_date="2024-01-02", exit=95.0, qty=10,
                strategy="momentum", stop=95.0, target=115.0, exit_reason="stop")
TRADE_C = Trade(symbol="C", entry_date="2024-01-01", entry=50.0,
                exit_date="2024-01-05", exit=55.0, qty=20,
                strategy="meanrev", stop=48.0, target=60.0, exit_reason="target")
TRADE_D = Trade(symbol="D", entry_date="2024-01-01", entry=50.0,
                exit_date="2024-01-02", exit=48.0, qty=20,
                strategy="meanrev", stop=48.0, target=60.0, exit_reason="stop")
TRADES = [TRADE_A, TRADE_B, TRADE_C, TRADE_D]


# ---------------------------------------------------------------------------
# r_multiple / hold_days
# ---------------------------------------------------------------------------


def test_r_multiple():
    assert metrics.r_multiple(TRADE_A) == pytest.approx(2.0)
    assert metrics.r_multiple(TRADE_B) == pytest.approx(-1.0)
    assert metrics.r_multiple(TRADE_C) == pytest.approx(2.5)


def test_r_multiple_zero_risk_is_zero():
    flat = Trade(symbol="Z", entry_date="2024-01-01", entry=100.0,
                exit_date="2024-01-02", exit=105.0, stop=100.0)
    assert metrics.r_multiple(flat) == 0.0


def test_hold_days():
    assert metrics.hold_days(TRADE_A) == 2
    assert metrics.hold_days(TRADE_B) == 1
    assert metrics.hold_days(TRADE_C) == 4


# ---------------------------------------------------------------------------
# trade_stats
# ---------------------------------------------------------------------------


def test_trade_stats_basic():
    ts = metrics.trade_stats(TRADES)
    assert ts.count == 4
    assert ts.win_rate == pytest.approx(0.5)
    assert ts.avg_win == pytest.approx(100.0)   # (100+100)/2
    assert ts.avg_loss == pytest.approx(45.0)   # (50+40)/2
    assert ts.expectancy_r == pytest.approx(0.625)   # (2.0-1.0+2.5-1.0)/4
    assert ts.profit_factor == pytest.approx(200.0 / 90.0)
    assert ts.avg_hold_days == pytest.approx(2.0)   # (2+1+4+1)/4


def test_trade_stats_empty():
    ts = metrics.trade_stats([])
    assert ts == metrics.TradeStats()


def test_trade_stats_profit_factor_no_losses():
    ts = metrics.trade_stats([TRADE_A, TRADE_C])
    assert ts.profit_factor == float("inf")


def test_trade_stats_profit_factor_no_wins():
    ts = metrics.trade_stats([TRADE_B, TRADE_D])
    assert ts.profit_factor == 0.0


# ---------------------------------------------------------------------------
# breakdown_by
# ---------------------------------------------------------------------------


def test_breakdown_by_strategy():
    groups = metrics.breakdown_by(TRADES, "strategy")
    assert set(groups) == {"momentum", "meanrev"}
    assert groups["momentum"].count == 2
    assert groups["momentum"].expectancy_r == pytest.approx(0.5)   # (2.0-1.0)/2
    assert groups["meanrev"].expectancy_r == pytest.approx(0.75)   # (2.5-1.0)/2


def test_breakdown_by_exit_reason():
    groups = metrics.breakdown_by(TRADES, "exit_reason")
    assert groups["target"].count == 2
    assert groups["target"].win_rate == pytest.approx(1.0)
    assert groups["stop"].win_rate == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# curve_stats
# ---------------------------------------------------------------------------


def _curve(values, start="2024-01-02"):
    dates = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=dates)


def test_curve_stats_total_return_and_drawdown():
    curve = _curve([1000, 1100, 1050, 900, 950])
    cs = metrics.curve_stats([], curve)
    assert cs.total_return == pytest.approx(-0.05)   # 950/1000 - 1
    assert cs.trading_days == 5
    # running max after day1 is 1100; day3 (900) is the trough -> -0.181818...
    assert cs.max_drawdown == pytest.approx((900 - 1100) / 1100)
    assert cs.max_drawdown_date == str(curve.index[3].date())
    expected_cagr = (950 / 1000) ** (252 / 5) - 1
    assert cs.cagr == pytest.approx(expected_cagr)


def test_curve_stats_trades_per_day():
    curve = _curve([1000, 1010, 1020, 1030, 1040])
    cs = metrics.curve_stats(TRADES, curve)   # 4 trades / 5 days
    assert cs.trades_per_day == pytest.approx(0.8)


def test_curve_stats_too_short_returns_zeros():
    cs = metrics.curve_stats([], _curve([1000]))
    assert cs == metrics.CurveStats(trading_days=1)


def test_time_in_market():
    curve = _curve([1000, 1010, 1020, 1030, 1040])   # 5 bdays: 01-02..01-05, 01-08
    d = curve.index
    trades = [
        Trade(symbol="A", entry_date=str(d[0].date()), entry=100.0,
             exit_date=str(d[2].date()), exit=105.0, stop=95.0),   # covers d0,d1,d2
        Trade(symbol="B", entry_date=str(d[3].date()), entry=50.0,
             exit_date=str(d[3].date()), exit=52.0, stop=48.0),    # covers d3 only
    ]
    cs = metrics.curve_stats(trades, curve)
    assert cs.time_in_market == pytest.approx(4 / 5)   # d0-d3 covered, d4 is not


# ---------------------------------------------------------------------------
# spy_buy_hold_return
# ---------------------------------------------------------------------------


def test_spy_buy_hold_return():
    curve = _curve([1000, 1010, 1020])
    spy_bars = pd.DataFrame({"close": [400.0, 410.0, 420.0]}, index=curve.index)
    ret = metrics.spy_buy_hold_return(spy_bars, curve)
    assert ret == pytest.approx(420.0 / 400.0 - 1)


def test_spy_buy_hold_return_too_short_is_zero():
    curve = _curve([1000])
    spy_bars = pd.DataFrame({"close": [400.0]}, index=curve.index)
    assert metrics.spy_buy_hold_return(spy_bars, curve) == 0.0


# ---------------------------------------------------------------------------
# report: restated Phase-1 frequency gate (P4)
# ---------------------------------------------------------------------------


def test_funnel_section_renders_restated_gate_legs():
    from backtest import report

    curve = _curve([1000, 1010, 1020, 1030, 1040])   # 5 trading days
    funnel = {"signals_generated": 10, "filled": 4}
    lines = report._funnel_section(funnel, TRADES, curve)   # 4 trades / 5 days
    text = "\n".join(lines)
    # signals/day = 10/5 = 2.0 >= 1 -> PASS
    assert "Gated signals/day 2.000 vs Phase-1 gate >=1/day: PASS" in text
    # fills/week = 0.8 * 5 = 4.0 >= 2 -> PASS
    assert "Fills/week 4.00 vs Phase-1 gate >=2/week: PASS" in text
    assert "Max drawdown" in text


def test_funnel_section_gate_legs_fail_when_sparse():
    from backtest import report

    curve = _curve([1000] * 9 + [1010])   # 10 trading days, 1 trade
    funnel = {"signals_generated": 3, "filled": 1}
    lines = report._funnel_section(funnel, [TRADE_A], curve)
    text = "\n".join(lines)
    assert "Gated signals/day 0.300 vs Phase-1 gate >=1/day: FAIL" in text
    assert "Fills/week 0.50 vs Phase-1 gate >=2/week: FAIL" in text
