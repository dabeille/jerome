"""Offline tests for the risk gate (0.5.1): sizing, daily/drawdown halts,
gate ranking + slots, and the sector-concentration cap. No network/keys.
"""

from __future__ import annotations

import pytest

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


def test_position_size_fractional_returns_exact_risk_bound():
    # entry=50 stop=43 -> risk_per_share=7; risk bound = 0.03*1000/7 ≈ 4.286
    # shares at $1k (below the by_size bound of 8.0), which the integer path
    # truncates to 4 but fractional keeps whole.
    sig = _signal(entry=50.0, stop=43.0)
    equity = 1_000.0
    by_risk = (equity * config.RISK_PER_TRADE) / sig.risk_per_share
    by_size = (equity * config.MAX_POSITION_PCT) / sig.entry
    assert by_risk < by_size  # risk-bound case
    frac = risk.position_size(equity, sig, fractional=True)
    assert frac == pytest.approx(by_risk)
    assert frac != int(frac)  # genuinely fractional (≈4.286)
    assert risk.position_size(equity, sig) == 4  # integer path floors


def test_position_size_fractional_partial_share():
    # entry=500 at $1k equity: MAX_POSITION_PCT (40%) caps at 0.8 shares — the
    # integer path floors to 0 (a size_zero drop), fractional keeps 0.8.
    sig = _signal(entry=500.0, stop=490.0)
    equity = 1_000.0
    assert risk.position_size(equity, sig) == 0
    assert risk.position_size(equity, sig, fractional=True) == pytest.approx(0.8)


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
# gate: reject_counts (0.5.4 signal-attrition funnel)
# ---------------------------------------------------------------------------


def test_gate_reject_counts_size_zero(monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 5)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 5)
    signals = [_signal(symbol="AAA", entry=100.0, stop=100.0)]  # untradeable -> qty 0
    reject_counts: dict[str, int] = {}
    approved = risk.gate(signals, equity=10_000.0, open_positions=[],
                         trades_today=0, reject_counts=reject_counts)
    assert approved == []
    assert reject_counts == {"size_zero": 1}


def test_gate_reject_counts_sector_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 10)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 10)
    signals = [
        _signal(symbol="NVDA", score=90.0),
        _signal(symbol="AMD", score=80.0),
        _signal(symbol="MU", score=70.0),  # third semis candidate, dropped
    ]
    reject_counts: dict[str, int] = {}
    approved = risk.gate(signals, equity=10_000.0, open_positions=[],
                         trades_today=0, reject_counts=reject_counts)
    assert [s.symbol for s, _ in approved] == ["NVDA", "AMD"]
    assert reject_counts == {"sector_cap": 1}


def test_gate_reject_counts_slots_full(monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 1)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 5)
    signals = [
        _signal(symbol="JPM", score=90.0),
        _signal(symbol="GS", score=80.0),
    ]
    reject_counts: dict[str, int] = {}
    approved = risk.gate(signals, equity=10_000.0, open_positions=[],
                         trades_today=0, reject_counts=reject_counts)
    assert [s.symbol for s, _ in approved] == ["JPM"]
    assert reject_counts == {"slots_full": 1}


def test_gate_reject_counts_default_none_is_unchanged(monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 2)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 5)
    signals = [_signal(symbol="JPM", score=90.0)]
    approved = risk.gate(signals, equity=10_000.0, open_positions=[], trades_today=0)
    assert [s.symbol for s, _ in approved] == ["JPM"]


# ---------------------------------------------------------------------------
# gate: 0.5.3 tuning knobs (fractional sizing, slot overrides)
# ---------------------------------------------------------------------------


def test_gate_fractional_keeps_sub_share_above_min_notional(monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 5)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 5)
    # entry=500 at $1k equity -> 0.8 shares ($400 notional): integer gate drops
    # it as size_zero, fractional gate keeps it.
    signals = [_signal(symbol="AAA", entry=500.0, stop=490.0, score=50.0)]
    integer = risk.gate(signals, equity=1_000.0, open_positions=[], trades_today=0)
    assert integer == []
    frac = risk.gate(signals, equity=1_000.0, open_positions=[], trades_today=0,
                     fractional=True)
    assert len(frac) == 1
    assert frac[0][1] == pytest.approx(0.8)


def test_gate_fractional_rejects_below_min_notional(monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 5)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 5)
    # entry=100 stop=1 (rps=99), equity=$10: risk bound = 0.03*10/99 ≈ 0.00303
    # shares ≈ $0.30 notional, below Alpaca's $1 floor -> size_zero even
    # in fractional mode.
    signals = [_signal(symbol="AAA", entry=100.0, stop=1.0, score=50.0)]
    reject_counts: dict[str, int] = {}
    frac = risk.gate(signals, equity=10.0, open_positions=[], trades_today=0,
                     fractional=True, reject_counts=reject_counts)
    assert frac == []
    assert reject_counts == {"size_zero": 1}


def test_gate_max_open_positions_override(monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 3)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 3)
    signals = [
        _signal(symbol="JPM", score=90.0),
        _signal(symbol="GS", score=80.0),
        _signal(symbol="V", score=70.0),
    ]
    # Override caps at 1 slot despite config allowing 3.
    approved = risk.gate(signals, equity=10_000.0, open_positions=[], trades_today=0,
                         max_open_positions=1, max_trades_per_day=1)
    assert [s.symbol for s, _ in approved] == ["JPM"]


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
