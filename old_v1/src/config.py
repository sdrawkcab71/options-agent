"""
Agent configuration constants.
Adjust CAPITAL and WATCHLIST to match your account.
"""

# ── Account ──────────────────────────────────────────────
CAPITAL: float = 5_000.0
MAX_SINGLE_TRADE_PCT: float = 0.20       # 20% cap per trade
STOP_LOSS_PCT: float = 0.50              # exit at -50% of premium

# ── Scoring thresholds ────────────────────────────────────
MIN_SCORE_TO_TRADE: int = 15
SMALL_TRADE_SCORE_MAX: int = 19          # 15–19 → small size
SMALL_TRADE_PCT: float = 0.075           # 7.5% (midpoint of 5–10%)
STANDARD_TRADE_PCT: float = 0.175        # 17.5% (midpoint of 15–20%)

# ── Scan universe ─────────────────────────────────────────
WATCHLIST: list[str] = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META",
    "AMZN", "GOOGL", "NFLX", "JPM", "BAC", "GLD", "TLT",
    "PLTR", "MARA", "COIN", "SOFI", "HOOD",
]

# ── Flow signal filters ───────────────────────────────────
MIN_VOL_OI_RATIO: float = 3.0
MIN_PREMIUM_USD: float = 50_000.0
MIN_DTE: int = 7
MAX_DTE: int = 21
MAX_OTM_PCT: float = 0.05                # max 5% out of the money
MAX_BID_ASK_SPREAD: float = 0.20
DELTA_MIN: float = 0.30
DELTA_MAX: float = 0.55

# ── VIX regime thresholds ─────────────────────────────────
VIX_LOW: float = 15.0
VIX_NEUTRAL: float = 20.0
VIX_ELEVATED: float = 25.0              # above this → prefer spreads
