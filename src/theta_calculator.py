"""
Theta Calculator — Strategy 3.
Per-position theta, hourly decay curve, gamma risk flag, compounding projections.
"""
import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from src.config import RISK_FREE_RATE, GAMMA_RISK_DTE, GAMMA_CRITICAL_DTE
from src.expected_move import bs_theta, bs_gamma, bs_delta

_POSITIONS_FILE = Path("positions.json")

# Approximate intraday theta distribution (must sum to 1.0)
# Empirical: last hour of trading earns ~35% of daily theta (gamma exposure)
_HOURLY_DECAY_PCT = {
    "9:30-10:30": 0.20,
    "10:30-11:30": 0.15,
    "11:30-12:30": 0.10,
    "12:30-13:30": 0.10,
    "13:30-14:30": 0.10,
    "14:30-15:30": 0.10,
    "15:30-16:00": 0.25,  # final 30 min extra-weighted
}


@dataclass
class LegTheta:
    role: str               # "short" | "long"
    contract_type: str
    strike: float
    theta_per_day: float    # $ per day (positive = income for short leg)
    gamma: float
    delta: float
    dte: int


@dataclass
class PositionTheta:
    position_id: str
    strategy: str
    underlying: str
    expiry: str
    dte: int

    net_theta_per_day: float    # portfolio net theta in $ (positive = income)
    legs: list[LegTheta] = field(default_factory=list)

    # Hourly breakdown ($ per hour period)
    hourly_curve: dict[str, float] = field(default_factory=dict)

    # Weekend theta
    weekend_theta_usd: float = 0.0

    # Gamma risk
    in_gamma_zone: bool = False
    gamma_critical: bool = False
    gamma_warning: str = ""

    # Compounding projections ($ accumulated)
    daily_target_usd: float = 0.0
    weekly_target_usd: float = 0.0
    monthly_target_usd: float = 0.0
    projection_30d: float = 0.0
    projection_60d: float = 0.0
    projection_90d: float = 0.0


@dataclass
class ThetaReport:
    positions: list[PositionTheta] = field(default_factory=list)
    total_net_theta: float = 0.0
    total_portfolio_value: float = 0.0
    theta_as_pct_portfolio: float = 0.0


def _dte_from_expiry(expiry: str) -> int:
    try:
        exp_date = date.fromisoformat(expiry)
        return max(0, (exp_date - date.today()).days)
    except Exception:
        return 0


def _theta_acceleration(dte: int) -> float:
    """Empirical acceleration factor for theta as expiry approaches."""
    if dte <= 7:
        return 2.5
    if dte <= 14:
        return 1.8
    if dte <= 21:
        return 1.5
    return 1.0


def calc_leg_theta(
    role: str,
    contract_type: str,
    strike: float,
    underlying_price: float,
    dte: int,
    iv: float,
    r: float = RISK_FREE_RATE,
) -> LegTheta:
    T = max(dte / 365.0, 1 / 365.0)

    # BS theta is negative (option loses value per day)
    raw_theta = bs_theta(underlying_price, strike, T, r, iv, contract_type)
    gamma = bs_gamma(underlying_price, strike, T, r, iv)
    delta = bs_delta(underlying_price, strike, T, r, iv, contract_type)

    # For seller (short), daily income = -theta × 100 (per contract) × acceleration
    sign = -1.0 if role == "short" else 1.0
    accel = _theta_acceleration(dte)
    theta_usd = sign * raw_theta * 100 * accel  # per contract per day

    return LegTheta(
        role=role,
        contract_type=contract_type,
        strike=strike,
        theta_per_day=round(theta_usd, 4),
        gamma=round(gamma, 6),
        delta=round(delta, 4),
        dte=dte,
    )


def _hourly_curve(net_theta_per_day: float) -> dict[str, float]:
    return {
        period: round(net_theta_per_day * pct, 4)
        for period, pct in _HOURLY_DECAY_PCT.items()
    }


def _weekend_theta(net_theta_per_day: float, dte: int, expiry: str) -> float:
    """
    Friday-to-Monday theta capture.
    Standard market convention: weekend = 3 calendar days.
    Only applies if position's next expiry is > Monday.
    """
    try:
        exp_date = date.fromisoformat(expiry)
        today = date.today()
        # Check if the coming Friday is before expiry
        days_to_friday = (4 - today.weekday()) % 7
        friday = today.replace(day=today.day + days_to_friday)
        if friday < exp_date:
            return round(net_theta_per_day * 3, 4)
    except Exception:
        pass
    return 0.0


