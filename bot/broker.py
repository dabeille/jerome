"""Broker wrapper around alpaca-py. All order flow goes through this module
so the kill switch and journaling can't be bypassed."""

from __future__ import annotations  # py3.9 compat (X | None, list[str] in annotations)

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from bot import config, journal
from bot.signals import Signal

ET = ZoneInfo("America/New_York")

# Order states that mean "this entry never became (and can't become) a live
# position", so entries_today() must not count them.
_DEAD_ORDER_STATES = {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED}
_STOP_ORDER_TYPES = {OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP}

# Order states in which an order is still working at the broker.
#
# HELD is the one that matters and the one we got wrong: a bracket's stop leg
# sits in `held` from the moment the entry fills until its stop price triggers
# — that is what an *armed* stop looks like — and Alpaca's `status=open` filter
# does not return held orders. positions_without_stops() used that filter, so
# it never saw a live stop and reported every position as naked on all 13 runs
# of the week of 2026-08-10, including the runs where the stops were fine.
# Query with QueryOrderStatus.ALL and filter on this set instead.
_LIVE_ORDER_STATES = {
    OrderStatus.NEW,
    OrderStatus.ACCEPTED,
    OrderStatus.HELD,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.PENDING_NEW,
    OrderStatus.ACCEPTED_FOR_BIDDING,
}

# Cap on history pulled back for the symbol/day-scoped order queries below.
# At 1-3 trades/day this is months of headroom.
_ORDER_QUERY_LIMIT = 500


def _status_str(status) -> str:
    """Alpaca enum -> its wire value ('held'), not its repr ('OrderStatus.HELD').

    The journal stored ``str(order.status)`` and so recorded
    'OrderStatus.PENDING_NEW' on every row — awkward to query and brittle
    across alpaca-py versions. Tolerates a plain string for test doubles."""
    return str(getattr(status, "value", status))


def _today_start() -> datetime:
    return datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)


_client: TradingClient | None = None


def client() -> TradingClient:
    global _client
    if _client is None:
        _client = TradingClient(
            config.ALPACA_KEY_ID,
            config.ALPACA_SECRET,
            paper=not config.IS_LIVE,
        )
    return _client


def account():
    return client().get_account()


def equity() -> float:
    return float(account().equity)


def open_position_symbols() -> list[str]:
    return [p.symbol for p in client().get_all_positions()]


def held_notional() -> float:
    """Market value of all open positions — the already-committed side of the
    no-leverage cap (plan §5, enforced in risk.gate)."""
    return sum(float(p.market_value) for p in client().get_all_positions())


def submit_bracket(sig: Signal, qty: int) -> str:
    """Entry + stop-loss + take-profit as one unit. The account is never in a
    position without a live broker-side stop (plan §5).

    GTC, not DAY. Under DAY the child legs were cancelled at the closing
    auction of the entry day — measured on every filled bracket in the week of
    2026-08-10 (cancellations at 20:00:01/20:00:20/20:00:36 UTC) — leaving the
    position naked from its first overnight onward and, because it could then
    no longer exit by stop *or* target, parked in the book consuming an open
    slot. Alpaca allows only `day` or `gtc` on a bracket, so GTC is the fix;
    the entry limit inherits it, and cancel_stale_entries() sweeps the unfilled
    entries at the close so a stale limit can't rest for weeks.
    """
    order = client().submit_order(
        LimitOrderRequest(
            symbol=sig.symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            limit_price=round(sig.entry, 2),
            order_class="bracket",
            stop_loss=StopLossRequest(stop_price=round(sig.stop, 2)),
            take_profit=TakeProfitRequest(limit_price=round(sig.target, 2)),
        )
    )
    journal.log_order(sig.symbol, "buy", qty, "bracket",
                      _status_str(order.status), str(order.id))
    return str(order.id)


def cancel_stale_entries() -> list[str]:
    """Cancel every still-unfilled BUY entry. Returns the symbols cancelled.

    The other half of the GTC change: exit legs must outlive the day, entry
    limits must not. A breakout limit priced off yesterday's close is a stale
    price by tomorrow, not a standing intention.

    Called at the close (retiring the day's misses) *and* at the top of the
    morning run before any new signal is submitted. The morning call is
    insurance: if a close run is ever missed — crash, power cut, `flock` skip —
    a GTC entry would otherwise rest overnight and fill days later at a price
    that meant something on a different day. Any live unfilled entry is stale
    by definition, so the lookback covers a long weekend rather than just today.

    Partially-filled entries are left alone — the position exists and its
    bracket legs are working, so cancelling the parent here would be meddling
    with a live position rather than retiring a dead order.
    """
    lookback = _today_start() - timedelta(days=7)
    orders = client().get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.ALL, after=lookback,
                                side=OrderSide.BUY, limit=_ORDER_QUERY_LIMIT)
    )
    cancelled = []
    for o in orders:
        if o.status not in _LIVE_ORDER_STATES:
            continue
        if float(o.filled_qty or 0) > 0:
            continue
        # No journal write here on purpose: reconcile_orders() runs immediately
        # after in the close session and stamps the original bracket row with
        # its real terminal status. Logging a second row under the same
        # broker_order_id would just give reconciliation two rows to update.
        client().cancel_order_by_id(o.id)
        cancelled.append(o.symbol)
    return cancelled


