"""Central configuration. No secrets in this file — secrets live in .env."""

import os
import stat
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE)

# --- Mode -----------------------------------------------------------------
# backtest: no broker connection at all
# paper:    full auto against Alpaca paper endpoint
# approve:  LIVE endpoint, every entry requires human approval
# live:     LIVE endpoint, full auto
MODE = os.getenv("BOT_MODE", "paper").lower()
VALID_MODES = {"backtest", "paper", "approve", "live"}
IS_LIVE = MODE in {"approve", "live"}

# --- Broker keys (selected by mode) ----------------------------------------
if IS_LIVE:
    ALPACA_KEY_ID = os.getenv("ALPACA_LIVE_KEY_ID", "")
    ALPACA_SECRET = os.getenv("ALPACA_LIVE_SECRET", "")
else:
    ALPACA_KEY_ID = os.getenv("ALPACA_PAPER_KEY_ID", "")
    ALPACA_SECRET = os.getenv("ALPACA_PAPER_SECRET", "")

FINNHUB_KEY = os.getenv("FINNHUB_KEY", "")
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Risk constants (see plan §5) -------------------------------------------
RISK_PER_TRADE = 0.03        # fraction of equity risked per trade
MAX_POSITION_PCT = 0.40      # max single position as fraction of equity
MAX_OPEN_POSITIONS = 3
MAX_PER_SECTOR = 2
DAILY_LOSS_LIMIT = -0.06     # flatten + halt for the day
DRAWDOWN_HALT = -0.20        # halt until manual review
MAX_BUYING_POWER_MULT = 1.0  # never use leverage even if margin grants it
MAX_TRADES_PER_DAY = 3

# --- Universe ----------------------------------------------------------------
ETF_UNIVERSE = ["SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLI", "SMH"]
STOCK_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO",
    "JPM", "BAC", "GS", "V", "MA", "XOM", "CVX", "UNH", "LLY", "JNJ", "MRK",
    "COST", "WMT", "HD", "NKE", "MCD", "DIS", "NFLX", "CRM", "ORCL", "ADBE",
    "INTC", "MU", "QCOM", "TXN", "CAT", "DE", "BA", "GE", "UBER", "ABNB",
    "PLTR", "COIN", "SHOP", "XYZ", "PYPL", "SOFI", "F", "GM", "T", "VZ",
]
UNIVERSE = ETF_UNIVERSE + STOCK_UNIVERSE

# --- Paths -------------------------------------------------------------------
DATA_DIR = ROOT / "data"
BARS_DIR = DATA_DIR / "bars"
NEWS_DIR = DATA_DIR / "news"          # cached headlines, one JSON per day
EARNINGS_DIR = DATA_DIR / "earnings"  # cached earnings calendar, one JSON per day
JOURNAL_DB = DATA_DIR / "journal.db"
KILL_FILE = ROOT / "KILL"            # touch this file to halt + liquidate
DASHBOARD = ROOT / "dashboard.md"

for d in (DATA_DIR, BARS_DIR, NEWS_DIR, EARNINGS_DIR):
    d.mkdir(parents=True, exist_ok=True)


def validate() -> None:
    """Fail loudly on misconfiguration. Called by main() before anything else."""
    if MODE not in VALID_MODES:
        sys.exit(f"BOT_MODE '{MODE}' invalid; must be one of {VALID_MODES}")
    if MODE != "backtest" and not (ALPACA_KEY_ID and ALPACA_SECRET):
        sys.exit(f"Missing Alpaca keys for mode '{MODE}' — check .env")
    if IS_LIVE:
        print(f"*** {MODE.upper()} MODE — REAL MONEY ***", file=sys.stderr)
    _check_env_perms()


def _check_env_perms() -> None:
    """Warn if .env is readable by other users (Pi hardening)."""
    if ENV_FILE.exists() and os.name == "posix":
        mode = stat.S_IMODE(ENV_FILE.stat().st_mode)
        if mode & 0o077:
            print(
                f"WARNING: {ENV_FILE} is readable by group/others "
                f"(mode {oct(mode)}). Run: chmod 600 .env",
                file=sys.stderr,
            )
