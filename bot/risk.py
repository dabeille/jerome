"""Risk gate (plan §5). Every trade passes through here; no exceptions."""

from __future__ import annotations  # py3.9 compat

from datetime import datetime, timezone

from bot import config, journal, sectors
from bot.signals import Signal


def kill_switch_active() -> bool:
    return config.KILL_FILE.exists()


def position_size(equity: float, sig: Signal, fractional: bool = False) -> float:
    """Shares such that (entry - stop) * shares ≈ RISK_PER_TRADE * equity,
    capped by MAX_POSITION_PCT of equity. Returns 0 if untradeable.

    ``fractional`` is a backtest-only knob (Alpaca bracket orders — the live
    path — require whole-share quantities, see broker.submit_bracket): when
    set, the whole-share truncation is skipped and a float quantity is
    returned. The default keeps the integer behaviour the live bot relies on.
    """
    if sig.risk_per_share <= 0 or sig.entry <= 0:
        return 0
    by_risk = (equity * config.RISK_PER_TRADE) / sig.risk_per_share
    by_size = (equity * config.MAX_POSITION_PCT) / sig.entry
    raw = min(by_risk, by_size)
    return max(raw if fractional else int(raw), 0)


def daily_loss_breached(equity_now: float) -> bool:
    today = datetime.now(timezone.utc).date().isoformat()
    start = journal.equity_at_day_start(today)
    return start > 0 and (equity_now - start) / start <= config.DAILY_LOSS_LIMIT


def drawdown_breached(equity_now: float) -> bool:
    hwm = journal.high_water_mark()
    return hwm > 0 and (equity_now - hwm) / hwm <= config.DRAWDOWN_HALT


MIN_NOTIONAL = 1.0  # Alpaca's real minimum order value; the fractional floor


def gate(signals: list[Signal], equity: float,
         open_positions: list[str], trades_today: int,
         reject_counts: dict[str, int] | None = None,
         fractional: bool = False,
         max_open_positions: int | None = None,
         max_trades_per_day: int | None = None) -> list[tuple[Signal, float]]:
    """Return (signal, qty) pairs that pass all checks, best score first.

    ``reject_counts``, if given, is incremented in place with the reason each
    dropped signal was dropped: vetoed, already_held, size_zero, sector_cap,
    slots_full. Default None leaves gate()'s behaviour unchanged.

    ``fractional`` (backtest-only, see position_size) allows sub-share
    quantities; a signal is then only size-zero below Alpaca's $1 minimum
    notional. ``max_open_positions`` / ``max_trades_per_day`` override the
    config constants for backtest tuning sweeps; None uses config as before.
    """
    max_open = config.MAX_OPEN_POSITIONS if max_open_positions is None else max_open_positions
    max_trades = config.MAX_TRADES_PER_DAY if max_trades_per_day is None else max_trades_per_day
    approved: list[tuple[Signal, float]] = []
    slots = min(max_open - len(open_positions), max_trades - trades_today)
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
        qty = position_size(equity, sig, fractional=fractional)
        too_small = (qty * sig.entry < MIN_NOTIONAL) if fractional else (qty < 1)
        if too_small:
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
