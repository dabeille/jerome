"""Dynamic-universe scanner (the DYNAMIC tier of the two-tier universe).

The static CORE tier in config.py anchors the scan; this module finds what is
*currently* in play: liquid names with real range, surfaced by Alpaca's
screener endpoints (most-actives + market movers) plus a seed watchlist, then
filtered hard for tradability on a small account:

    price >= $10                     (sub-$10 momo spreads eat the 3% risk budget)
    20-day avg dollar volume >= $100M
    14-day ATR >= 2.5% of price      (the range the 4-10R momentum thesis needs)

Survivors are ranked by ATR% x dollar volume and the top N written to
data/dynamic_universe.json, which config.py folds into UNIVERSE at import.

Run weekly, pre-market Monday: python -m scripts.refresh_universe

Backtest note: this tier is point-in-time. Feeding today's list to a backtest
bakes in survivorship/look-ahead bias — backtests use the static tiers only.
"""

from __future__ import annotations  # py3.9 compat

import json
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.enums import MostActivesBy
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import (
    MarketMoversRequest,
    MostActivesRequest,
    StockBarsRequest,
)
from alpaca.data.timeframe import TimeFrame

from bot import config, data

# Liquid high-beta names that earn a look every week even when the screener is
# quiet. They pass through the same filters as everything else.
SEED_WATCHLIST = [
    "NET", "DDOG", "SNOW", "CEG", "HIMS", "RKLB", "IONQ", "OKLO", "TEM",
    "IBIT", "DKNG", "AFRM", "RBLX", "SE", "MDB", "PANW", "ZS", "TTD",
]

# The screener loves leveraged/inverse/VIX ETPs — structurally decaying
# products, not swing candidates. Extend as new ones show up in refresh output.
EXCLUDE = {
    "TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXS", "UPRO", "SPXU",
    "TSLL", "TSLQ", "TSLZ", "NVDL", "NVDU", "NVDD", "MSTU", "MSTX", "MSTZ",
    "UVXY", "VXX", "VIXY", "SVXY", "SVIX", "UVIX",
    "LABU", "LABD", "TNA", "TZA", "FAS", "FAZ", "SSO", "SDS", "QLD", "QID",
    "YINN", "YANG", "FNGU", "FNGD", "BITX", "BITU", "SBIT", "ETHU",
    "BOIL", "KOLD", "UNG", "USO",
}

MIN_PRICE = 10.0
MIN_DOLLAR_VOLUME = 100e6   # 20-day average of close * volume
MIN_ATR_PCT = 2.5           # 14-day ATR as % of last close
TOP_N = 15

_SYMBOL_RE = re.compile(r"[A-Z]{1,5}")  # skips preferreds/warrants/units

_screener: ScreenerClient | None = None


def screener() -> ScreenerClient:
    global _screener
    if _screener is None:
        _screener = ScreenerClient(config.ALPACA_KEY_ID, config.ALPACA_SECRET)
    return _screener


def candidates() -> set[str]:
    """Screener output + seed watchlist, minus static tiers and junk."""
    syms: set[str] = set(SEED_WATCHLIST)
    try:
        actives = screener().get_most_actives(
            MostActivesRequest(by=MostActivesBy.VOLUME, top=100)
        )
        syms |= {a.symbol for a in actives.most_actives}
    except Exception as exc:  # one dead endpoint shouldn't kill the refresh
        print(f"most-actives fetch failed ({exc}) — continuing without it")
    try:
        movers = screener().get_market_movers(MarketMoversRequest(top=50))
        syms |= {m.symbol for m in movers.gainers + movers.losers}
    except Exception as exc:
        print(f"market-movers fetch failed ({exc}) — continuing without it")

    static = set(config.ETF_UNIVERSE) | set(config.CORE_STOCKS)
    return {
        s for s in syms
        if _SYMBOL_RE.fullmatch(s) and s not in EXCLUDE and s not in static
    }


MAX_ATR_PCT = 15.0        # above this it's a low-float squeeze, not swing range
MAX_DAILY_MOVE_PCT = 60.0  # single-day |close/prev_close - 1|; catches unadjusted
                           # splits / symbol-reuse artifacts in the feed, not real moves


def metrics(df: pd.DataFrame) -> "dict | None":
    """Close / 20d avg dollar volume / 14d ATR% for one symbol, or None when
    there isn't enough history to judge (recent IPOs, long halts) or the bars
    contain a data artifact (see MAX_DAILY_MOVE_PCT)."""
    if len(df) < 40:
        return None
    close = df["close"]
    prev = close.shift(1)
    daily_move_pct = ((close / prev - 1).abs() * 100).tail(90)
    if daily_move_pct.max() > MAX_DAILY_MOVE_PCT:
        return None  # e.g. unadjusted split/ticker-reuse discontinuity, not a real move
    last = float(close.iloc[-1])
    dollar_vol = float((close * df["volume"]).tail(20).mean())
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
        axis=1,
    ).max(axis=1)
    atr = float(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
    atr_pct = atr / last * 100
    return {
        "close": round(last, 2),
        "dollar_vol": round(dollar_vol),
        "atr_pct": round(atr_pct, 2),
        "score": round(atr_pct * dollar_vol),
    }


def refresh(top_n: int = TOP_N) -> dict:
    """Screen, rank, and write data/dynamic_universe.json; returns the payload."""
    pool = sorted(candidates())
    rows: dict[str, dict] = {}
    if pool:
        start = datetime.now(timezone.utc) - timedelta(days=120)
        req = StockBarsRequest(
            symbol_or_symbols=pool, timeframe=TimeFrame.Day, start=start
        )
        bars = data.client().get_stock_bars(req).df  # transient — not cached
        for sym in pool:
            if sym not in bars.index.get_level_values(0):
                continue
            m = metrics(bars.xs(sym, level=0))
            if (
                m
                and m["close"] >= MIN_PRICE
                and m["dollar_vol"] >= MIN_DOLLAR_VOLUME
                and MIN_ATR_PCT <= m["atr_pct"] <= MAX_ATR_PCT
            ):
                rows[sym] = m

    ranked = sorted(rows, key=lambda s: rows[s]["score"], reverse=True)[:top_n]
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "criteria": {
            "min_price": MIN_PRICE,
            "min_dollar_volume": MIN_DOLLAR_VOLUME,
            "min_atr_pct": MIN_ATR_PCT,
            "max_atr_pct": MAX_ATR_PCT,
            "top_n": top_n,
        },
        "symbols": ranked,
        "metrics": {s: rows[s] for s in ranked},
    }
    config.DYNAMIC_UNIVERSE_FILE.write_text(json.dumps(payload, indent=2))
    return payload
