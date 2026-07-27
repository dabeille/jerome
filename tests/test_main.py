"""Offline tests for the run() control flow (plan 2a/2b/2c). No network/keys —
every collaborator (broker, risk, data, alerts) is replaced with a stub via
monkeypatch on the bot.main module namespace.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot import config, journal, main


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
        client=lambda: SimpleNamespace(get_all_positions=lambda: []),
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
    monkeypatch.setattr(main, "alerts", SimpleNamespace(alert=lambda *a, **k: True))
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
    assert payload["signals_generated"] == 0
    assert payload["entered"] == 0


def test_naked_position_alerts_and_journals(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    alerted = []
    _fake_broker(monkeypatch, naked=["AAPL"])
    monkeypatch.setattr(main, "alerts",
                        SimpleNamespace(alert=lambda subj, body="": alerted.append(subj)))
    monkeypatch.setattr(main, "_write_dashboard", lambda session: None)
    main.run("close")  # close session reaches the naked check then snapshots
    assert alerted and "NAKED POSITION" in alerted[0]
    assert len(journal.recent_decisions("NAKED-POSITION")) == 1


def test_run_with_alerts_pages_on_crash(monkeypatch):
    alerted = []
    monkeypatch.setattr(main, "alerts",
                        SimpleNamespace(alert=lambda subj, body="": alerted.append(subj)))
    monkeypatch.setattr(main, "run", lambda session: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        main._run_with_alerts("morning")
    assert alerted and "CRASHED" in alerted[0]
