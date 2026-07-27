"""Entry point. Run via cron/systemd on the Pi:

    python -m bot.main morning    # ~9:00 ET: scan, propose/execute entries
    python -m bot.main midday     # ~12:30 ET: optional second scan
    python -m bot.main close      # ~15:45 ET: manage exits, snapshot equity
    python -m bot.main resume     # after a drawdown halt: operator-gated resume

Order of operations per run: kill switch -> trading-day check -> circuit
breakers -> naked-position check -> signals -> LLM review -> risk gate ->
(approval) -> execute -> journal (incl. funnel) + dashboard.

Half-days (early close) need no special handling: the market is a trading day,
so runs proceed; the close run after an early close simply snapshots.
"""

import sys
import traceback

from bot import alerts, approve, broker, config, data, journal, risk
from bot.signals import llm_analyst, meanrev, momentum


def run(session: str) -> None:
    config.validate()

    if session == "resume":
        _resume()
        return

    # 1. Kill switch — checked before anything else touches the broker.
    if risk.kill_switch_active():
        print("KILL file present — flattening and exiting.")
        if config.MODE != "backtest":
            broker.flatten_all("kill switch")
        return

    if config.MODE == "backtest":
        sys.exit("Use backtest/run.py for backtests, not bot.main")

    # 2. Trading-day check — after the kill switch (which must always run), but
    # before any trading work. Uses the broker calendar, not get_clock(): the
    # 9:00 run fires before the 9:30 open on perfectly good days.
    if not broker.is_trading_day():
        print(f"{session}: not a trading day (broker calendar) — no-op.")
        return

    equity = broker.equity()
    journal.log_equity(equity, float(broker.account().cash), f"{session} start")

    # 3. Circuit breakers (plan §5).
    if risk.drawdown_breached(equity):
        broker.flatten_all("drawdown circuit breaker")
        journal.log_decision(session, "*", "risk", "HALT-DRAWDOWN",
                             reasoning="-20% from high-water mark")
        print("Halted on drawdown circuit breaker. After reviewing the "
             "journal together, run: python -m bot.main resume")
        return
    if risk.daily_loss_breached(equity):
        broker.flatten_all("daily loss limit")
        journal.log_decision(session, "*", "risk", "HALT-DAILY",
                             reasoning="-6% on the day")
        return

    # 4. Naked-position guard (plan §5): alert + journal only, no auto-remediation.
    _check_naked_positions(session)

    if session == "close":
        _write_dashboard(session)
        return  # exits are bracket-managed broker-side; close run = snapshot

    # 5. Signals — modules selected by config.ENABLED_STRATEGIES, so a
    # strategy can be benched (or a redesign swapped in) via .env alone.
    # Scanners are resolved here, not at import, so the set is per-run.
    scanners = {"momentum": momentum.scan, "meanrev": meanrev.scan}
    bars = data.get_daily_bars(config.UNIVERSE, refresh=True)
    signals = []
    for name in config.ENABLED_STRATEGIES:
        signals += scanners[name](bars)
    for s in signals:
        journal.log_decision(session, s.symbol, s.strategy, "candidate",
                             s.score, s.entry, s.stop, s.target,
                             reasoning=s.reasoning)

    # 6. LLM analyst review (veto layer).
    syms = [s.symbol for s in signals]
    signals = llm_analyst.review(signals, data.get_headlines(syms),
                                 data.earnings_within(syms))

    # 7. Risk gate — trades_today from real broker orders (plan 1.1.5), and
    # reject_counts captured so the live funnel matches the backtest's shape.
    reject_counts: dict[str, int] = {}
    proposals = risk.gate(signals, equity, broker.open_position_symbols(),
                          trades_today=broker.entries_today(),
                          reject_counts=reject_counts)

    # 8. Approval gate (Phase 2 only), then execute.
    if config.MODE == "approve":
        proposals = approve.request_approval(proposals)
    entered = 0
    for sig, qty in proposals:
        order_id = broker.submit_bracket(sig, qty)
        entered += 1
        journal.log_decision(session, sig.symbol, sig.strategy, "entered",
                             sig.score, sig.entry, sig.stop, sig.target,
                             qty, f"order {order_id}")

    # 9. Live signal funnel (plan 1.1.5) — one row per run, comparable to the
    # backtest funnel: signals in == entered + approval-skipped + all drops.
    journal.log_funnel(session, {
        "strategies": list(config.ENABLED_STRATEGIES),
        "signals_generated": len(signals),
        "approved": len(proposals),
        "entered": entered,
        **reject_counts,
    })

    _write_dashboard(session)


