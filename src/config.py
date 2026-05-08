"""Global configuration constants for the Options Agent V2 (credit spread / iron condor strategy)."""

# ── Account ──────────────────────────────────────────────────────────────────
CAPITAL: float = 5_000.0
MAX_RISK_PCT: float = 0.05          # Max 5% of account at risk per trade ($250 on $5k)

# ── Credit spread entry rules ─────────────────────────────────────────────────
MIN_CREDIT: float = 0.50            # Minimum net credit to bother entering (per share)
DEFAULT_SPREAD_WIDTH: float = 5.0   # Default short/long strike distance (SPX $5 increments)

# ── Exit rules ────────────────────────────────────────────────────────────────
STOP_LOSS_MULTIPLIER: float = 2.0   # Stop out when spread value reaches 2× credit received
PROFIT_TARGET_PCT: float = 0.50     # Close at 50% of max credit

# ── Strike selection ──────────────────────────────────────────────────────────
DEFAULT_SHORT_DELTA: float = 0.15   # Target ~85% probability of expiring OTM
MIN_WIN_RATE: float = 0.80          # Never recommend < 80% historical win rate
WIDER_STRIKE_EVENTS: list[str] = ["FOMC", "CPI", "NFP", "JOBS", "PCE"]

# Delta → historical win rate table (1 - delta ≈ probability of expiring OTM)
DELTA_WIN_RATE_TABLE: dict[float, float] = {
    0.10: 0.90,
    0.15: 0.85,
    0.16: 0.84,
    0.20: 0.80,
    0.25: 0.75,
    0.30: 0.70,
}

# ── VIX regimes ───────────────────────────────────────────────────────────────
VIX_LOW: float = 15.0
VIX_NORMAL: float = 20.0
VIX_ELEVATED: float = 30.0          # RED regime threshold — no trades above this
VIX_CRISIS: float = 30.0

# ── Iron condor ───────────────────────────────────────────────────────────────
ADJUSTMENT_TRIGGER_PCT: float = 0.30  # Adjust when underlying within 30% of short strike
INDEX_PREFERENCE: list[str] = ["SPX", "SPY", "QQQ", "IWM"]

# ── Theta / gamma ────────────────────────────────────────────────────────────
GAMMA_RISK_DTE: int = 21            # Gamma accelerates inside 21 DTE
GAMMA_CRITICAL_DTE: int = 7         # Critical gamma risk zone

# ── Goal engine risk profiles ─────────────────────────────────────────────────
GOAL_CONSERVATIVE_MAX_WEEKLY: float = 0.005   # ≤0.5%/week
GOAL_MODERATE_MAX_WEEKLY: float = 0.015       # 0.5–1.5%/week
GOAL_AGGRESSIVE_MAX_WEEKLY: float = 0.030     # 1.5–3%/week
# >3%/week = VERY_AGGRESSIVE

# ── Options pricing ───────────────────────────────────────────────────────────
RISK_FREE_RATE: float = 0.045       # ~10Y treasury rate; update periodically

# ── DTE windows ──────────────────────────────────────────────────────────────
DEFAULT_DTE_SHORT: int = 7          # ~1-week expiry
DEFAULT_DTE_STANDARD: int = 14      # ~2-week expiry (sweet spot for theta)
DEFAULT_DTE_LONG: int = 30          # Monthly expiry
