"""Broker wrapper around alpaca-py. All order flow goes through this module
so the kill switch and journaling can't be bypassed."""

from __future__ import annotations  # py3.9 compat (X | None, list[str] in annotations)

from datetime import datetime
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


def submit_bracket(sig: Signal, qty: int) -> str:
    """Entry + stop-loss + take-profit as one unit. The account is never in a
    position without a live broker-side stop (plan §5)."""
    order = client().submit_order(
        LimitOrderRequest(
            symbol=sig.symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(sig.entry, 2),
            order_class="bracket",
            stop_loss=StopLossRequest(stop_price=round(sig.stop, 2)),
            take_profit=TakeProfitRequest(limit_price=round(sig.target, 2)),
        )
    )
    journal.log_order(sig.symbol, "buy", qty, "bracket",
                      str(order.status), str(order.id))
    return str(order.id)


def entries_today() -> int:
    """Count BUY entry orders submitted today (ET) that are still live or
    filled — i.e. everything except canceled/expired/rejected (plan 1.1.5).

    This replaces main.py's hard-coded ``trades_today=0``. It counts pending
    entries, not just fills, on purpose: fills-only would let the midday run
    stack fresh brackets on top of unfilled morning ones, breaching the
    max-trades-per-day cap. The broker is ground truth here — the journal's
    orders table only records submission-time status and misses manual UI
    actions."""
    start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    orders = client().get_orders(
        filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL, after=start, side=OrderSide.BUY
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
    ``touch KILL``."""
    protected = {
        o.symbol
        for o in client().get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, side=OrderSide.SELL)
        )
        if (o.type or o.order_type) in _STOP_ORDER_TYPES
    }
    return [sym for sym in open_position_symbols() if sym not in protected]


def flatten_all(reason: str) -> None:
    """Cancel everything and liquidate. Used by kill switch + circuit breakers."""
    client().cancel_orders()
    client().close_all_positions(cancel_orders=True)
    journal.log_order("*", "sell", 0, "liquidate_all", f"submitted: {reason}")
