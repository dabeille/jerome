"""Symbol -> sector map for the risk gate's sector-concentration cap (plan §5).

Hand-maintained, covers config.ETF_UNIVERSE and config.CORE_STOCKS. Dynamic-
universe symbols won't be here — that's fine, they're just uncapped.
"""

from __future__ import annotations

SECTOR_BY_SYMBOL: dict[str, str] = {
    # ETFs
    "SPY": "index", "QQQ": "index", "IWM": "index", "ARKK": "index",
    "XLK": "tech",
    "XLF": "financials", "KRE": "financials",
    "XLE": "energy",
    "XLV": "healthcare", "XBI": "healthcare",
    "XLI": "industrials",
    "SMH": "semis",
    "GDX": "materials",

    # Semis
    "NVDA": "semis", "AMD": "semis", "AVGO": "semis", "MU": "semis",
    "QCOM": "semis", "TXN": "semis", "MRVL": "semis", "ARM": "semis",
    "SMCI": "semis", "INTC": "semis",

    # Tech
    "AAPL": "tech", "MSFT": "tech", "GOOGL": "tech", "META": "tech",
    "CRM": "tech", "ORCL": "tech", "ADBE": "tech", "PLTR": "tech",
    "APP": "tech", "CRWD": "tech",

    # Financials
    "JPM": "financials", "BAC": "financials", "GS": "financials",
    "V": "financials", "MA": "financials", "PYPL": "financials",
    "SOFI": "financials", "HOOD": "financials", "COIN": "financials",
    "MSTR": "financials",

    # Energy
    "XOM": "energy", "CVX": "energy", "VST": "energy",

    # Healthcare
    "UNH": "healthcare", "LLY": "healthcare",

    # Consumer
    "AMZN": "consumer", "TSLA": "consumer", "COST": "consumer",
    "HD": "consumer", "NKE": "consumer", "MCD": "consumer",
    "DIS": "consumer", "NFLX": "consumer", "UBER": "consumer",
    "ABNB": "consumer", "SHOP": "consumer", "XYZ": "consumer",

    # Industrials
    "CAT": "industrials", "DE": "industrials", "BA": "industrials",
    "GE": "industrials",
}


def sector_of(symbol: str) -> str:
    """Empty string = unknown / uncapped."""
    return SECTOR_BY_SYMBOL.get(symbol, "")
