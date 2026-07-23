"""Offline tests for the risk gate (0.5.1): sizing, daily/drawdown halts,
gate ranking + slots, and the sector-concentration cap. No network/keys.
"""

from __future__ import annotations

from bot import config, journal, risk, sectors
from bot.signals import Signal


def _signal(symbol="AAA", entry=100.0, stop=95.0, target=110.0,
            score=50.0, vetoed=False, strategy="momentum") -> Signal:
    return Signal(
        symbol=symbol, side="buy", score=score, entry=entry, stop=stop,
        target=target, strategy=strategy, vetoed=vetoed,
    )


# ---------------------------------------------------------------------------
# position_size
# ---------------------------------------------------------------------------


def test_position_size_risk_bound():
    # entry=100, stop=90 -> risk_per_share=10, wide enough that RISK_PER_TRADE
    # binds before MAX_POSITION_PCT does (verified against current config).
    sig = _signal(entry=100.0, stop=90.0)
    equity = 10_000.0
    by_risk = (equity * config.RISK_PER_TRADE) / sig.risk_per_share
    by_size = (equity * config.MAX_POSITION_PCT) / sig.entry
    assert by_risk < by_size  # sanity: this case is actually risk-bound

    qty = risk.position_size(equity, sig)
    assert abs((sig.entry - sig.stop) * qty - config.RISK_PER_TRADE * equity) < (sig.entry - sig.stop)


def test_position_size_size_capped():
    sig = _signal(entry=100.0, stop=99.9)  # tight stop -> risk bound is huge
    equity = 10_000.0
    qty = risk.position_size(equity, sig)
    expected = int((equity * config.MAX_POSITION_PCT) / sig.entry)
    assert qty == expected


def test_position_size_untradeable():
    equity = 10_000.0
    assert risk.position_size(equity, _signal(entry=100.0, stop=100.0)) == 0
    assert risk.position_size(equity, _signal(entry=0.0, stop=95.0)) == 0


# ---------------------------------------------------------------------------
# daily_loss_breached
# ---------------------------------------------------------------------------


def test_daily_loss_breached(monkeypatch):
    monkeypatch.setattr(journal, "equity_at_day_start", lambda date: 10_000.0)
    assert risk.daily_loss_breached(10_000.0 * (1 + config.DAILY_LOSS_LIMIT)) is True


def test_daily_loss_not_breached_just_above(monkeypatch):
    monkeypatch.setattr(journal, "equity_at_day_start", lambda date: 10_000.0)
    just_above = 10_000.0 * (1 + config.DAILY_LOSS_LIMIT) + 1.0
    assert risk.daily_loss_breached(just_above) is False


def test_daily_loss_not_breached_when_no_start(monkeypatch):
    monkeypatch.setattr(journal, "equity_at_day_start", lambda date: 0.0)
    assert risk.daily_loss_breached(5_000.0) is False


# ---------------------------------------------------------------------------
# drawdown_breached
# ---------------------------------------------------------------------------


def test_drawdown_breached(monkeypatch):
    monkeypatch.setattr(journal, "high_water_mark", lambda: 10_000.0)
    assert risk.drawdown_breached(10_000.0 * (1 + config.DRAWDOWN_HALT)) is True


def test_drawdown_not_breached_just_above(monkeypatch):
    monkeypatch.setattr(journal, "high_water_mark", lambda: 10_000.0)
    just_above = 10_000.0 * (1 - 0.19)
    assert risk.drawdown_breached(just_above) is False


def test_drawdown_not_breached_when_no_hwm(monkeypatch):
    monkeypatch.setattr(journal, "high_water_mark", lambda: 0.0)
    assert risk.drawdown_breached(5_000.0) is False


# ---------------------------------------------------------------------------
# gate: ranking + slots
# ---------------------------------------------------------------------------


def test_gate_ranking_and_slots(monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 2)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 5)
    signals = [
        _signal(symbol="JPM", score=10.0),
        _signal(symbol="GS", score=90.0),
        _signal(symbol="V", score=50.0),
    ]
    approved = risk.gate(signals, equity=10_000.0, open_positions=[], trades_today=0)
    assert [s.symbol for s, _ in approved] == ["GS", "V"]


def test_gate_skips_vetoed_and_already_held(monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 3)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 3)
    signals = [
        _signal(symbol="JPM", score=90.0, vetoed=True),
        _signal(symbol="GS", score=80.0),
    ]
    approved = risk.gate(signals, equity=10_000.0, open_positions=["GS"], trades_today=0)
    assert approved == []


# ---------------------------------------------------------------------------
# gate: sector cap
# ---------------------------------------------------------------------------


def test_gate_sector_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 10)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 10)
    signals = [
        _signal(symbol="NVDA", score=90.0),
        _signal(symbol="AMD", score=80.0),
        _signal(symbol="MU", score=70.0),  # third semis candidate, should be dropped
    ]
    approved = risk.gate(signals, equity=10_000.0, open_positions=[], trades_today=0)
    assert [s.symbol for s, _ in approved] == ["NVDA", "AMD"]


def test_gate_sector_cap_counts_open_positions(monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 10)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 10)
    signals = [
        _signal(symbol="AMD", score=80.0),
        _signal(symbol="MU", score=70.0),
    ]
    approved = risk.gate(signals, equity=10_000.0, open_positions=["NVDA"], trades_today=0)
    assert [s.symbol for s, _ in approved] == ["AMD"]


def test_gate_unknown_sector_uncapped(monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 10)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 10)
    signals = [
        _signal(symbol="ZZZZ", score=90.0),
        _signal(symbol="YYYY", score=80.0),
    ]
    assert sectors.sector_of("ZZZZ") == ""
    assert sectors.sector_of("YYYY") == ""
    approved = risk.gate(signals, equity=10_000.0, open_positions=[], trades_today=0)
    assert [s.symbol for s, _ in approved] == ["ZZZZ", "YYYY"]


# ---------------------------------------------------------------------------
# sectors.sector_of
# ---------------------------------------------------------------------------


def test_sector_of_known_and_unknown():
    assert sectors.sector_of("NVDA") == "semis"
    assert sectors.sector_of("NOPE") == ""


# ---------------------------------------------------------------------------
# kill_switch_active
# ---------------------------------------------------------------------------


def test_kill_switch_active(tmp_path, monkeypatch):
    kill_file = tmp_path / "KILL"
    monkeypatch.setattr(config, "KILL_FILE", kill_file)
    assert risk.kill_switch_active() is False
    kill_file.touch()
    assert risk.kill_switch_active() is True
