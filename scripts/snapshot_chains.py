"""EOD option-chain snapshots (plan 1.2.5). Collection only — no strategy
reads these yet; we build IV history ahead of the options Addendum.

Pulls indicative chains for config.OPTION_UNDERLYINGS, filtered to <=60 DTE and
strikes within +/-20% of spot, and stores one gzipped JSON per underlying per
day under data/chains/<SYM>/<YYYY-MM-DD>.json.gz (SD-card friendly). Idempotent
(skip-if-exists), per-underlying try/except, alert only on total failure. Runs
on its own ~16:15 ET cron line, outside trading sessions, so a chain failure
can never touch the trading path.

    python -m scripts.snapshot_chains
    python -m scripts.snapshot_chains --symbols SPY,QQQ
    python -m scripts.snapshot_chains --force        # re-snapshot even if present
"""

from __future__ import annotations  # py3.9 compat

import argparse
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone

from alpaca.data.enums import OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest

from bot import alerts, broker, config, data

MAX_DTE = 60
STRIKE_BAND = 0.20  # +/-20% of spot


def _option_client() -> OptionHistoricalDataClient:
    return OptionHistoricalDataClient(config.ALPACA_KEY_ID, config.ALPACA_SECRET)


def _parse_occ(occ: str) -> tuple[str, float, str]:
    """OCC symbol -> (expiry ISO, strike, 'call'|'put').
    e.g. 'SPY260717C00620000' -> ('2026-07-17', 620.0, 'call')."""
    strike = int(occ[-8:]) / 1000.0
    kind = "call" if occ[-9].upper() == "C" else "put"
    yy, mm, dd = occ[-15:-13], occ[-13:-11], occ[-11:-9]
    return f"20{yy}-{mm}-{dd}", strike, kind


def _spot(symbol: str) -> float:
    """Latest cached close as the strike-band anchor. The cache is a day stale
    at 16:15 ET, which is fine for a +/-20% filter."""
    bars = data.get_daily_bars([symbol], years=1, refresh=False).get(symbol)
    if bars is None or bars.empty:
        raise RuntimeError(f"no cached bars for {symbol}; run fetch_history first")
    return float(bars["close"].iloc[-1])


def _snapshot_one(client: OptionHistoricalDataClient, symbol: str,
                  today: str, force: bool) -> str:
    out_dir = config.CHAINS_DIR / symbol
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}.json.gz"
    if out_path.exists() and not force:
        return "skip"

    spot = _spot(symbol)
    cutoff = (datetime.now(broker.ET).date() + timedelta(days=MAX_DTE)).isoformat()
    chain = client.get_option_chain(OptionChainRequest(
        underlying_symbol=symbol,
        feed=OptionsFeed.INDICATIVE,
        expiration_date_lte=cutoff,
        strike_price_gte=round(spot * (1 - STRIKE_BAND), 2),
        strike_price_lte=round(spot * (1 + STRIKE_BAND), 2),
    ))

    contracts = []
    for occ, snap in chain.items():
        expiry, strike, kind = _parse_occ(occ)
        quote, trade, greeks = snap.latest_quote, snap.latest_trade, snap.greeks
        contracts.append({
            "symbol": occ, "expiry": expiry, "strike": strike, "type": kind,
            "bid": float(quote.bid_price) if quote else None,
            "ask": float(quote.ask_price) if quote else None,
            "last": float(trade.price) if trade else None,
            "iv": float(snap.implied_volatility) if snap.implied_volatility else None,
            "delta": float(greeks.delta) if greeks and greeks.delta is not None else None,
        })

    payload = {
        "underlying": symbol, "date": today, "spot": spot,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "contract_count": len(contracts), "contracts": contracts,
    }
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        json.dump(payload, f)
    return f"{len(contracts)} contracts"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="",
                        help="comma-separated override; default OPTION_UNDERLYINGS")
    parser.add_argument("--force", action="store_true",
                        help="re-snapshot even if today's file already exists")
    args = parser.parse_args()

    config.validate()
    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else config.OPTION_UNDERLYINGS)
    today = datetime.now(broker.ET).date().isoformat()
    client = _option_client()

    ok, failed = 0, []
    for sym in symbols:
        try:
            result = _snapshot_one(client, sym, today, args.force)
            print(f"  {sym:6} {result}")
            ok += 1
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not stop the rest
            print(f"  {sym:6} FAILED: {exc}", file=sys.stderr)
            failed.append(sym)

    print(f"\n{ok}/{len(symbols)} underlyings snapshotted into {config.CHAINS_DIR}")
    if ok == 0:
        alerts.alert("bot chain snapshot FAILED",
                     f"Every underlying failed: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