def _compounding_projection(
    portfolio_value: float,
    net_theta_per_day: float,
    days: int,
    reinvest_rate: float = 0.80,
) -> float:
    """Compound theta income into growing portfolio."""
    if portfolio_value <= 0:
        return 0.0
    daily_return = net_theta_per_day * reinvest_rate / portfolio_value
    return round(portfolio_value * ((1 + daily_return) ** days), 2)


def calc_position_theta(
    position: dict,
    underlying_price: float,
    iv: float = 0.18,
) -> PositionTheta:
    pos_id = position.get("id", "unknown")
    strategy = position.get("type", position.get("strategy", "spread"))
    underlying = position.get("underlying", "SPX")
    expiry = position.get("expiry", "")
    dte = _dte_from_expiry(expiry)

    legs_raw = position.get("legs", [])
    # For legacy flat positions, synthesize legs
    if not legs_raw:
        legs_raw = _synthesize_legs(position)

    computed_legs: list[LegTheta] = []
    for leg in legs_raw:
        role = leg.get("role", "short")
        ct = leg.get("contract_type", "put")
        strike = float(leg.get("strike", underlying_price))
        leg_iv = float(leg.get("iv_at_entry", iv))
        computed_legs.append(
            calc_leg_theta(role, ct, strike, underlying_price, dte, leg_iv)
        )

    net_theta = sum(l.theta_per_day for l in computed_legs)

    in_gamma = dte <= GAMMA_RISK_DTE
    gamma_crit = dte <= GAMMA_CRITICAL_DTE
    gamma_warn = ""
    if gamma_crit:
        gamma_warn = f"CRITICAL: {dte} DTE — gamma risk very high, consider closing"
    elif in_gamma:
        gamma_warn = f"WARNING: {dte} DTE inside gamma risk zone (≤{GAMMA_RISK_DTE})"

    hourly = _hourly_curve(net_theta)
    weekend = _weekend_theta(net_theta, dte, expiry)

    return PositionTheta(
        position_id=pos_id,
        strategy=strategy,
        underlying=underlying,
        expiry=expiry,
        dte=dte,
        net_theta_per_day=round(net_theta, 4),
        legs=computed_legs,
        hourly_curve=hourly,
        weekend_theta_usd=weekend,
        in_gamma_zone=in_gamma,
        gamma_critical=gamma_crit,
        gamma_warning=gamma_warn,
        daily_target_usd=round(net_theta, 2),
        weekly_target_usd=round(net_theta * 5, 2),
        monthly_target_usd=round(net_theta * 21, 2),
    )


def _synthesize_legs(position: dict) -> list[dict]:
    """Convert flat position dict to synthetic legs list for theta calc."""
    legs = []
    if "put_short_strike" in position:
        legs.append({"role": "short", "contract_type": "put", "strike": position["put_short_strike"]})
        legs.append({"role": "long", "contract_type": "put", "strike": position["put_long_strike"]})
    if "call_short_strike" in position:
        legs.append({"role": "short", "contract_type": "call", "strike": position["call_short_strike"]})
        legs.append({"role": "long", "contract_type": "call", "strike": position["call_long_strike"]})
    if not legs and "short_strike" in position:
        ct = position.get("contract_type", "put")
        legs.append({"role": "short", "contract_type": ct, "strike": position["short_strike"]})
        legs.append({"role": "long", "contract_type": ct, "strike": position["long_strike"]})
    return legs


def build_theta_report(
    underlying_price: float = 5000.0,
    iv: float = 0.18,
    account_size: float = 5000.0,
) -> ThetaReport:
    if not _POSITIONS_FILE.exists():
        return ThetaReport()

    try:
        raw = json.loads(_POSITIONS_FILE.read_text())
    except Exception:
        return ThetaReport()

    open_positions = [p for p in raw if isinstance(p, dict) and p.get("status") == "open" and not p.get("_comment")]

    report = ThetaReport()
    for pos in open_positions:
        pos_theta = calc_position_theta(pos, underlying_price, iv)

        # Add compounding projections using the position's own theta
        pos_theta.projection_30d = _compounding_projection(account_size, pos_theta.net_theta_per_day, 30)
        pos_theta.projection_60d = _compounding_projection(account_size, pos_theta.net_theta_per_day, 60)
        pos_theta.projection_90d = _compounding_projection(account_size, pos_theta.net_theta_per_day, 90)

        report.positions.append(pos_theta)
        report.total_net_theta += pos_theta.net_theta_per_day

    report.total_net_theta = round(report.total_net_theta, 4)
    report.total_portfolio_value = account_size
    if account_size > 0:
        report.theta_as_pct_portfolio = round(report.total_net_theta / account_size * 100, 4)

    return report
