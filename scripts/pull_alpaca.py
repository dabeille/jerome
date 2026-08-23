"""Read-only Alpaca pull for the weekly Phase-1 review (plan 1.2.3).

Banks the account, positions and full order lifecycle for a review window into
``docs/pi/<date>/alpaca.json``, then prints the checks the review turns on.

    python -m scripts.pull_alpaca                          # the week that just closed
    python -m scripts.pull_alpaca --since 2026-08-17 --until 2026-08-22
    python -m scripts.pull_alpaca --out docs/pi/2026-08-23

Why this is a separate pull rather than a journal query: ``submit_bracket``
journals the bracket *parent* only, so the child stop/target legs live nowhere
but the broker. Those legs are the evidence for the one gate item the
remediation plan left open — that a stop now survives the night (§2, "live
paper proof"). The journal cannot answer it at all.

Every call in this file is a GET. Nothing here submits, cancels or replaces.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.common.enums import Sort
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    QueryOrderStatus,
)
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest

from bot import config

ET = ZoneInfo("America/New_York")

# What counts as a working order at the broker.
#
# Deliberately *not* imported from bot.broker. This script's job is to audit the
# bot's own stop bookkeeping, and an auditor that shares the definition
# confirms its own bugs — which is precisely how the `held`-status defect
# survived a fully passing test suite (plan §10: "the test double was the
# reason B1 shipped broken"). Two independent spellings, or no check at all.
_LIVE_STATES = {
    OrderStatus.NEW,
    OrderStatus.ACCEPTED,
    OrderStatus.HELD,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.PENDING_NEW,
}
_STOP_TYPES = {OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP}

# The DAY-TIF signature: under the old code every bracket child leg was killed
# at the closing auction of its entry day (cancellations at 20:00:01 / 20:00:20
# / 20:00:36 UTC, plan §2). Checked in ET so it holds across DST.
_AUCTION = (time(15, 58), time(16, 5))

_PAGE = 500          # Alpaca's per-request order cap
_OLD_ACCOUNT_EQUITY = 50_000   # anything near this is the pre-remediation account


# --- helpers ----------------------------------------------------------------

def _money(x: float) -> str:
    return f"${x:,.2f}"


def _ts(dt: datetime | None) -> str:
    """Timestamps are printed in ET — the timezone every schedule in this
    project is expressed in — with the UTC time the broker recorded alongside,
    since the last review's evidence was all quoted in UTC."""
    if dt is None:
        return "—"
    return f"{dt.astimezone(ET):%Y-%m-%d %H:%M:%S} ET ({dt:%H:%M:%S}Z)"


def _num(x) -> float:
    return float(x or 0)


def _parse_day(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=ET)


def _default_window(now: datetime) -> tuple[datetime, datetime]:
    """Monday 00:00 ET of the week under review, through now.

    Run in its intended Sunday slot this is the week that just closed; run
    midweek it is the week in progress."""
    anchor = now
    if now.weekday() >= 5:                      # Sat/Sun -> back up to Friday
        anchor = now - timedelta(days=now.weekday() - 4)
    monday = anchor - timedelta(days=anchor.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0), now


def _fetch_orders(client: TradingClient, since: datetime,
                  until: datetime) -> list:
    """Every order touching the window, oldest first, child legs nested.

    ``status=ALL`` is required: the interesting rows are the canceled and
    expired ones, and the default filter hides them."""
    seen: set[str] = set()
    out: list = []
    cursor = since
    while True:
        page = client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL, after=cursor, until=until,
            nested=True, direction=Sort.ASC, limit=_PAGE,
        ))
        fresh = [o for o in page if str(o.id) not in seen]
        if not fresh:
            break
        seen.update(str(o.id) for o in fresh)
        out.extend(fresh)
        if len(page) < _PAGE:
            break
        cursor = max(o.submitted_at or o.created_at for o in fresh)
    return out


