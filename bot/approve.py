"""Human approval gate for Phase 2 ('approve' mode).

v1 is deliberately simple: proposals print to the terminal (run it over SSH
from the Pi) and each entry needs an explicit y. Anything else = skip.
Later this can become a morning message via a Cowork scheduled task.
"""

from bot.signals import Signal


def request_approval(proposals: list[tuple[Signal, int]]) -> list[tuple[Signal, int]]:
    approved: list[tuple[Signal, int]] = []
    if not proposals:
        print("No proposals today.")
        return approved
    print(f"\n=== {len(proposals)} PROPOSED TRADE(S) — REAL MONEY ===\n")
    for sig, qty in proposals:
        print(f"  {sig.symbol}: BUY {qty} @ ~{sig.entry:.2f}  "
              f"stop {sig.stop:.2f}  target {sig.target:.2f}  "
              f"[{sig.strategy}, score {sig.score:.0f}]")
        print(f"  reasoning: {sig.reasoning}\n")
        if input("  approve? [y/N] ").strip().lower() == "y":
            approved.append((sig, qty))
    return approved
