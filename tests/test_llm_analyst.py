"""Offline tests for the LLM analyst (0.4.4): deterministic veto, LLM veto/score
paths (mocked, no network), fail-open on error, and prompt contents. Follows
the same pattern as test_signals.py / test_data.py: module-level helpers, bare
assert, monkeypatch.setattr instead of unittest.mock.
"""

from __future__ import annotations

from bot import config
from bot.signals import Signal, llm_analyst


def _signal(symbol="XYZ", score=50.0, reasoning="breakout") -> Signal:
    return Signal(
        symbol=symbol,
        side="buy",
        score=score,
        entry=100.0,
        stop=95.0,
        target=110.0,
        strategy="momentum",
        reasoning=reasoning,
    )


def test_deterministic_earnings_veto_no_key_no_mock():
    sig = _signal(symbol="AAPL")
    out = llm_analyst.review([sig], {}, {"AAPL"})
    assert out == [sig]
    assert sig.vetoed is True
    assert sig.veto_reason == "earnings within hold window"


def test_llm_disabled_passthrough(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    sig = _signal()
    llm_analyst.review([sig], {}, set())
    assert sig.vetoed is False


def test_llm_veto_path(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "dummy-key")
    monkeypatch.setattr(
        llm_analyst, "_ask_llm",
        lambda c, h: {"XYZ": {"veto": True, "reason": "earnings tonight", "score_adjust": 0}},
    )
    sig = _signal()
    llm_analyst.review([sig], {}, set())
    assert sig.vetoed is True
    assert sig.veto_reason == "earnings tonight"


def test_llm_score_adjust_path_clamped(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "dummy-key")

    monkeypatch.setattr(
        llm_analyst, "_ask_llm",
        lambda c, h: {"XYZ": {"veto": False, "reason": "", "score_adjust": 15}},
    )
    up = _signal(score=50.0)
    llm_analyst.review([up], {}, set())
    assert up.score == 65.0

    monkeypatch.setattr(
        llm_analyst, "_ask_llm",
        lambda c, h: {"XYZ": {"veto": False, "reason": "", "score_adjust": -15}},
    )
    down = _signal(score=10.0)
    llm_analyst.review([down], {}, set())
    assert down.score == 0.0  # clamped at 0, not -5


def test_fail_open_on_llm_error(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "dummy-key")

    def _raise(candidates, headlines):
        raise RuntimeError("boom")

    monkeypatch.setattr(llm_analyst, "_ask_llm", _raise)
    sig = _signal()
    out = llm_analyst.review([sig], {}, set())
    assert out == [sig]
    assert sig.vetoed is False


def test_prompt_contents_include_symbol_and_headline():
    sig = _signal(symbol="XYZ")
    prompt = llm_analyst._build_prompt([sig], {"XYZ": ["headline A"]})
    assert "XYZ" in prompt
    assert "headline A" in prompt
