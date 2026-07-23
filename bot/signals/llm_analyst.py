"""LLM analyst (plan §4C): filter/veto layer, never the primary signal.

Reads candidate signals + recent headlines, vetoes trades into binary
events (earnings tonight, FDA decisions, macro releases), and may adjust
conviction scores.
"""

from __future__ import annotations  # py3.9 compat

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

    candidates = [s for s in signals if not s.vetoed]
    if not candidates:
        return signals

    try:
        verdicts = _ask_llm(candidates, headlines)
    except Exception as exc:  # fail-open — never block trading on an LLM hiccup
        print(f"LLM analyst call failed, skipping review: {exc}")
        return signals

    _apply(candidates, verdicts)
    return signals


def _build_prompt(candidates: list[Signal], headlines: dict[str, list[str]]) -> str:
    """One prompt listing each candidate plus its recent headlines."""
    lines = [
        "You are a risk-averse trading analyst. Review these candidate trades "
        "and their recent headlines. Veto only trades walking into a binary "
        "event landmine (earnings, FDA decision, major macro release) that "
        "isn't already accounted for. Otherwise, nudge conviction up or down "
        "based on headline sentiment/relevance.",
        "",
        "Return ONLY a JSON object keyed by symbol, no other text:",
        '{"SYM": {"veto": bool, "reason": str, "score_adjust": int}}',
        "score_adjust must be clamped to -20..20.",
        "",
        "Candidates:",
    ]
    for s in candidates:
        lines.append(
            f"- {s.symbol} ({s.strategy}, side={s.side}): entry={s.entry} "
            f"stop={s.stop} target={s.target} score={s.score} "
            f"reasoning={s.reasoning!r}"
        )
        for h in headlines.get(s.symbol, []):
            lines.append(f"    headline: {h}")
    return "\n".join(lines)


def _ask_llm(candidates: list[Signal], headlines: dict[str, list[str]]) -> dict[str, dict]:
    """Single Claude call; returns the parsed per-symbol verdict dict."""
    import json

    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        messages=[{"role": "user", "content": _build_prompt(candidates, headlines)}],
    )
    text = response.content[0].text.strip()
    return json.loads(text)


def _apply(candidates: list[Signal], verdicts: dict[str, dict]) -> None:
    for s in candidates:
        verdict = verdicts.get(s.symbol)
        if verdict is None:
            continue
        if verdict.get("veto"):
            s.vetoed = True
            s.veto_reason = verdict.get("reason", "")
        else:
            adjust = max(-20, min(20, int(verdict.get("score_adjust", 0))))
            s.score = max(0, min(100, s.score + adjust))
            reason = verdict.get("reason")
            if reason:
                s.reasoning = f"{s.reasoning}; {reason}" if s.reasoning else reason
