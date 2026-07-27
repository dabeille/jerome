"""Offline test for the sweep holdout guard (plan 2f): _guard refuses any
window ending past the 2023-12-31 training boundary so the 2024-2026
validation window can only be run by the deliberate, separate validation
path — never accidentally by the tuning sweep. No network/keys.
"""

from __future__ import annotations

import pytest

from backtest import sweep


def test_guard_raises_past_boundary():
    with pytest.raises(SystemExit):
        sweep._guard("2024-01-01")


def test_guard_raises_well_past_boundary():
    with pytest.raises(SystemExit):
        sweep._guard("2026-06-30")


def test_guard_passes_at_boundary():
    sweep._guard("2023-12-31")  # exactly the boundary is allowed


def test_guard_passes_before_boundary():
    sweep._guard("2022-06-01")