def _flatten(orders: list) -> dict[str, tuple]:
    """id -> (order, parent_id). Parents and bracket child legs alike.

    Deduped by id so it stays correct whether the API rolls legs up under
    their parent or also returns them as top-level rows."""
    flat: dict[str, tuple] = {}

    def walk(o, parent_id: str | None) -> None:
        if str(o.id) in flat:
            return
        flat[str(o.id)] = (o, parent_id)
        for leg in (getattr(o, "legs", None) or []):
            walk(leg, str(o.id))

    for o in orders:
        walk(o, None)
    return flat


def _in_auction(dt: datetime | None) -> bool:
    return dt is not None and _AUCTION[0] <= dt.astimezone(ET).time() <= _AUCTION[1]


# --- report sections --------------------------------------------------------

def _report_account(acct, positions) -> None:
    equity, cash = _num(acct.equity), _num(acct.cash)
    held = sum(_num(p.market_value) for p in positions)
    print(f"\n{'=' * 72}\nACCOUNT\n{'=' * 72}")
    print(f"  mode              {config.MODE}")
    print(f"  account           {acct.account_number}")
    print(f"  equity            {_money(equity)}")
    print(f"  cash              {_money(cash)}")
    print(f"  buying power      {_money(_num(acct.buying_power))}  "
          f"(multiplier {acct.multiplier}, shorting "
          f"{'enabled' if acct.shorting_enabled else 'disabled'})")

    # §6: MAX_BUYING_POWER_MULT is the no-leverage rule the gate now enforces.
    if equity:
        used = held / equity
        cap = config.MAX_BUYING_POWER_MULT
        flag = "  ** OVER CAP **" if used > cap else ""
        print(f"  held notional     {_money(held)} = {used:.1%} of equity "
              f"(cap {cap:.0%}){flag}")

    if equity > _OLD_ACCOUNT_EQUITY:
        print(f"\n  !! {_money(equity)} looks like the pre-remediation $100k "
              f"account, not the\n     $1,000 account from workstream D. Check "
              f"ALPACA_PAPER_* in .env — this\n     pull may be describing the "
              f"wrong account.")


def _report_positions(positions, equity: float) -> None:
    print(f"\n{'=' * 72}\nOPEN POSITIONS\n{'=' * 72}")
    if not positions:
        print("  (flat)")
        return
    print(f"  {'sym':<6} {'qty':>8} {'avg entry':>11} {'value':>12} "
          f"{'unreal P&L':>12} {'% equity':>9}")
    for p in sorted(positions, key=lambda p: -_num(p.market_value)):
        mv = _num(p.market_value)
        print(f"  {p.symbol:<6} {_num(p.qty):>8.0f} {_num(p.avg_entry_price):>11.2f} "
              f"{mv:>12,.0f} {_num(p.unrealized_pl):>12,.2f} "
              f"{(mv / equity if equity else 0):>8.1%}")


def _report_stops(positions, flat: dict[str, tuple]) -> None:
    """The gate item: is every open position protected, and did any exit leg
    die at the closing auction the way they all did pre-remediation?"""
    print(f"\n{'=' * 72}\nSTOP COVERAGE  (plan §2 — the open gate item)\n{'=' * 72}")

    protected = {
        o.symbol for o, _ in flat.values()
        if o.side == OrderSide.SELL
        and o.status in _LIVE_STATES
        and (o.type or o.order_type) in _STOP_TYPES
    }
    if positions:
        for p in positions:
            ok = p.symbol in protected
            print(f"  {p.symbol:<6} {'PROTECTED' if ok else '*** NAKED ***':<15}"
                  f"{'' if ok else '  no live SELL stop at the broker'}")
    else:
        print("  (no open positions to protect)")

    exits = [(o, pid) for o, pid in flat.values() if o.side == OrderSide.SELL]
    if exits:
        print(f"\n  Exit legs in window ({len(exits)}):")
        print(f"  {'sym':<6} {'type':<12} {'tif':<5} {'status':<12} "
              f"{'created':<28} {'ended'}")
        for o, _ in sorted(exits, key=lambda x: x[0].created_at or datetime.min):
            ended = o.filled_at or o.canceled_at or o.expired_at
            print(f"  {o.symbol:<6} {str(getattr(o.type or o.order_type, 'value', '')):<12} "
                  f"{str(getattr(o.time_in_force, 'value', '')):<5} "
                  f"{str(getattr(o.status, 'value', o.status)):<12} "
                  f"{_ts(o.created_at):<28} {_ts(ended)}")

    killed = [o for o, _ in flat.values()
              if o.side == OrderSide.SELL
              and o.status == OrderStatus.CANCELED
              and _in_auction(o.canceled_at)]
    print()
    if killed:
        print(f"  *** {len(killed)} exit leg(s) canceled inside the closing "
              f"auction window — the\n      DAY-TIF signature from the week of "
              f"2026-08-10. GTC brackets are NOT\n      in effect on this "
              f"account:")
        for o in killed:
            print(f"        {o.symbol:<6} canceled {_ts(o.canceled_at)}")
    else:
        print("  No exit leg was canceled at the closing auction. This is what "
              "workstream A\n  was supposed to fix; combined with a position "
              "held overnight still showing a\n  live stop above, it is the "
              "§2 proof.")


