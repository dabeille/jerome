"""Momentum / breakout strategy (plan §4A). Step 0.4.1 — logic TODO.

Intended signals:
- 20-day-high breakout with relative volume > 2x 20-day average
- Relative strength vs SPY over 20 days in top decile of universe
- Entry at/near breakout level, stop below consolidation low (~1R),
  target 3R+ or trailing exit
"""

from __future__ import annotations  # py3.9 compat

import pandas as pd

from bot.signals import Signal


def scan(bars: dict[str, pd.DataFrame]) -> list[Signal]:
    """bars: symbol -> daily OHLCV DataFrame (ascending dates).

    TODO(0.4.1): implement breakout scan. Returns [] so the loop runs
    end-to-end before strategies exist.
    """
    return []
