"""Market data ingestion with local CSV caching (step 0.3).

CSV over parquet on purpose: avoids the pyarrow dependency, which is heavy
on a Raspberry Pi. Daily bars for ~60 symbols is tiny data — CSV is fine.
"""

from __future__ import annotations  # py3.9 compat

from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from bot import config

_data_client: StockHistoricalDataClient | None = None


def client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        _data_client = StockHistoricalDataClient(
            config.ALPACA_KEY_ID, config.ALPACA_SECRET
        )
    return _data_client


def get_daily_bars(symbols: list[str], years: int = 5,
                   refresh: bool = False) -> dict[str, pd.DataFrame]:
    """symbol -> OHLCV DataFrame indexed by date, cached in data/bars/."""
    out: dict[str, pd.DataFrame] = {}
    stale: list[str] = []
    for sym in symbols:
        path = config.BARS_DIR / f"{sym}.csv"
        if path.exists() and not refresh:
            out[sym] = pd.read_csv(path, index_col=0, parse_dates=True)
        else:
            stale.append(sym)

    if stale:
        start = datetime.now(timezone.utc) - timedelta(days=365 * years)
        req = StockBarsRequest(symbol_or_symbols=stale,
                               timeframe=TimeFrame.Day, start=start)
        bars = client().get_stock_bars(req).df
        for sym in stale:
            if sym in bars.index.get_level_values(0):
                df = bars.xs(sym, level=0)
                df.to_csv(config.BARS_DIR / f"{sym}.csv")
                out[sym] = df
    return out


def get_headlines(symbols: list[str]) -> dict[str, list[str]]:
    """Recent headlines per symbol for the LLM analyst.

    TODO(0.3.3): Alpaca News API primary, Finnhub /company-news fallback
    (free tier: 60 calls/min — batch symbols, cache for the day).
    """
    return {s: [] for s in symbols}


def earnings_within(symbols: list[str], days: int = 5) -> set[str]:
    """Symbols reporting earnings within `days` — feeds the earnings veto.

    TODO(0.3.3): Finnhub /calendar/earnings (free tier). Cache daily.
    """
    return set()
