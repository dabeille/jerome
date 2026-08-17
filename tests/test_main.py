"""Offline tests for the run() control flow (plan 2a/2b/2c). No network/keys —
every collaborator (broker, risk, data, alerts) is replaced with a stub via
monkeypatch on the bot.main module namespace.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot import alerts, config, journal, main


class Recorder:
    """Records which broker/side-effect calls happened, so a test can assert
    the run stopped (or proceeded) at the right point."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            return None
        return record


def _fake_broker(monkeypatch, *, trading_day=True, drawdown=False,
                 daily_loss=False, entries=2, naked=None):
    rec = Recorder()
    acct = SimpleNamespace(equity="10000", cash="5000")
    calls = {"flatten": [], "funnel": [], "gate_trades_today": []}

    fake_broker = SimpleNamespace(
        equity=lambda: 10000.0,
        account=lambda: acct,
        is_trading_day=lambda: trading_day,
        entries_today=lambda: entries,
        open_position_symbols=lambda: [],
        positions_without_stops=lambda: (naked or []),
        flatten_all=lambda reason: calls["flatten"].append(reason),
        submit_bracket=lambda sig, qty: "order-1",
        held_notional=lambda: 0.0,
        cancel_stale_entries=lambda: [],
        reconcile_orders=lambda: 0,
        client=lambda: SimpleNamespace(get_all_positions=lambda: [
            SimpleNamespace(symbol=sym, qty="10", avg_entry_price="100",
                            market_value="1000", unrealized_pl="0")
            for sym in (naked or [])
        ]),
    )
    fake_risk = SimpleNamespace(
        kill_switch_active=lambda: False,
        drawdown_breached=lambda eq: drawdown,
        daily_loss_breached=lambda eq: daily_loss,
        gate=lambda *a, **k: (calls["gate_trades_today"].append(k.get("trades_today")) or []),
    )
    fake_data = SimpleNamespace(
        get_daily_bars=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("signals should not be reached in this run")),
        get_headlines=lambda syms: {},
        earnings_within=lambda syms: set(),
    )
    monkeypatch.setattr(main, "broker", fake_broker)
    monkeypatch.setattr(main, "risk", fake_risk)
    monkeypatch.setattr(main, "data", fake_data)
    monkeypatch.setattr(main, "alerts", SimpleNamespace(
        alert=lambda *a, **k: True,
        naked_position=alerts.naked_position,
        crashed=alerts.crashed,
    ))
    monkeypatch.setattr(config, "validate", lambda: None)
    monkeypatch.setattr(config, "MODE", "paper")
    return fake_broker, fake_risk, calls


def test_kill_switch_short_circuits(monkeypatch):
    _, fake_risk, calls = _fake_broker(monkeypatch)
    monkeypatch.setattr(main.risk, "kill_switch_active", lambda: True)
    main.run("morning")
    assert calls["flatten"] == ["kill switch"]
    # never reached the trading-day check / signals
    assert calls["gate_trades_today"] == []