def reconcile_orders() -> int:
    """Write today's terminal order statuses and fill prices back to the
    journal. Returns the number of rows updated.

    The journal's ``orders`` table was write-once at submission: every row read
    `status=OrderStatus.PENDING_NEW, fill_price=0.0` forever, so the journal
    held no fill data at all and the plan §1.2.3 slippage measurement had
    nothing to read. This closes the loop at the end of each day.
    """
    orders = client().get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.ALL, after=_today_start(),
                                limit=_ORDER_QUERY_LIMIT)
    )
    updated = 0
    for o in orders:
        updated += journal.update_order(
            str(o.id), _status_str(o.status), float(o.filled_avg_price or 0)
        )
    return updated


def entries_today() -> int:
    """Count BUY entry orders submitted today (ET) that are still live or
    filled — i.e. everything except canceled/expired/rejected (plan 1.1.5).

    This replaces main.py's hard-coded ``trades_today=0``. It counts pending
    entries, not just fills, on purpose: fills-only would let the midday run
    stack fresh brackets on top of unfilled morning ones, breaching the
    max-trades-per-day cap. The broker is ground truth here — the journal's
    orders table only records submission-time status and misses manual UI
    actions."""
    orders = client().get_orders(
        filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL, after=_today_start(), side=OrderSide.BUY
        )
    )
    return sum(1 for o in orders if o.status not in _DEAD_ORDER_STATES)


def is_trading_day() -> bool:
    """True if the market is open for regular trading today (ET), per Alpaca's
    own calendar (plan 1.1.3).

    Uses the calendar rather than ``get_clock().is_open`` because the 9:00 run
    fires before the 9:30 open — on a perfectly good trading day the market
    isn't open yet. Alpaca's calendar is authoritative for when Alpaca will
    accept orders, so no market-calendar dependency is needed."""
    today = datetime.now(ET).date()
    calendar = client().get_calendar(
        filters=GetCalendarRequest(start=today, end=today)
    )
    return any(day.date == today for day in calendar)


def positions_without_stops() -> list[str]:
    """Open position symbols that have no live broker-side SELL stop order
    protecting them (plan §5: 'never a position without a live stop').

    Alert-only signal — no auto-remediation in this phase. A false positive
    that auto-liquidated would be worse than a loud page you clear with one
    ``touch KILL``.

    Queries QueryOrderStatus.ALL scoped to the held symbols and filters on
    _LIVE_ORDER_STATES rather than using the OPEN filter: an armed stop sits in
    `held`, which Alpaca's `status=open` omits. See the _LIVE_ORDER_STATES
    comment — the OPEN filter made this function return every open position,
    always, which is why a week of naked-position alerts carried no
    information."""
    held = open_position_symbols()
    if not held:
        return []
    orders = client().get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.ALL, side=OrderSide.SELL,
                                symbols=held, limit=_ORDER_QUERY_LIMIT)
    )
    protected = {
        o.symbol
        for o in orders
        if o.status in _LIVE_ORDER_STATES
        and (o.type or o.order_type) in _STOP_ORDER_TYPES
    }
    return [sym for sym in held if sym not in protected]


def attach_exit_oco(symbol: str, qty: float, stop: float, target: float) -> str:
    """Attach a GTC stop/take-profit OCO pair to an already-open position.

    Remediation path for a position that lost its bracket legs (see
    ``bot.main reattach-stops``). A bracket can only be created with its entry,
    so re-arming an existing position needs a standalone OCO sell."""
    order = client().submit_order(
        LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            limit_price=round(target, 2),
            order_class="oco",
            stop_loss=StopLossRequest(stop_price=round(stop, 2)),
            take_profit=TakeProfitRequest(limit_price=round(target, 2)),
        )
    )
    journal.log_order(symbol, "sell", int(qty), "oco_reattach",
                      _status_str(order.status), str(order.id))
    return str(order.id)


def flatten_all(reason: str) -> None:
    """Cancel everything and liquidate. Used by kill switch + circuit breakers."""
    client().cancel_orders()
    client().close_all_positions(cancel_orders=True)
    journal.log_order("*", "sell", 0, "liquidate_all", f"submitted: {reason}")
