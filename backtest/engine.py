"""Backtest engine (step 0.5.2). Skeleton — implementation TODO.

Design: simple daily-bar event loop (not vectorbt/numba — both are painful
on ARM, and at 1-3 trades/day a plain loop over 5y of dailies runs in
seconds even on a Pi).

- Walk-forward: tune on 2019-2023, validate untouched on 2024-mid-2026
- Costs: $0 commission, 0.05% slippage per side
- Reuses bot.risk.position_size and the same Signal objects the live bot
  uses, so backtest and production share one code path
- Report: expectancy, win rate, max drawdown, trades/day, equity curve CSV
"""

from dataclasses import dataclass

SLIPPAGE = 0.0005


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry: float
    exit_date: str = ""
    exit: float = 0.0
    qty: int = 0
    strategy: str = ""

    @property
    def pnl(self) -> float:
        return (self.exit - self.entry) * self.qty


def run_backtest(start: str, end: str) -> list[Trade]:
    """TODO(0.5.2): iterate day by day over cached bars, generate signals via
    bot.signals modules, simulate bracket fills (stop/target vs next bars)."""
    raise NotImplementedError("Step 0.5.2")