def test_non_trading_day_no_ops_before_signals(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    _fake_broker(monkeypatch, trading_day=False)
    main.run("morning")  # fake_data.get_daily_bars would raise if reached
    # no equity snapshot logged, no funnel row
    assert journal.last_funnel() is None


def test_drawdown_breach_flattens_and_journals(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    _, _, calls = _fake_broker(monkeypatch, drawdown=True)
    main.run("morning")
    assert calls["flatten"] == ["drawdown circuit breaker"]
    rows = journal.recent_decisions("HALT-DRAWDOWN")
    assert len(rows) == 1


def test_happy_path_threads_entries_today_and_journals_funnel(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    fake_broker, _, calls = _fake_broker(monkeypatch, entries=2)
    # signals path must run: give an empty scan + no-op dashboard/LLM
    monkeypatch.setattr(main, "data", SimpleNamespace(
        get_daily_bars=lambda *a, **k: {},
        get_headlines=lambda syms: {},
        earnings_within=lambda syms: set()))
    monkeypatch.setattr(main.momentum, "scan", lambda bars: [])
    monkeypatch.setattr(main.meanrev, "scan", lambda bars: [])
    monkeypatch.setattr(main.llm_analyst, "review", lambda s, h, e: s)
    monkeypatch.setattr(main, "_write_dashboard", lambda session: None)

    main.run("morning")

    # gate got trades_today from broker.entries_today(), not the old hard-coded 0
    assert calls["gate_trades_today"] == [2]
    funnel = journal.last_funnel()
    assert funnel is not None
    _, run_name, payload = funnel
    assert run_name == "morning"
    assert payload["strategies"] == ["momentum", "meanrev"]
    assert payload["signals_generated"] == 0
    assert payload["entered"] == 0


def test_enabled_strategies_benches_meanrev(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    _fake_broker(monkeypatch, entries=0)
    monkeypatch.setattr(main, "data", SimpleNamespace(
        get_daily_bars=lambda *a, **k: {},
        get_headlines=lambda syms: {},
        earnings_within=lambda syms: set()))
    scanned = []
    monkeypatch.setattr(main.momentum, "scan",
                        lambda bars: scanned.append("momentum") or [])
    monkeypatch.setattr(main.meanrev, "scan", lambda bars: (_ for _ in ()).throw(
        AssertionError("meanrev is benched and must not be scanned")))
    monkeypatch.setattr(main.llm_analyst, "review", lambda s, h, e: s)
    monkeypatch.setattr(main, "_write_dashboard", lambda session: None)
    monkeypatch.setattr(config, "ENABLED_STRATEGIES", ("momentum",))

    main.run("morning")

    assert scanned == ["momentum"]
    _, _, payload = journal.last_funnel()
    assert payload["strategies"] == ["momentum"]


def test_naked_position_alerts_and_journals(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    alerted = []
    _fake_broker(monkeypatch, naked=["AAPL"])
    monkeypatch.setattr(main, "alerts", SimpleNamespace(
        alert=lambda subj, body="": alerted.append((subj, body)),
        naked_position=alerts.naked_position,
        crashed=alerts.crashed,
    ))
    monkeypatch.setattr(main, "_write_dashboard", lambda session: None)
    main.run("close")  # close session reaches the naked check then snapshots
    subject, body = alerted[0]
    assert "UNPROTECTED" in subject
    assert "$1,000" in subject          # exposure, not just the ticker
    assert "AAPL" in body and "WHAT TO DO NOW" in body
    assert len(journal.recent_decisions("NAKED-POSITION")) == 1


def test_run_with_alerts_pages_on_crash(monkeypatch):
    alerted = []
    monkeypatch.setattr(main, "alerts", SimpleNamespace(
        alert=lambda subj, body="": alerted.append((subj, body)),
        naked_position=alerts.naked_position,
        crashed=alerts.crashed,
    ))
    monkeypatch.setattr(main, "run", lambda session: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        main._run_with_alerts("morning")
    subject, body = alerted[0]
    assert "CRASH" in subject
    assert "boom" in body


# ---------------------------------------------------------------------------
# reattach-stops (remediation path the UNPROTECTED alert points at)
# ---------------------------------------------------------------------------


def test_reattach_stops_is_blocked_by_the_kill_switch(monkeypatch, tmp_path):
    """Re-arming places live orders, so KILL must win over it."""
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    fake_broker, _, calls = _fake_broker(monkeypatch, naked=["AAPL"])
    attached = []
    fake_broker.attach_exit_oco = lambda *a: attached.append(a)
    monkeypatch.setattr(main.risk, "kill_switch_active", lambda: True)

    main.run("reattach-stops")

    assert attached == []
    assert calls["flatten"] == ["kill switch"]


def test_reattach_stops_uses_journalled_entry_levels(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    fake_broker, _, _ = _fake_broker(monkeypatch, naked=["AAPL"])
    attached = []
    fake_broker.attach_exit_oco = lambda *a: (attached.append(a) or "oco-1")
    journal.log_decision("morning", "AAPL", "meanrev", "entered",
                         entry=100.0, stop=97.0, target=103.0, qty=10)

    main.run("reattach-stops")

    assert attached == [("AAPL", 10.0, 97.0, 103.0)]
    assert len(journal.recent_decisions("STOPS-REATTACHED")) == 1


def test_reattach_stops_skips_positions_with_no_journalled_levels(monkeypatch, tmp_path):
    """Never invent a stop for a position of unknown provenance."""
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    fake_broker, _, _ = _fake_broker(monkeypatch, naked=["AAPL"])
    attached = []
    fake_broker.attach_exit_oco = lambda *a: attached.append(a)

    main.run("reattach-stops")

    assert attached == []


def test_close_run_cancels_stale_entries_and_reconciles(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    fake_broker, _, _ = _fake_broker(monkeypatch)
    seen = {"cancelled": False, "reconciled": False}
    fake_broker.cancel_stale_entries = lambda: (
        seen.__setitem__("cancelled", True) or ["AAPL"])
    fake_broker.reconcile_orders = lambda: (
        seen.__setitem__("reconciled", True) or 1)
    monkeypatch.setattr(main, "_write_dashboard", lambda session: None)

    main.run("close")

    assert seen == {"cancelled": True, "reconciled": True}
    assert len(journal.recent_decisions("STALE-ENTRY-CANCELLED")) == 1


def test_morning_run_sweeps_entries_left_over_from_a_missed_close(monkeypatch, tmp_path):
    """GTC entries must not survive to fill days later at a stale price."""
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    fake_broker, _, _ = _fake_broker(monkeypatch)
    order = []
    fake_broker.cancel_stale_entries = lambda: (order.append("swept") or [])
    fake_broker.submit_bracket = lambda sig, qty: (order.append("submitted") or "o1")
    monkeypatch.setattr(main, "_write_dashboard", lambda session: None)
    monkeypatch.setattr(main.data, "get_daily_bars", lambda *a, **k: {})
    monkeypatch.setattr(main.llm_analyst, "review", lambda s, h, e: s)

    main.run("morning")

    # Swept before anything this session could place.
    assert order[:1] == ["swept"]
