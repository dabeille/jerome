"""Shared signal type. Each strategy module exposes scan(bars) -> list[Signal]."""

from dataclasses import dataclass, field


@dataclass
class Signal:
    symbol: str
    side: str          # "buy" only, for now (no shorting per plan §4)
    score: float       # 0-100 conviction
    entry: float       # intended entry price
    stop: float        # hard stop (broker-side bracket leg)
    target: float      # take-profit (bracket leg)
    strategy: str      # "momentum" | "meanrev"
    reasoning: str = ""
    vetoed: bool = False
    veto_reason: str = ""
    veto_source: str = ""   # "earnings" (deterministic) | "llm" — see §9

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)
