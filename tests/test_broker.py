"""Offline tests for the broker wrapper (plan 2a/2b/2c). No network/keys — a
stub TradingClient is injected via monkeypatch on broker.client, matching the
monkeypatch style in test_risk.py.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from alpaca.trading.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from bot import broker, config, journal
from bot.signals import Signal


def _order(symbol, side, status=OrderStatus.NEW, otype=OrderType.LIMIT,
           order_id="oid", qty=10, filled_qty=0, filled_avg_price=None):
    return SimpleNamespace(symbol=symbol, side=side, status=status,
                           type=otype, order_type=otype, id=order_id,
                           qty=qty, filled_qty=filled_qty,
                           filled_avg_price=filled_avg_price)


# Statuses Alpaca's `status=open` filter actually returns. HELD is deliberately
# absent: a bracket's stop leg sits in `held` once the entry fills, and the real
# API does not return it under `open`. The previous version of this double
# treated `held` as open, which is precisely why positions_without_stops()
# shipped a bug that reported every position naked for a week — the double was
# more forgiving than the API it stood in for.
_API_OPEN_STATUSES = {
    OrderStatus.NEW,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.PENDING_NEW,
}


class FakeClient:
    """Emulates just enough of TradingClient, honoring the side/status/symbols
    filters the real API applies server-side so the tests exercise real logic."""

    def __init__(self, orders=None, positions=None, calendar=None):
        self._orders = orders or []
        self._positions = positions or []
        self._calendar = calendar or []
        self.submitted = None
        self.canceled = False
        self.closed_all = False
        self.canceled_ids = []

    def get_orders(self, filter=None):
        out = self._orders
        if filter is not None:
            if filter.side is not None:
                out = [o for o in out if o.side == filter.side]
            if getattr(filter, "symbols", None):
                out = [o for o in out if o.symbol in filter.symbols]
            if getattr(filter, "status", None) is not None and \
                    str(filter.status.value) == "open":
                out = [o for o in out if o.status in _API_OPEN_STATUSES]
        return list(out)

    def cancel_order_by_id(self, order_id):
        self.canceled_ids.append(order_id)

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


def test_held_stop_leg_counts_as_protected(fake):
    """The regression that cost a week of meaningless alerts.

    An armed bracket stop sits in `held` until its trigger price is touched.
    Reading it as "no stop" made the naked-position alert fire on every run
    whether or not anything was wrong — including 2026-08-10, when the stop it
    could not see executed 11 minutes later."""
    fake._positions = [SimpleNamespace(symbol="AAPL")]
    fake._orders = [_order("AAPL", OrderSide.SELL, OrderStatus.HELD, OrderType.STOP)]
    assert broker.positions_without_stops() == []


def test_dead_stop_leg_does_not_count_as_protected(fake):
    """The other side of it: a cancelled stop must still read as naked, or the
    fix would simply silence the alarm instead of correcting it."""
    fake._positions = [SimpleNamespace(symbol="AAPL")]
    fake._orders = [_order("AAPL", OrderSide.SELL, OrderStatus.CANCELED, OrderType.STOP)]
    assert broker.positions_without_stops() == ["AAPL"]


def test_positions_without_stops_no_positions_skips_query(fake):
    fake._positions = []
    fake._orders = [_order("AAPL", OrderSide.SELL, OrderStatus.HELD, OrderType.STOP)]
    assert broker.positions_without_stops() == []


# ---------------------------------------------------------------------------
# cancel_stale_entries / reconcile_orders (close-run housekeeping)
# ---------------------------------------------------------------------------


def test_cancel_stale_entries_cancels_only_live_unfilled_buys(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    fake._orders = [
        _order("AAPL", OrderSide.BUY, OrderStatus.NEW, order_id="live"),
        _order("NVDA", OrderSide.BUY, OrderStatus.FILLED, order_id="done"),
        _order("MSFT", OrderSide.BUY, OrderStatus.CANCELED, order_id="dead"),
        # partially filled: the position exists and its legs are working
        _order("AMD", OrderSide.BUY, OrderStatus.PARTIALLY_FILLED,
               order_id="partial", filled_qty=4),
    ]
    assert broker.cancel_stale_entries() == ["AAPL"]
    assert fake.canceled_ids == ["live"]


def test_reconcile_orders_writes_fills_back_to_journal(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    sig = Signal(symbol="NVDA", side="buy", score=90.0, entry=100.0,
                 stop=95.0, target=115.0, strategy="momentum")
    broker.submit_bracket(sig, qty=7)  # journals with the submission status

    fake._orders = [_order("NVDA", OrderSide.BUY, OrderStatus.FILLED,
                           order_id="order-xyz", filled_avg_price="101.25")]
    assert broker.reconcile_orders() == 1

    with journal._conn() as c:
        row = c.execute("SELECT status, fill_price FROM orders").fetchone()
    assert row == ("filled", 101.25)


def test_submitted_status_is_stored_as_value_not_enum_repr(fake, tmp_path, monkeypatch):
    """The journal recorded 'OrderStatus.PENDING_NEW' on every row."""
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    fake.submit_order = lambda req: SimpleNamespace(
        status=OrderStatus.PENDING_NEW, id="o1")
    sig = Signal(symbol="NVDA", side="buy", score=90.0, entry=100.0,
                 stop=95.0, target=115.0, strategy="momentum")
    broker.submit_bracket(sig, qty=7)

    with journal._conn() as c:
        assert c.execute("SELECT status FROM orders").fetchone()[0] == "pending_new"


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
    # GTC, not DAY: under DAY the exit legs were cancelled at the closing
    # auction of the entry day, leaving the position naked overnight.
    assert req.time_in_force == TimeInForce.GTC

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