def _report_entries(flat: dict[str, tuple]) -> None:
    """Fill rate and TIF on the entry side. This is the measurement §4 wanted
    in the funnel payload and never got, so it still has to be derived here."""
    entries = [o for o, pid in flat.values()
               if pid is None and o.side == OrderSide.BUY]
    print(f"\n{'=' * 72}\nENTRIES  (fill rate — plan §4)\n{'=' * 72}")
    if not entries:
        print("  (no entry orders submitted in this window)")
        return

    print(f"  {'sym':<6} {'qty':>6} {'limit':>9} {'tif':<5} {'status':<11} "
          f"{'fill':>9} {'submitted':<28}")
    for o in sorted(entries, key=lambda o: o.submitted_at or o.created_at):
        print(f"  {o.symbol:<6} {_num(o.qty):>6.0f} {_num(o.limit_price):>9.2f} "
              f"{str(getattr(o.time_in_force, 'value', '')):<5} "
              f"{str(getattr(o.status, 'value', o.status)):<11} "
              f"{_num(o.filled_avg_price):>9.2f} "
              f"{_ts(o.submitted_at or o.created_at):<28}")

    filled = [o for o in entries if _num(o.filled_qty) > 0]
    dead = [o for o in entries
            if o.status in {OrderStatus.CANCELED, OrderStatus.EXPIRED,
                            OrderStatus.REJECTED} and not _num(o.filled_qty)]
    live = len(entries) - len(filled) - len(dead)
    print(f"\n  submitted {len(entries)}   filled {len(filled)}   "
          f"canceled/expired {len(dead)}   still working {live}")
    print(f"  fill rate {len(filled) / len(entries):.0%}")

    stale = [o for o in entries if o.time_in_force
             and getattr(o.time_in_force, "value", "") != "gtc"]
    if stale:
        print(f"\n  *** {len(stale)} entry order(s) submitted with TIF "
              f"'{getattr(stale[0].time_in_force, 'value', '?')}', not 'gtc'. "
              f"The Pi is running\n      pre-remediation code — check "
              f"`git log -1` there before trusting any of this.")

    # Slippage against the limit, on the fills that got one (§1.2.3's trigger
    # is realized entries drifting > ~0.1% from intended).
    slips = [(_num(o.filled_avg_price) - _num(o.limit_price)) / _num(o.limit_price)
             for o in filled if _num(o.limit_price)]
    if slips:
        print(f"  entry slippage vs limit: avg {sum(slips) / len(slips):+.3%}, "
              f"worst {max(slips, key=abs):+.3%}")


