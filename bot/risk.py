"""Risk gate (plan §5). Every trade passes through here; no exceptions."""

from datetime import datetime, timezone

from bot import config, journal
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
         open_positions: list[str], trades_today: int) -> list[tuple[Signal, int]]:
    """Return (signal, qty) pairs that pass all checks, best score first."""
    approved: list[tuple[Signal, int]] = []
    slots = min(config.MAX_OPEN_POSITIONS - len(open_positions),
                config.MAX_TRADES_PER_DAY - trades_today)
    for sig in sorted(signals, key=lambda s: s.score, reverse=True):
        if len(approved) >= max(slots, 0):
            break
        if sig.vetoed or sig.symbol in open_positions:
            continue
        qty = position_size(equity, sig)
        if qty < 1:
            continue
        # TODO(0.5.1): sector-concentration check (MAX_PER_SECTOR)
        approved.append((sig, qty))
    return approved
