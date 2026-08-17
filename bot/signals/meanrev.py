"""Mean-reversion strategy on ETFs/mega-caps (plan §4B). Step 0.4.2.

- RSI(2) < 10 while close > 200-day SMA (only buy dips in uptrends)
- Exit on close above 5-day SMA, hard stop ~3% below entry
"""

from __future__ import annotations  # py3.9 compat

import pandas as pd

from bot.signals import Signal

SMA_TREND = 200          # long-term uptrend filter
SMA_EXIT = 5             # exit-target moving average
RSI_PERIOD = 2
RSI_OVERSOLD = 10        # buy dips this oversold or deeper
MIN_BARS = SMA_TREND

# Retuned 2026-08-16 from 3.0% (backtest.sweep --meanrev, 36 configs over the
# 2019-2023 training window). Tighter is better on every P&L measure, and the
# gain is not a measurement artifact — profit factor, which is pure P&L, rises
# with it too (1.37 -> 1.75 averaged across the grid).
#
# The mechanism: the 40%-of-equity position cap binds at both stop widths, so
# halving the stop halves the *actual* risk per trade (1.2% -> 0.6% of equity)
# rather than doubling the share count. Positions also turn over faster, which
# frees the open slots that dominated the live funnel.
STOP_PCT = 0.015

# Floor on the take-profit distance, as a multiple of risk (entry - stop).
#
# The plan §4B exit is "exit on close above the 5-day MA", but the live path
# can only place a *static* bracket target, so it freezes that MA at entry. In
# the week of 2026-08-10 that produced targets of +1.24% and +1.33% against a
# -3.0% stop — 0.41R and 0.44R winners. A ~65% win rate paying 0.4:1 is a coin
# flip that loses to costs, which is the shape the 0.5.3 validation measured at
# -0.91R. Flooring the target at TARGET_MIN_R x risk makes the geometry
# survivable without needing the dynamic exit the bracket cannot express.
#
# 2.0 chosen from the 2026-08-16 sweep, where the effect is monotonic across
# the whole grid: expectancy 0.245R -> 0.351R and total return +132% -> +177%
# as the floor goes 0 -> 2.0. Expect the win rate to *fall* (0.45 -> 0.36) —
# that is the trade being made, not a regression: fewer, larger winners.
TARGET_MIN_R = 2.0


def rsi(series: pd.Series, period: int = 2) -> pd.Series:
    """Plain-pandas RSI — deliberately no TA-Lib (won't build cleanly on Pi)."""
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def scan(bars: dict[str, pd.DataFrame], rsi_oversold: float = RSI_OVERSOLD,
         stop_pct: float = STOP_PCT,
         target_min_r: float = TARGET_MIN_R) -> list[Signal]:
    """bars: symbol -> daily OHLCV DataFrame (ascending dates).

    ``rsi_oversold`` (dip-buy threshold), ``stop_pct`` (hard-stop distance) and
    ``target_min_r`` (take-profit floor in R) override the module defaults for
    backtest tuning; defaults match live.
    """
    signals = []
    for sym, df in bars.items():
        if len(df) < MIN_BARS:
            continue

        close = pd.to_numeric(df["close"], errors="coerce")
        last_close = close.iloc[-1]
        if pd.isna(last_close):
            continue

        sma_trend = close.rolling(SMA_TREND).mean().iloc[-1]
        if pd.isna(sma_trend) or last_close <= sma_trend:
            continue  # not in an uptrend

        rsi2 = rsi(close, RSI_PERIOD).iloc[-1]
        if pd.isna(rsi2) or rsi2 >= rsi_oversold:
            continue  # not oversold enough

        sma_exit = close.rolling(SMA_EXIT).mean().iloc[-1]
        entry = float(last_close)
        stop = entry * (1 - stop_pct)
        target = float(sma_exit) if not pd.isna(sma_exit) and sma_exit > entry else entry * 1.03
        target = max(target, entry + target_min_r * (entry - stop))

        score = round(min(100.0, max(0.0, (rsi_oversold - rsi2) / rsi_oversold * 100.0)), 1)
        signals.append(Signal(
            symbol=sym,
            side="buy",
            score=score,
            entry=entry,
            stop=stop,
            target=target,
            strategy="meanrev",
            reasoning=(
                f"RSI(2) {rsi2:.1f} < {rsi_oversold} while close above "
                f"{SMA_TREND}d SMA (uptrend dip-buy)"
            ),
        ))

    signals.sort(key=lambda s: s.score, reverse=True)
    return signals
