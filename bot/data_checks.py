"""Step 0.3.4: data sanity checks over the cached daily bars.

Cheap, dependency-free checks run before any signal trusts the data. Bars are
already split+dividend adjusted at the source (the 0.3.1 downloader requests
Alpaca ``Adjustment.ALL`` / yfinance ``auto_adjust``); this module verifies
nothing slipped through and that the series is otherwise sane:

- missing_days     gaps vs the benchmark's own trading calendar. SPY is the
                   ground truth for which days the market was open, so we need
                   no market-calendar dependency (Pi-safe).
- stale            last bar too far behind the benchmark's last bar — catches
                   delisted/renamed tickers (e.g. SQ→XYZ) and download gaps.
- suspected_split  a day-over-day move too large to be real ⇒ an unadjusted
                   corporate action. Should be rare now that bars are adjusted;
                   this is the backstop that says "look at this one".
- ohlc_integrity   high≥low, high≥max(open,close), low≤min(open,close),
                   non-negative volume, no NaN in OHLCV.
"""

from __future__ import annotations  # py3.9 compat

from dataclasses import dataclass
from datetime import date

import pandas as pd

# Tunables.
STALE_MAX_LAG = 3          # benchmark trading days the last bar may trail by
SPLIT_MOVE_THRESHOLD = 0.5  # |close-to-close| beyond this ⇒ suspected split
_MISSING_EXAMPLES = 5      # how many example gap dates to include in a report


@dataclass
class Issue:
    symbol: str
    kind: str      # missing_days | stale | suspected_split | ohlc_integrity
    detail: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.symbol}: {self.detail}"


def _trading_dates(df: pd.DataFrame) -> list[date]:
    """Sorted unique calendar dates in a bar frame (tz-agnostic)."""
    return sorted({ts.date() for ts in df.index})


def run_checks(bars: dict[str, pd.DataFrame],
               benchmark: str = "SPY") -> list[Issue]:
    """Run every check over ``bars`` and return a flat, sorted issue list."""
    issues: list[Issue] = []
    ref = _trading_dates(bars[benchmark]) if benchmark in bars else None
    if ref is None:
        issues.append(Issue(benchmark, "missing_days",
                            f"benchmark {benchmark} not in dataset — "
                            f"missing-day/stale checks skipped"))

    for sym, df in bars.items():
        issues.extend(_check_ohlc(sym, df))
        issues.extend(_check_split(sym, df))
        if ref is not None:
            issues.extend(_check_missing(sym, df, ref))
            issues.extend(_check_stale(sym, df, ref))

    issues.sort(key=lambda i: (i.symbol, i.kind))
    return issues


def _check_missing(sym: str, df: pd.DataFrame, ref: list[date]) -> list[Issue]:
    """Benchmark trading days within the symbol's own range that it lacks.

    Bounded to [first, last] of the symbol so a shorter history (later IPO)
    isn't flagged for the pre-listing period."""
    have = set(_trading_dates(df))
    if not have:
        return [Issue(sym, "missing_days", "no bars at all")]
    lo, hi = min(have), max(have)
    gaps = [d for d in ref if lo <= d <= hi and d not in have]
    if not gaps:
        return []
    ex = ", ".join(str(d) for d in gaps[:_MISSING_EXAMPLES])
    more = "" if len(gaps) <= _MISSING_EXAMPLES else f", +{len(gaps) - _MISSING_EXAMPLES} more"
    return [Issue(sym, "missing_days", f"{len(gaps)} gap(s) vs benchmark ({ex}{more})")]


def _check_stale(sym: str, df: pd.DataFrame, ref: list[date]) -> list[Issue]:
    """Flag if the benchmark has more than STALE_MAX_LAG trading days after
    this symbol's last bar (⇒ delisted/renamed, or a stalled download)."""
    have = _trading_dates(df)
    if not have:
        return []  # already reported by _check_missing
    last = have[-1]
    behind = [d for d in ref if d > last]
    if len(behind) > STALE_MAX_LAG:
        return [Issue(sym, "stale",
                      f"last bar {last} is {len(behind)} trading days behind "
                      f"benchmark ({ref[-1]})")]
    return []


def _check_split(sym: str, df: pd.DataFrame) -> list[Issue]:
    """Suspected unadjusted corporate action: a close-to-close move beyond
    SPLIT_MOVE_THRESHOLD."""
    close = pd.to_numeric(df["close"], errors="coerce")
    pct = close.pct_change(fill_method=None)
    hits = pct[pct.abs() > SPLIT_MOVE_THRESHOLD].dropna()
    return [
        Issue(sym, "suspected_split",
              f"{ts.date()} close moved {chg:+.0%} (adjusted-data anomaly?)")
        for ts, chg in hits.items()
    ]


def _check_ohlc(sym: str, df: pd.DataFrame) -> list[Issue]:
    """Structural OHLCV validity. One issue per failing rule (with row counts)."""
    issues: list[Issue] = []
    o, h, l, c = (pd.to_numeric(df[x], errors="coerce")
                  for x in ("open", "high", "low", "close"))
    v = pd.to_numeric(df["volume"], errors="coerce")

    nan_rows = int(pd.concat([o, h, l, c, v], axis=1).isna().any(axis=1).sum())
    if nan_rows:
        issues.append(Issue(sym, "ohlc_integrity", f"{nan_rows} row(s) with NaN in OHLCV"))

    bad_hl = int((h < l).sum())
    if bad_hl:
        issues.append(Issue(sym, "ohlc_integrity", f"{bad_hl} row(s) with high < low"))

    bad_hi = int((h < o.combine(c, max)).sum())
    if bad_hi:
        issues.append(Issue(sym, "ohlc_integrity", f"{bad_hi} row(s) with high < max(open,close)"))

    bad_lo = int((l > o.combine(c, min)).sum())
    if bad_lo:
        issues.append(Issue(sym, "ohlc_integrity", f"{bad_lo} row(s) with low > min(open,close)"))

    bad_vol = int((v < 0).sum())
    if bad_vol:
        issues.append(Issue(sym, "ohlc_integrity", f"{bad_vol} row(s) with negative volume"))

    return issues