def _report_ledger(flat: dict[str, tuple]) -> None:
    """Per-symbol fill ledger. Window-scoped and therefore approximate: a
    position opened before ``since`` has no buy side here, so its realized
    number is meaningless. Alpaca's dashboard remains authoritative."""
    buys: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    sells: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for o, _ in flat.values():
        qty, px = _num(o.filled_qty), _num(o.filled_avg_price)
        if qty <= 0 or px <= 0:
            continue
        book = buys if o.side == OrderSide.BUY else sells
        book[o.symbol][0] += qty
        book[o.symbol][1] += qty * px

    symbols = sorted(set(buys) | set(sells))
    print(f"\n{'=' * 72}\nFILL LEDGER  (window-scoped, approximate)\n{'=' * 72}")
    if not symbols:
        print("  (no fills in this window)")
        return
    print(f"  {'sym':<6} {'bought':>8} {'avg':>9} {'sold':>8} {'avg':>9} "
          f"{'realized':>11}")
    total = 0.0
    for sym in symbols:
        bq, bn = buys[sym]
        sq, sn = sells[sym]
        b_avg = bn / bq if bq else 0.0
        s_avg = sn / sq if sq else 0.0
        realized = min(bq, sq) * (s_avg - b_avg) if bq and sq else 0.0
        total += realized
        print(f"  {sym:<6} {bq:>8.0f} {b_avg:>9.2f} {sq:>8.0f} {s_avg:>9.2f} "
              f"{realized:>11,.2f}")
    print(f"  {'':<6} {'':>8} {'':>9} {'':>8} {'total':>9} {total:>11,.2f}")


# --- main -------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", type=_parse_day, help="YYYY-MM-DD (ET), inclusive")
    ap.add_argument("--until", type=_parse_day, help="YYYY-MM-DD (ET), inclusive")
    ap.add_argument("--out", type=Path,
                    help="directory for alpaca.json "
                         "(default docs/pi/<today>/)")
    ap.add_argument("--allow-live", action="store_true",
                    help="permit running against the LIVE account")
    args = ap.parse_args()

    if config.IS_LIVE and not args.allow_live:
        sys.exit(f"BOT_MODE is '{config.MODE}' — this would read the LIVE "
                 f"account. Re-run with --allow-live if that is intended.")
    if not (config.ALPACA_KEY_ID and config.ALPACA_SECRET):
        sys.exit(f"No Alpaca keys for mode '{config.MODE}' — check .env.")

    now = datetime.now(ET)
    since, until = _default_window(now)
    if args.since:
        since = args.since
    if args.until:
        until = args.until.replace(hour=23, minute=59, second=59)

    client = TradingClient(config.ALPACA_KEY_ID, config.ALPACA_SECRET,
                           paper=not config.IS_LIVE)

    print(f"Window {since:%Y-%m-%d} .. {until:%Y-%m-%d} ET  "
          f"(pulled {now:%Y-%m-%d %H:%M} ET)")

    acct = client.get_account()
    positions = client.get_all_positions()
    orders = _fetch_orders(client, since, until)
    flat = _flatten(orders)

    try:
        history = client.get_portfolio_history(GetPortfolioHistoryRequest(
            start=since, end=until, timeframe="1D"))
    except Exception as exc:                       # non-essential cross-check
        print(f"  (portfolio history unavailable: {exc})")
        history = None

    _report_account(acct, positions)
    _report_positions(positions, _num(acct.equity))
    _report_stops(positions, flat)
    _report_entries(flat)
    _report_ledger(flat)

    out_dir = args.out or (config.ROOT / "docs" / "pi" / f"{now:%Y-%m-%d}")
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "alpaca.json"
    dest.write_text(json.dumps({
        "pulled_at": now.isoformat(),
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "mode": config.MODE,
        "account": acct.model_dump(mode="json"),
        "positions": [p.model_dump(mode="json") for p in positions],
        "orders": [o.model_dump(mode="json") for o in orders],
        "orders_flat": [
            {**o.model_dump(mode="json"), "parent_order_id": pid}
            for o, pid in flat.values()
        ],
        "portfolio_history": history.model_dump(mode="json") if history else None,
    }, indent=2, default=str))
    print(f"\nWrote {dest} ({dest.stat().st_size / 1024:.0f} KB, "
          f"{len(flat)} orders incl. child legs)")


if __name__ == "__main__":
    main()
