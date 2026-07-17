"""Market data ingestion with local CSV caching (step 0.3).

CSV over parquet on purpose: avoids the pyarrow dependency, which is heavy
on a Raspberry Pi. Daily bars for ~60 symbols is tiny data — CSV is fine.

Bars are fetched from Alpaca first; anything Alpaca can't serve (or if the
whole request errors — missing keys, network) falls back to yfinance, which
is an optional dependency (see requirements.txt). Both sources are normalised
to one schema so cached CSVs are indistinguishable downstream:

    index: timestamp (ascending)
    cols:  open, high, low, close, volume, trade_count, vwap

yfinance has no per-bar trade_count/vwap, so those come back as NaN from the
fallback path — acceptable for a backup source (signals key off OHLCV).
"""

from __future__ import annotations  # py3.9 compat

import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from bot import config

_data_client: StockHistoricalDataClient | None = None

# Canonical bar schema (matches Alpaca's .df columns). Fallback sources are
# reshaped to this before caching so every CSV on disk looks the same.
_COLUMNS = ["open", "high", "low", "close", "volume", "trade_count", "vwap"]


def client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        _data_client = StockHistoricalDataClient(
            config.ALPACA_KEY_ID, config.ALPACA_SECRET
        )
    return _data_client


def get_daily_bars(symbols: list[str], years: int = 5,
                   refresh: bool = False) -> dict[str, pd.DataFrame]:
    """symbol -> OHLCV DataFrame indexed by date, cached in data/bars/.

    Cached symbols are read from disk unless ``refresh`` is set. Everything
    else is fetched from Alpaca, with a per-symbol yfinance fallback for any
    Alpaca misses. Symbols neither source can serve are simply absent from the
    returned dict (a warning is printed).
    """
    out: dict[str, pd.DataFrame] = {}
    stale: list[str] = []
    for sym in symbols:
        path = config.BARS_DIR / f"{sym}.csv"
        if path.exists() and not refresh:
            out[sym] = pd.read_csv(path, index_col=0, parse_dates=True)
        else:
            stale.append(sym)

    if not stale:
        return out

    start = datetime.now(timezone.utc) - timedelta(days=365 * years)
    fetched = _fetch_alpaca(stale, start)
    missing = [s for s in stale if s not in fetched]
    if missing:
        fetched.update(_fetch_yfinance(missing, start))

    for sym, df in fetched.items():
        df = _normalize(df)
        df.to_csv(config.BARS_DIR / f"{sym}.csv")
        out[sym] = df

    unresolved = [s for s in stale if s not in fetched]
    if unresolved:
        print(f"WARNING: no bars for {unresolved} "
              f"(Alpaca and yfinance both came up empty)", file=sys.stderr)
    return out


def _fetch_alpaca(symbols: list[str], start: datetime) -> dict[str, pd.DataFrame]:
    """Batch one Alpaca request for all symbols. Returns {sym: raw df}.

    On any client error (missing keys, network, rate limit) returns {} so the
    caller falls back to yfinance for the whole batch rather than crashing.
    """
    try:
        req = StockBarsRequest(symbol_or_symbols=symbols,
                               timeframe=TimeFrame.Day, start=start)
        bars = client().get_stock_bars(req).df
    except Exception as e:  # noqa: BLE001 — any failure ⇒ try the fallback
        print(f"Alpaca fetch failed ({e}); falling back to yfinance.",
              file=sys.stderr)
        return {}

    out: dict[str, pd.DataFrame] = {}
    if bars.empty:
        return out
    have = set(bars.index.get_level_values(0))
    for sym in symbols:
        if sym in have:
            out[sym] = bars.xs(sym, level=0)
    return out


def _fetch_yfinance(symbols: list[str], start: datetime) -> dict[str, pd.DataFrame]:
    """Per-symbol yfinance fallback. Optional dep — degrades to {} if absent."""
    try:
        import yfinance as yf
    except ImportError:
        print("WARNING: yfinance not installed, so no fallback source. "
              "`pip install yfinance` to enable it.", file=sys.stderr)
        return {}

    out: dict[str, pd.DataFrame] = {}
    start_str = start.strftime("%Y-%m-%d")
    for sym in symbols:
        try:
            df = yf.Ticker(sym).history(start=start_str, interval="1d",
                                        auto_adjust=False)
        except Exception as e:  # noqa: BLE001 — skip this symbol, keep going
            print(f"  yfinance {sym} failed: {e}", file=sys.stderr)
            continue
        if not df.empty:
            out[sym] = df
    return out


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Reshape any source's bars to the canonical schema (see module docstring).

    Idempotent for Alpaca frames (already lower-case OHLCV); for yfinance it
    lower-cases columns, drops extras (Dividends/Stock Splits/Adj Close), and
    fills the missing trade_count/vwap columns with NaN.
    """
    df = df.rename(columns=str.lower)
    for col in _COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    df = df[_COLUMNS]
    df.index.name = "timestamp"
    return df.sort_index()


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
