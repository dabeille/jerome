"""Momentum / breakout strategy (plan §4A). Step 0.4.1.

- 20-day-high breakout with relative volume > 2x 20-day average
- Relative strength vs SPY over 20 days in top decile of the passing set
- Entry at breakout close, stop below the consolidation low (~1R),
  target 3R
"""

from __future__ import annotations  # py3.9 compat

import pandas as pd

from bot.signals import Signal

LOOKBACK = 20              # sessions in the breakout/RS/relvol window
MIN_BARS = LOOKBACK + 20   # buffer so the rolling window isn't the whole history
RELVOL_MULT = 2.0          # today's volume must exceed this multiple of the 20d avg
RS_TOP_PCT = 0.90          # keep only the top decile of relative strength
BENCHMARK = "SPY"
TARGET_R = 3.0             # target = entry + TARGET_R * (entry - stop)


def _candidate(sym: str, df: pd.DataFrame, bench_ret: float) -> dict | None:
    """Breakout/relvol/RS metrics for one symbol, or None if it doesn't qualify."""
    if len(df) < MIN_BARS:
        return None

    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce")

    window_high = high.iloc[-LOOKBACK - 1:-1]
    window_low = low.iloc[-LOOKBACK - 1:-1]
    window_vol = volume.iloc[-LOOKBACK - 1:-1]
    last_close, last_vol = close.iloc[-1], volume.iloc[-1]

    prior_high = window_high.max()
    if pd.isna(prior_high) or pd.isna(last_close) or last_close <= prior_high:
        return None  # no breakout

    avg_vol = window_vol.mean()
    if pd.isna(avg_vol) or avg_vol <= 0 or pd.isna(last_vol) or last_vol < RELVOL_MULT * avg_vol:
        return None  # not enough volume confirmation

    swing_low = window_low.min()
    if pd.isna(swing_low) or swing_low >= last_close:
        return None  # no usable stop distance

    start_close = close.iloc[-LOOKBACK - 1]
    if pd.isna(start_close) or start_close <= 0:
        return None
    rs = (last_close / start_close - 1) - bench_ret

    return {
        "symbol": sym,
        "entry": float(last_close),
        "stop": float(swing_low),
        "relvol": float(last_vol / avg_vol),
        "rs": float(rs),
    }


def scan(bars: dict[str, pd.DataFrame], target_r: float = TARGET_R) -> list[Signal]:
    """bars: symbol -> daily OHLCV DataFrame (ascending dates).

    ``target_r`` overrides the take-profit distance (target = entry +
    target_r * risk) for backtest tuning; the module default matches live.
    """
    bench_df = bars.get(BENCHMARK)
    if bench_df is None or len(bench_df) < MIN_BARS:
        return []
    bench_close = pd.to_numeric(bench_df["close"], errors="coerce")
    bench_start, bench_last = bench_close.iloc[-LOOKBACK - 1], bench_close.iloc[-1]
    if pd.isna(bench_start) or bench_start <= 0 or pd.isna(bench_last):
        return []
    bench_ret = bench_last / bench_start - 1

    candidates = [
        c for sym, df in bars.items() if sym != BENCHMARK
        for c in [_candidate(sym, df, bench_ret)] if c is not None
    ]
    if not candidates:
        return []

    rs_cutoff = pd.Series([c["rs"] for c in candidates]).quantile(RS_TOP_PCT)

    signals = []
    for c in candidates:
        if c["rs"] < rs_cutoff:
            continue
        entry, stop = c["entry"], c["stop"]
        target = entry + target_r * (entry - stop)
        score = round(min(100.0, max(0.0,
            50.0 * (c["relvol"] / RELVOL_MULT) + 500.0 * max(c["rs"], 0.0)
        )), 1)
        signals.append(Signal(
            symbol=c["symbol"],
            side="buy",
            score=score,
            entry=entry,
            stop=stop,
            target=target,
            strategy="momentum",
            reasoning=(
                f"20d-high breakout, relvol {c['relvol']:.1f}x, "
                f"RS vs {BENCHMARK} {c['rs']:+.1%} (top decile of candidates)"
            ),
        ))

    signals.sort(key=lambda s: s.score, reverse=True)
    return signals