def _resume() -> None:
    """Operator-gated recovery from a drawdown halt (plan §5: "we review the
    journal together before resuming"). Requires typing RESUME on stdin, so a
    cron-triggered session can never trip this — the halt stays a human gate,
    it just now has a door."""
    if config.MODE == "backtest":
        sys.exit("resume is a live/paper session — a backtest models this "
                 "with run_backtest(..., resume_after_days=N) instead")

    equity = broker.equity()
    acct = broker.account()
    hwm = journal.high_water_mark()
    drawdown = (equity - hwm) / hwm if hwm > 0 else 0.0

    print(f"Equity: ${equity:,.2f}  HWM: ${hwm:,.2f}  Drawdown: {drawdown:+.1%}")
    print("Recent HALT-DRAWDOWN decisions:")
    for ts, symbol, reasoning in journal.recent_decisions("HALT-DRAWDOWN"):
        print(f"  {ts}  {symbol}  {reasoning}")

    typed = input("Type RESUME to clear the halt and rebase the "
                 "high-water mark: ")
    if typed.strip() != "RESUME":
        print("Not resumed.")
        return

    journal.mark_resume(equity, float(acct.cash))
    journal.log_decision("resume", "*", "risk", "HALT-RESUMED",
                         reasoning=f"operator resumed at equity {equity:,.2f}")
    print("Resumed — high-water mark rebased to current equity.")


def _check_naked_positions(session: str) -> None:
    """Every session: page if any open position lacks a live broker-side stop
    (plan §5). Alert-only — a false positive that auto-liquidated would be
    worse than a loud page you clear with one `touch KILL`."""
    naked = broker.positions_without_stops()
    if not naked:
        return
    joined = ", ".join(naked)
    journal.log_decision(session, ",".join(naked), "risk", "NAKED-POSITION",
                         reasoning="open position(s) without a live stop")
    alerts.alert(f"bot {session}: NAKED POSITION — {joined}",
                 f"Open without a live broker-side stop: {joined}. "
                 f"Review now; `touch KILL` to flatten everything.")


def _write_dashboard(session: str) -> None:
    acct = broker.account()
    positions = broker.client().get_all_positions()
    pos_lines = "\n".join(
        f"- {p.symbol}: {p.qty} @ {p.avg_entry_price} "
        f"(P&L {float(p.unrealized_pl):+.2f})"
        for p in positions
    ) or "(none)"
    account_summary = (
        f"Equity: ${float(acct.equity):,.2f} · Cash: ${float(acct.cash):,.2f} "
        f"· last run: {session}"
    )
    journal.write_dashboard(
        account_summary, pos_lines, "See data/journal.db (decisions table).")
    journal.write_dashboard_html(
        account_summary,
        [(p.symbol, p.qty, p.avg_entry_price, f"{float(p.unrealized_pl):+.2f}")
         for p in positions],
    )


def _run_with_alerts(session: str) -> None:
    """Wrap run() so an unhandled exception pages the operator before it
    propagates. Re-raises so the traceback still reaches data/cron.log."""
    try:
        run(session)
    except Exception:
        alerts.alert(f"bot {session} CRASHED", traceback.format_exc()[-1500:])
        raise


if __name__ == "__main__":
    _run_with_alerts(sys.argv[1] if len(sys.argv) > 1 else "morning")
