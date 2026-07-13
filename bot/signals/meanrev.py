"""Mean-reversion strategy on ETFs/mega-caps (plan §4B). Step 0.4.2 — logic TODO.

Intended signals:
- RSI(2) < 10 while close > 200-day SMA (only buy dips in uptrends)
- Exit on close above 5-day SMA, hard stop ~3% below entry
"""

import pandas as pd

from bot.signals import Signal


def rsi(series: pd.Series, period: int = 2) -> pd.Series:
    """Plain-pandas RSI — deliberately no TA-Lib (won't build cleanly on Pi)."""
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def scan(bars: dict[str, pd.DataFrame]) -> list[Signal]:
    """TODO(0.4.2): implement RSI(2) dip-buy scan."""
    return []
