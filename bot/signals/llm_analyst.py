"""LLM analyst (plan §4C): filter/veto layer, never the primary signal.

Reads candidate signals + recent headlines, vetoes trades into binary
events (earnings tonight, FDA decisions, macro releases), and may adjust
conviction scores. Step 0.4.3 — prompt & parsing TODO.
"""

from bot import config
from bot.signals import Signal


def review(signals: list[Signal], headlines: dict[str, list[str]],
           earnings_soon: set[str]) -> list[Signal]:
    """Apply cheap deterministic vetoes first, then (optionally) the LLM.

    headlines: symbol -> recent headline strings
    earnings_soon: symbols reporting within the hold horizon
    """
    for s in signals:
        if s.symbol in earnings_soon:
            s.vetoed = True
            s.veto_reason = "earnings within hold window"

    if not config.ANTHROPIC_API_KEY:
        return signals  # deterministic vetoes only; LLM layer disabled

    # TODO(0.4.3): one batched Claude call over remaining candidates:
    #   input: signal reasoning + headlines; output: per-symbol
    #   {veto: bool, reason: str, score_adjust: -20..+20}
    # Keep it to ONE call per run to hold cost at pennies/day.
    return signals
