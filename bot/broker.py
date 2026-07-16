"""Broker wrapper around alpaca-py. All order flow goes through this module
so the kill switch and journaling can't be bypassed."""

from __future__ import annotations  # py3.9 compat (X | None, list[str] in annotations)

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from bot import config, journal
from bot.signals import Signal

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


def flatten_all(reason: str) -> None:
    """Cancel everything and liquidate. Used by kill switch + circuit breakers."""
    client().cancel_orders()
    client().close_all_positions(cancel_orders=True)
    journal.log_order("*", "sell", 0, "liquidate_all", f"submitted: {reason}")
