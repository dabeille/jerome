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

import json
import sys
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from bot import config

_data_client: StockHistoricalDataClient | None = None
_news_client_: NewsClient | None = None

FINNHUB_BASE = "https://finnhub.io/api/v1"
_HTTP_TIMEOUT = 15

# News tunables.
_NEWS_LOOKBACK_DAYS = 2   # how far back to pull headlines
_MAX_HEADLINES = 10       # per symbol, most-recent first
# Earnings: fetch a wide-ish forward window once/day; earnings_within() filters
# to the caller's tighter horizon out of the cached calendar.
_EARNINGS_HORIZON_DAYS = 21

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


def news_client() -> NewsClient:
    global _news_client_
    if _news_client_ is None:
        _news_client_ = NewsClient(config.ALPACA_KEY_ID, config.ALPACA_SECRET)
    return _news_client_


def get_headlines(symbols: list[str]) -> dict[str, list[str]]:
    """symbol -> recent headline strings, for the LLM analyst.

    Alpaca News (Benzinga-sourced) is primary — one batched request covers all
    symbols. Any symbol Alpaca returns nothing for falls back to Finnhub
    /company-news (per-symbol, free tier 60 calls/min). Results are cached to
    data/news/<date>.json so the morning/midday/close runs share one fetch.
    """
    if not symbols:
        return {}
    cache_path = config.NEWS_DIR / f"{date.today().isoformat()}.json"
    cached: dict[str, list[str]] = _load_cache(cache_path) or {}

    missing = [s for s in symbols if s not in cached]
    if missing:
        fetched = _fetch_alpaca_news(missing)
        for sym in missing:
            if not fetched.get(sym):            # Alpaca had nothing → Finnhub
                fetched[sym] = _fetch_finnhub_news(sym)
            cached[sym] = fetched.get(sym, [])  # cache empties too (no re-hit)
        _save_cache(cache_path, cached)

    return {s: cached.get(s, []) for s in symbols}


def _fetch_alpaca_news(symbols: list[str]) -> dict[str, list[str]]:
    """One batched Alpaca News request. Returns {sym: headlines} for symbols
    that had any coverage; symbols with none are simply absent (⇒ Finnhub)."""
    start = datetime.now(timezone.utc) - timedelta(days=_NEWS_LOOKBACK_DAYS)
    try:
        req = NewsRequest(symbols=",".join(symbols), start=start, sort="desc",
                          exclude_contentless=True, include_content=False,
                          limit=50)
        res = news_client().get_news(req)
    except Exception as e:  # noqa: BLE001 — any failure ⇒ Finnhub fallback
        print(f"Alpaca news fetch failed ({e}); falling back to Finnhub.",
              file=sys.stderr)
        return {}

    # NewsSet.data is a flat {"news": [...]} list; each article carries its own
    # .symbols, so we fan each headline out to the requested tickers it tags.
    articles = res.data.get("news", []) if isinstance(res.data, dict) else res.data
    want = set(symbols)
    out: dict[str, list[str]] = {}
    for art in articles:
        for sym in art.symbols:
            if sym in want and len(out.setdefault(sym, [])) < _MAX_HEADLINES:
                out[sym].append(art.headline)
    return out


def _fetch_finnhub_news(symbol: str) -> list[str]:
    """Finnhub /company-news fallback for a single symbol."""
    if not config.FINNHUB_KEY:
        return []
    today = date.today()
    try:
        r = requests.get(
            f"{FINNHUB_BASE}/company-news",
            params={"symbol": symbol,
                    "from": (today - timedelta(days=_NEWS_LOOKBACK_DAYS)).isoformat(),
                    "to": today.isoformat(),
                    "token": config.FINNHUB_KEY},
            timeout=_HTTP_TIMEOUT,
        )
        r.raise_for_status()
        items = r.json()
    except Exception as e:  # noqa: BLE001 — best-effort advisory data
        print(f"  Finnhub news {symbol} failed: {e}", file=sys.stderr)
        return []
    return [it["headline"] for it in items[:_MAX_HEADLINES] if it.get("headline")]


def earnings_within(symbols: list[str], days: int = 5) -> set[str]:
    """Symbols reporting earnings within `days` — feeds the earnings veto.

    Sources the day's full earnings calendar from Finnhub (cached), then
    filters to the requested symbols whose next report lands in [today, today+days].

    Best-effort: on a Finnhub outage this returns an empty set (fails *open*, so
    trading isn't halted by a data hiccup) and prints a loud warning — the veto
    simply can't fire that run. Callers relying on it for safety should treat a
    warning as a reason to be cautious.
    """
    if not symbols:
        return set()
    calendar = _earnings_calendar()  # {symbol: next earnings date (ISO)}
    horizon = date.today() + timedelta(days=days)
    soon: set[str] = set()
    for sym in symbols:
        iso = calendar.get(sym)
        if iso and date.fromisoformat(iso) <= horizon:
            soon.add(sym)
    return soon


def _earnings_calendar() -> dict[str, str]:
    """Whole-market upcoming earnings for the next ``_EARNINGS_HORIZON_DAYS``,
    as {symbol: nearest upcoming date}. Cached daily; only *successful* fetches
    are cached, so a transient failure is retried on the next run."""
    today = date.today()
    cache_path = config.EARNINGS_DIR / f"{today.isoformat()}.json"
    cached = _load_cache(cache_path)
    if cached is not None:
        return cached

    if not config.FINNHUB_KEY:
        print("WARNING: FINNHUB_KEY unset — earnings veto disabled.",
              file=sys.stderr)
        return {}
    try:
        r = requests.get(
            f"{FINNHUB_BASE}/calendar/earnings",
            params={"from": today.isoformat(),
                    "to": (today + timedelta(days=_EARNINGS_HORIZON_DAYS)).isoformat(),
                    "token": config.FINNHUB_KEY},
            timeout=_HTTP_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json().get("earningsCalendar", []) or []
    except Exception as e:  # noqa: BLE001 — don't cache a failure; retry next run
        print(f"WARNING: earnings calendar fetch failed ({e}); "
              f"earnings veto disabled this run.", file=sys.stderr)
        return {}

    calendar: dict[str, str] = {}
    for row in rows:
        sym, day = row.get("symbol"), row.get("date")
        if sym and day and day >= today.isoformat():
            if sym not in calendar or day < calendar[sym]:  # keep nearest
                calendar[sym] = day
    _save_cache(cache_path, calendar)
    return calendar


def _load_cache(path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None  # corrupt/unreadable cache ⇒ refetch


def _save_cache(path, obj: dict) -> None:
    path.write_text(json.dumps(obj))
