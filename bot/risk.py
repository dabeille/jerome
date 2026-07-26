"""Risk gate (plan §5). Every trade passes through here; no exceptions."""

from __future__ import annotations  # py3.9 compat

from datetime import datetime, timezone

from bot import config, journal, sectors
from bot.signals import Signal


def kill_switch_active() -> bool:
    return config.KILL_FILE.exists()


def position_size(equity: float, sig: Signal) -> int:
    """Shares such that (entry - stop) * shares ≈ RISK_PER_TRADE * equity,
    capped by MAX_POSITION_PCT of equity. Returns 0 if untradeable."""
    if sig.risk_per_share <= 0 or sig.entry <= 0:
        return 0
    by_risk = (equity * config.RISK_PER_TRADE) / sig.risk_per_share
    by_size = (equity * config.MAX_POSITION_PCT) / sig.entry
    return max(int(min(by_risk, by_size)), 0)


def daily_loss_breached(equity_now: float) -> bool:
    today = datetime.now(timezone.utc).date().isoformat()
    start = journal.equity_at_day_start(today)
    return start > 0 and (equity_now - start) / start <= config.DAILY_LOSS_LIMIT


def drawdown_breached(equity_now: float) -> bool:
    hwm = journal.high_water_mark()
    return hwm > 0 and (equity_now - hwm) / hwm <= config.DRAWDOWN_HALT


def gate(signals: list[Signal], equity: float,
         open_positions: list[str], trades_today: int,
         reject_counts: dict[str, int] | None = None) -> list[tuple[Signal, int]]:
    """Return (signal, qty) pairs that pass all checks, best score first.

    ``reject_counts``, if given, is incremented in place with the reason each
    dropped signal was dropped: vetoed, already_held, size_zero, sector_cap,
    slots_full. Default None leaves gate()'s behaviour unchanged.
    """
    approved: list[tuple[Signal, int]] = []
    slots = min(config.MAX_OPEN_POSITIONS - len(open_positions),
                config.MAX_TRADES_PER_DAY - trades_today)
    sector_counts: dict[str, int] = {}
    for sym in open_positions:
        sec = sectors.sector_of(sym)
        if sec:
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
    ranked = sorted(signals, key=lambda s: s.score, reverse=True)
    for i, sig in enumerate(ranked):
        if len(approved) >= max(slots, 0):
            # Every remaining candidate is rejected for this same reason —
            # tally all of them, not just this one, so a funnel report can
            # reconcile signals in against drops + fills.
            if reject_counts is not None:
                reject_counts["slots_full"] = (
                    reject_counts.get("slots_full", 0) + (len(ranked) - i)
                )
            break
        if sig.vetoed:
            if reject_counts is not None:
                reject_counts["vetoed"] = reject_counts.get("vetoed", 0) + 1
            continue
        if sig.symbol in open_positions:
            if reject_counts is not None:
                reject_counts["already_held"] = reject_counts.get("already_held", 0) + 1
            continue
        qty = position_size(equity, sig)
        if qty < 1:
            if reject_counts is not None:
                reject_counts["size_zero"] = reject_counts.get("size_zero", 0) + 1
            continue
        sec = sectors.sector_of(sig.symbol)
        if sec and sector_counts.get(sec, 0) >= config.MAX_PER_SECTOR:
            if reject_counts is not None:
                reject_counts["sector_cap"] = reject_counts.get("sector_cap", 0) + 1
            continue
        approved.append((sig, qty))
        if sec:
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
    return approved
