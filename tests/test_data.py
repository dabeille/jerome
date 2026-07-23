"""Offline tests for bot.data's transforms + caching (steps 0.3.1, 0.3.3).

No network/keys: _normalize and the JSON cache helpers are pure; get_headlines
is exercised with its fetch calls monkeypatched and its cache dir redirected to
a tmp path.
"""

from __future__ import annotations

import pandas as pd

from bot import config, data


# --- _normalize --------------------------------------------------------------

def test_normalize_alpaca_shaped_is_idempotent():
    idx = pd.DatetimeIndex(["2024-01-03", "2024-01-02"], name="timestamp")
    df = pd.DataFrame(
        {c: [1.0, 2.0] for c in data._COLUMNS}, index=idx
    )
    out = data._normalize(df)
    assert list(out.columns) == data._COLUMNS
    assert out.index.name == "timestamp"
    assert out.index.is_monotonic_increasing          # sorted ascending
    assert not out.isna().any().any()                 # nothing introduced


def test_normalize_yfinance_shaped_reshapes_and_nan_fills():
    idx = pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    df = pd.DataFrame(
        {
            "Open": [1.0, 2.0], "High": [1.5, 2.5], "Low": [0.5, 1.5],
            "Close": [1.2, 2.2], "Volume": [10, 20],
            "Dividends": [0.0, 0.0], "Stock Splits": [0.0, 0.0],
        },
        index=idx,
    )
    out = data._normalize(df)
    assert list(out.columns) == data._COLUMNS          # extras dropped, order canonical
    assert out["trade_count"].isna().all()             # absent in yfinance → NaN
    assert out["vwap"].isna().all()
    assert out["open"].tolist() == [1.0, 2.0]
    assert out.index.name == "timestamp"


# --- cache helpers -----------------------------------------------------------

def test_cache_roundtrip(tmp_path):
    path = tmp_path / "day.json"
    payload = {"AAA": ["h1", "h2"], "BBB": []}
    data._save_cache(path, payload)
    assert data._load_cache(path) == payload


def test_load_cache_missing_returns_none(tmp_path):
    assert data._load_cache(tmp_path / "nope.json") is None


def test_load_cache_corrupt_returns_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not valid json")
    assert data._load_cache(path) is None


# --- get_headlines cache behaviour -------------------------------------------

def test_get_headlines_caches_and_reuses(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NEWS_DIR", tmp_path)
    calls = {"alpaca": 0}

    def fake_alpaca(symbols):
        calls["alpaca"] += 1
        return {s: [f"{s} headline"] for s in symbols}

    monkeypatch.setattr(data, "_fetch_alpaca_news", fake_alpaca)
    monkeypatch.setattr(data, "_fetch_finnhub_news", lambda s: [])

    first = data.get_headlines(["AAA", "BBB"])
    assert first == {"AAA": ["AAA headline"], "BBB": ["BBB headline"]}
    assert list(tmp_path.glob("*.json"))               # day-cache written

    second = data.get_headlines(["AAA", "BBB"])         # served from cache
    assert second == first
    assert calls["alpaca"] == 1                          # no re-fetch


def test_get_headlines_finnhub_fallback_for_uncovered_symbol(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NEWS_DIR", tmp_path)
    # Alpaca covers AAA only; BBB must fall through to Finnhub.
    monkeypatch.setattr(data, "_fetch_alpaca_news", lambda syms: {"AAA": ["a"]})
    monkeypatch.setattr(data, "_fetch_finnhub_news", lambda s: [f"finnhub {s}"])

    out = data.get_headlines(["AAA", "BBB"])
    assert out["AAA"] == ["a"]
    assert out["BBB"] == ["finnhub BBB"]
