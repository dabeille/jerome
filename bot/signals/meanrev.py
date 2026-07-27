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
STOP_PCT = 0.03          # hard stop ~3% below entry


def rsi(series: pd.Series, period: int = 2) -> pd.Series:
    """Plain-pandas RSI — deliberately no TA-Lib (won't build cleanly on Pi)."""
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def scan(bars: dict[str, pd.DataFrame], rsi_oversold: float = RSI_OVERSOLD,
         stop_pct: float = STOP_PCT) -> list[Signal]:
    """bars: symbol -> daily OHLCV DataFrame (ascending dates).

    ``rsi_oversold`` (dip-buy threshold) and ``stop_pct`` (hard-stop distance)
    override the module defaults for backtest tuning; defaults match live.
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
