"""Offline tests for the broker wrapper (plan 2a/2b/2c). No network/keys — a
stub TradingClient is injected via monkeypatch on broker.client, matching the
monkeypatch style in test_risk.py.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from alpaca.trading.enums import OrderSide, OrderStatus, OrderType
from bot import broker, config, journal
from bot.signals import Signal


def _order(symbol, side, status=OrderStatus.NEW, otype=OrderType.LIMIT):
    return SimpleNamespace(symbol=symbol, side=side, status=status,
                           type=otype, order_type=otype)


class FakeClient:
    """Emulates just enough of TradingClient, honoring the side/status filters
    the real API applies server-side so the tests exercise real logic."""

    def __init__(self, orders=None, positions=None, calendar=None):
        self._orders = orders or []
        self._positions = positions or []
        self._calendar = calendar or []
        self.submitted = None
        self.canceled = False
        self.closed_all = False

    def get_orders(self, filter=None):
        out = self._orders
        if filter is not None:
            if filter.side is not None:
                out = [o for o in out if o.side == filter.side]
            # QueryOrderStatus.OPEN -> only live orders (not filled/dead)
            if getattr(filter, "status", None) is not None and \
                    str(filter.status.value) == "open":
                dead = {OrderStatus.FILLED, OrderStatus.CANCELED,
                        OrderStatus.EXPIRED, OrderStatus.REJECTED}
                out = [o for o in out if o.status not in dead]
        return list(out)

    def get_all_positions(self):
        return self._positions

    def get_calendar(self, filters=None):
        return self._calendar

    def submit_order(self, req):
        self.submitted = req
        return SimpleNamespace(status="accepted", id="order-xyz")

    def cancel_orders(self):
        self.canceled = True

    def close_all_positions(self, cancel_orders=False):
        self.closed_all = True


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(broker, "client", lambda: client)
    return client


# ---------------------------------------------------------------------------
# entries_today
# ---------------------------------------------------------------------------


def test_entries_today_counts_open_and_filled_buys(fake):
    fake._orders = [
        _order("AAPL", OrderSide.BUY, OrderStatus.FILLED),
        _order("NVDA", OrderSide.BUY, OrderStatus.NEW),
        _order("TSLA", OrderSide.BUY, OrderStatus.PARTIALLY_FILLED),
    ]
    assert broker.entries_today() == 3


def test_entries_today_excludes_dead_and_sell_legs(fake):
    fake._orders = [
        _order("AAPL", OrderSide.BUY, OrderStatus.FILLED),      # count
        _order("NVDA", OrderSide.BUY, OrderStatus.CANCELED),    # excluded
        _order("MSFT", OrderSide.BUY, OrderStatus.EXPIRED),     # excluded
        _order("AMD", OrderSide.BUY, OrderStatus.REJECTED),     # excluded
        _order("AAPL", OrderSide.SELL, OrderStatus.NEW),        # sell leg, excluded
    ]
    assert broker.entries_today() == 1


# ---------------------------------------------------------------------------
# is_trading_day
# ---------------------------------------------------------------------------


def test_is_trading_day_true_when_calendar_has_today(fake):
    today = date.today()
    fake._calendar = [SimpleNamespace(date=today, open=None, close=None)]
    assert broker.is_trading_day() is True


def test_is_trading_day_false_when_calendar_empty(fake):
    fake._calendar = []
    assert broker.is_trading_day() is False


# ---------------------------------------------------------------------------
# positions_without_stops
# ---------------------------------------------------------------------------


def test_positions_without_stops_flags_naked(fake):
    fake._positions = [SimpleNamespace(symbol="AAPL"), SimpleNamespace(symbol="TSLA")]
    fake._orders = [
        _order("AAPL", OrderSide.SELL, OrderStatus.NEW, OrderType.STOP),   # protects AAPL
        _order("TSLA", OrderSide.SELL, OrderStatus.NEW, OrderType.LIMIT),  # take-profit, not a stop
    ]
    assert broker.positions_without_stops() == ["TSLA"]


def test_positions_without_stops_empty_when_all_covered(fake):
    fake._positions = [SimpleNamespace(symbol="AAPL")]
    fake._orders = [_order("AAPL", OrderSide.SELL, OrderStatus.NEW, OrderType.STOP_LIMIT)]
    assert broker.positions_without_stops() == []


# ---------------------------------------------------------------------------
# submit_bracket
# ---------------------------------------------------------------------------


def test_submit_bracket_request_shape_and_journal(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    sig = Signal(symbol="NVDA", side="buy", score=90.0, entry=100.126,
                 stop=95.44, target=115.98, strategy="momentum")
    order_id = broker.submit_bracket(sig, qty=7)

    assert order_id == "order-xyz"
    req = fake.submitted
    assert req.symbol == "NVDA"
    assert req.qty == 7
    assert req.side == OrderSide.BUY
    assert req.limit_price == 100.13            # rounded to 2dp
    assert req.stop_loss.stop_price == 95.44
    assert req.take_profit.limit_price == 115.98

    with journal._conn() as c:
        row = c.execute("SELECT symbol, side, qty, order_type, broker_order_id "
                        "FROM orders").fetchone()
    assert row == ("NVDA", "buy", 7, "bracket", "order-xyz")


# ---------------------------------------------------------------------------
# flatten_all
# ---------------------------------------------------------------------------


def test_flatten_all_cancels_closes_and_journals(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    broker.flatten_all("kill switch")
    assert fake.canceled is True
    assert fake.closed_all is True
    with journal._conn() as c:
        row = c.execute("SELECT symbol, order_type FROM orders").fetchone()
    assert row == ("*", "liquidate_all")
