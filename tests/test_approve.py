"""Offline tests for the human approval gate (plan 2f). No network — stdin is
replaced with a scripted iterator via monkeypatch on builtins.input.
"""

from __future__ import annotations

from bot import approve
from bot.signals import Signal


def _sig(symbol):
    return Signal(symbol=symbol, side="buy", score=80.0, entry=100.0,
                  stop=95.0, target=115.0, strategy="momentum")


def test_request_approval_keeps_only_yes(monkeypatch):
    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    proposals = [(_sig("AAPL"), 5), (_sig("NVDA"), 3)]
    approved = approve.request_approval(proposals)
    assert [s.symbol for s, _ in approved] == ["AAPL"]


def test_request_approval_case_insensitive_and_whitespace(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "  Y  ")
    approved = approve.request_approval([(_sig("AAPL"), 5)])
    assert [s.symbol for s, _ in approved] == ["AAPL"]


def test_request_approval_anything_else_skips(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    approved = approve.request_approval([(_sig("AAPL"), 5)])
    assert approved == []


def test_request_approval_empty_proposals(monkeypatch):
    called = []
    monkeypatch.setattr("builtins.input", lambda prompt="": called.append(1) or "y")
    assert approve.request_approval([]) == []
    assert called == []  # never prompted
