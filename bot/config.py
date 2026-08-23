"""Central configuration. No secrets in this file — secrets live in .env."""

import json
import os
import stat
import sys
import time
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

# --- Alerts (Resend email, plan 1.1.4) -------------------------------------
# Failure alerts only — an alert path must never crash a trading run, so
# alerts.py fails open (stderr warning + no-op) if these are unset.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")
# resend.dev's shared sender delivers to the account owner without any domain
# setup; swap for your own verified domain later if you want nicer From lines.
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM", "onboarding@resend.dev")
LLM_MODEL = "claude-opus-4-8"   # analyst veto/conviction call (plan §4C)
# One call per run, returning a verdict object keyed by symbol. 1024 was sized
# for the 2-4 candidates of early Phase 1; the week of 2026-08-17 scanned up to
# 19 at once, the response truncated mid-string on three runs, json.loads raised
# and the veto layer failed open in silence. Sized now for a full-universe scan
# (~60 candidates x ~130 chars of verdict) with headroom.
LLM_MAX_TOKENS = 4096

# --- Risk constants (see plan §5) -------------------------------------------
RISK_PER_TRADE = 0.03        # fraction of equity risked per trade
MAX_POSITION_PCT = 0.40      # max single position as fraction of equity
MAX_OPEN_POSITIONS = 3
MAX_PER_SECTOR = 2
DAILY_LOSS_LIMIT = -0.06     # flatten + halt for the day
DRAWDOWN_HALT = -0.20        # halt until manual review
MAX_BUYING_POWER_MULT = 1.0  # never use leverage even if margin grants it
MAX_TRADES_PER_DAY = 3

# --- Strategy set (checkpoint decision 2026-07-27: blended) ------------------
# Which signal modules the daily loop scans. Benching one (e.g. meanrev during
# a retune) or swapping a redesign back in is a .env change, not a code change:
#     ENABLED_STRATEGIES=momentum
# The backtest engine's `strategies` parameter mirrors this knob.
KNOWN_STRATEGIES = ("momentum", "meanrev")
ENABLED_STRATEGIES = tuple(
    s.strip() for s in
    os.getenv("ENABLED_STRATEGIES", ",".join(KNOWN_STRATEGIES)).split(",")
    if s.strip()
)

# --- Paths -------------------------------------------------------------------
DATA_DIR = ROOT / "data"
BARS_DIR = DATA_DIR / "bars"
NEWS_DIR = DATA_DIR / "news"          # cached headlines, one JSON per day
EARNINGS_DIR = DATA_DIR / "earnings"  # cached earnings calendar, one JSON per day
JOURNAL_DB = DATA_DIR / "journal.db"
KILL_FILE = ROOT / "KILL"            # touch this file to halt + liquidate
DASHBOARD = ROOT / "dashboard.md"
# LAN dashboard: only data/public/ is ever served (see README) — never data/
# itself, which holds journal.db and other private files.
PUBLIC_DIR = DATA_DIR / "public"
DASHBOARD_HTML = PUBLIC_DIR / "dashboard.html"
CHAINS_DIR = DATA_DIR / "chains"     # EOD option-chain snapshots (plan 1.2.5)

for d in (DATA_DIR, BARS_DIR, NEWS_DIR, EARNINGS_DIR, PUBLIC_DIR, CHAINS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- Universe (two tiers) -----------------------------------------------------
# CORE: static, always-liquid — mega-caps that anchor the scan plus high-beta
# names with the range the momentum thesis (plan §4A) actually needs.
# DYNAMIC: whatever is currently in play — refreshed weekly by
# scripts/refresh_universe.py into data/dynamic_universe.json (screen rules in
# bot/universe.py). Missing or stale file just means we trade the static tiers.
ETF_UNIVERSE = [
    "SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLI", "SMH",
    "XBI", "KRE", "GDX", "ARKK",  # less-correlated dip feedstock for meanrev
]
CORE_STOCKS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO",
    "JPM", "BAC", "GS", "V", "MA", "XOM", "CVX", "UNH", "LLY",
    "COST", "HD", "NKE", "MCD", "DIS", "NFLX", "CRM", "ORCL", "ADBE",
    "INTC", "MU", "QCOM", "TXN", "CAT", "DE", "BA", "GE", "UBER", "ABNB",
    "PLTR", "COIN", "SHOP", "XYZ", "PYPL", "SOFI",
    "HOOD", "MSTR", "ARM", "CRWD", "MRVL", "SMCI", "APP", "VST",
]

DYNAMIC_UNIVERSE_FILE = DATA_DIR / "dynamic_universe.json"
DYNAMIC_UNIVERSE_MAX_AGE_DAYS = 10  # warn past this (see validate())


def _load_dynamic_universe() -> "list[str]":
    """Scanner output minus anything already in a static tier."""
    if not DYNAMIC_UNIVERSE_FILE.exists():
        return []
    try:
        payload = json.loads(DYNAMIC_UNIVERSE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    static = set(ETF_UNIVERSE) | set(CORE_STOCKS)
    return [s for s in payload.get("symbols", [])
            if isinstance(s, str) and s not in static]


DYNAMIC_STOCKS = _load_dynamic_universe()
STOCK_UNIVERSE = CORE_STOCKS + DYNAMIC_STOCKS
UNIVERSE = ETF_UNIVERSE + STOCK_UNIVERSE

# --- Options-chain snapshots (plan 1.2.5) -----------------------------------
# Collection only in Phase 1 — no strategy reads this yet; we build IV history
# ahead of the options Addendum. Liquid, tight-spread underlyings only.
OPTION_UNDERLYINGS = [
    "SPY", "QQQ", "IWM", "SMH", "XBI",
    "AAPL", "NVDA", "TSLA", "AMD", "META", "AMZN",
]


def validate() -> None:
    """Fail loudly on misconfiguration. Called by main() before anything else."""
    if MODE not in VALID_MODES:
        sys.exit(f"BOT_MODE '{MODE}' invalid; must be one of {VALID_MODES}")
    if MODE != "backtest" and not (ALPACA_KEY_ID and ALPACA_SECRET):
        sys.exit(f"Missing Alpaca keys for mode '{MODE}' — check .env")
    unknown = set(ENABLED_STRATEGIES) - set(KNOWN_STRATEGIES)
    if not ENABLED_STRATEGIES or unknown:
        sys.exit(f"ENABLED_STRATEGIES {ENABLED_STRATEGIES!r} invalid — must "
                 f"be a non-empty subset of {KNOWN_STRATEGIES}")
    if IS_LIVE:
        print(f"*** {MODE.upper()} MODE — REAL MONEY ***", file=sys.stderr)
    _check_dynamic_universe()
    _check_env_perms()


def _check_dynamic_universe() -> None:
    """The dynamic tier is optional, but it shouldn't rot silently."""
    if not DYNAMIC_UNIVERSE_FILE.exists():
        print(
            "NOTE: no dynamic universe file — trading static tiers only. "
            "Run: python -m scripts.refresh_universe",
            file=sys.stderr,
        )
        return
    age_days = (time.time() - DYNAMIC_UNIVERSE_FILE.stat().st_mtime) / 86400
    if age_days > DYNAMIC_UNIVERSE_MAX_AGE_DAYS:
        print(
            f"WARNING: dynamic universe is {age_days:.0f} days old — "
            "run: python -m scripts.refresh_universe",
            file=sys.stderr,
        )


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
