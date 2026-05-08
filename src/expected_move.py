"""
Black-Scholes options math foundation.
All functions are pure — no API calls. Used by every strategy module.
"""
import math
from dataclasses import dataclass
from typing import Literal

import numpy as np

from src.config import RISK_FREE_RATE

ContractType = Literal["call", "put"]


# ── Normal distribution helpers ───────────────────────────────────────────────

def norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ── Black-Scholes core ────────────────────────────────────────────────────────

def bs_d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return float("inf") if S >= K else float("-inf")
    return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))


def bs_d2(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return float("inf") if S >= K else float("-inf")
    return bs_d1(S, K, T, r, sigma) - sigma * math.sqrt(T)


def bs_call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return max(S - K, 0.0)
    d1 = bs_d1(S, K, T, r, sigma)
    d2 = bs_d2(S, K, T, r, sigma)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)


def bs_put_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return max(K - S, 0.0)
    d1 = bs_d1(S, K, T, r, sigma)
    d2 = bs_d2(S, K, T, r, sigma)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_delta(
    S: float, K: float, T: float, r: float, sigma: float, contract_type: ContractType
) -> float:
    if T <= 0:
        if contract_type == "call":
            return 1.0 if S >= K else 0.0
        return -1.0 if S <= K else 0.0
    d1 = bs_d1(S, K, T, r, sigma)
    if contract_type == "call":
        return norm_cdf(d1)
    return norm_cdf(d1) - 1.0


def bs_theta(
    S: float, K: float, T: float, r: float, sigma: float, contract_type: ContractType
) -> float:
    """Daily theta in dollars per share (negative = option loses value each day)."""
    if T <= 0:
        return 0.0
    d1 = bs_d1(S, K, T, r, sigma)
    d2 = bs_d2(S, K, T, r, sigma)
    # Annual theta divided by 365 to get daily
    common = -(S * sigma * norm_pdf(d1)) / (2.0 * math.sqrt(T))
    if contract_type == "call":
        annual = common - r * K * math.exp(-r * T) * norm_cdf(d2)
    else:
        annual = common + r * K * math.exp(-r * T) * norm_cdf(-d2)
    return annual / 365.0


def bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = bs_d1(S, K, T, r, sigma)
    return norm_pdf(d1) / (S * sigma * math.sqrt(T))


def bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Vega per 1% change in IV (divided by 100 from the raw formula)."""
    if T <= 0:
        return 0.0
    d1 = bs_d1(S, K, T, r, sigma)
    return S * norm_pdf(d1) * math.sqrt(T) / 100.0


# ── Probability helpers ───────────────────────────────────────────────────────

def prob_expire_otm(delta: float) -> float:
    """P(expire worthless for seller) ≈ 1 - |delta|."""
    return max(0.0, 1.0 - abs(delta))


def prob_expire_otm_exact(
    S: float, K: float, T: float, r: float, sigma: float, contract_type: ContractType
) -> float:
    """Exact P(expire OTM) = N(-d2) for calls, N(d2) for puts."""
    if T <= 0:
        if contract_type == "call":
            return 1.0 if S < K else 0.0
        return 1.0 if S > K else 0.0
    d2 = bs_d2(S, K, T, r, sigma)
    if contract_type == "call":
        return norm_cdf(-d2)
    return norm_cdf(d2)


# ── Expected move ─────────────────────────────────────────────────────────────

@dataclass
class ExpectedMoveResult:
    underlying_price: float
    iv: float               # decimal, e.g. 0.18 = 18%
    dte: int
    expected_move: float    # 1σ in price points
    range_low: float
    range_high: float
    one_sigma_pct: float    # as percentage
    two_sigma_low: float
    two_sigma_high: float


def calc_expected_move(
    price: float,
    iv: float,          # decimal
    dte: int,
    r: float = RISK_FREE_RATE,
) -> ExpectedMoveResult:
    T = max(dte / 365.0, 1 / 365.0)
    em = price * iv * math.sqrt(T)
    return ExpectedMoveResult(
        underlying_price=price,
        iv=iv,
        dte=dte,
        expected_move=round(em, 2),
        range_low=round(price - em, 2),
        range_high=round(price + em, 2),
        one_sigma_pct=round(iv * math.sqrt(T) * 100, 2),
        two_sigma_low=round(price - 2 * em, 2),
        two_sigma_high=round(price + 2 * em, 2),
    )


# ── Realized volatility ───────────────────────────────────────────────────────

def calc_realized_vol(closes: list[float], period: int = 20) -> float:
    """Annualized realized volatility (decimal) from closing prices."""
    if len(closes) < period + 1:
        return 0.0
    prices = np.array(closes[-(period + 1):])
    log_returns = np.log(prices[1:] / prices[:-1])
    return float(np.std(log_returns, ddof=1) * math.sqrt(252))


# ── Strike search ─────────────────────────────────────────────────────────────

def find_strike_for_delta(
    S: float,
    target_delta: float,   # positive; function handles put sign
    iv: float,
    dte: int,
    contract_type: ContractType,
    r: float = RISK_FREE_RATE,
    step: float = 1.0,
    max_iter: int = 200,
) -> float:
    """
    Binary search for the strike that produces a given |delta|.
    Returns the strike rounded to the nearest `step`.
    """
    T = max(dte / 365.0, 1 / 365.0)
    target = abs(target_delta)

    lo, hi = S * 0.50, S * 1.50
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        d = abs(bs_delta(S, mid, T, r, iv, contract_type))
        if abs(d - target) < 0.0001:
            break
        # Puts: higher K → more ITM → higher |delta|. Calls: higher K → more OTM → lower delta.
        if contract_type == "put":
            if d < target:
                lo = mid  # K too low (too OTM), search higher
            else:
                hi = mid  # K too high (too ITM), search lower
        else:
            if d < target:
                hi = mid  # K too high (too OTM), search lower
            else:
                lo = mid  # K too low (too ITM), search higher

    raw = (lo + hi) / 2.0
    return round(raw / step) * step
