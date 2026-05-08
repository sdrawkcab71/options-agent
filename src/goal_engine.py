"""
Goal Engine — Gamified goal wizard.
Maps profit target + time horizon to risk profile and strategy recommendation.
"""
import math
from dataclasses import dataclass, field
from typing import Optional

from src.config import (
    CAPITAL,
    GOAL_CONSERVATIVE_MAX_WEEKLY,
    GOAL_MODERATE_MAX_WEEKLY,
    GOAL_AGGRESSIVE_MAX_WEEKLY,
    DELTA_WIN_RATE_TABLE,
)

HORIZON_OPTIONS = [5, 10, 15, 30, 60, 90]


@dataclass
class GoalProfile:
    # Inputs
    target_profit_usd: float
    account_size: float
    horizon_days: int

    # Computed returns
    required_return_pct: float      # target / account (%)
    daily_return_needed: float      # per day (%)
    weekly_return_needed: float     # per 5-day week (%)

    # Risk classification
    risk_profile: str               # "CONSERVATIVE" | "MODERATE" | "AGGRESSIVE" | "VERY_AGGRESSIVE"
    risk_score: int                 # 1-10 (1=ultra-safe, 10=max risk)

    # Strategy recommendation
    target_delta: float
    recommended_strategy: str
    strategy_description: str
    win_probability_estimate: float # historical win rate %
    daily_target_usd: float
    weekly_target_usd: float

    # Feasibility
    feasibility: str                # "REALISTIC" | "STRETCH" | "UNLIKELY"
    feasibility_note: str
    probability_of_goal: float      # rough estimate %

    # Position sizing
    max_risk_per_trade_usd: float
    trades_per_week: int
    contracts_estimate: int

    # Milestones (for gamification)
    milestones: list[dict] = field(default_factory=list)


def _classify_risk(weekly_return_needed: float) -> tuple[str, float, float, str, str, int]:
    """Returns (profile, target_delta, win_prob, strategy, description, risk_score)."""
    if weekly_return_needed <= GOAL_CONSERVATIVE_MAX_WEEKLY:
        return (
            "CONSERVATIVE",
            0.10,
            0.90,
            "Far OTM Iron Condors",
            "Sell iron condors at 0.10 delta (90% win rate) on SPX. "
            "Max 2% account risk per trade. Aim for 1–3 condors per week.",
            2,
        )
    if weekly_return_needed <= GOAL_MODERATE_MAX_WEEKLY:
        return (
            "MODERATE",
            0.15,
            0.85,
            "Standard Iron Condors / Credit Spreads",
            "Sell iron condors at 0.15 delta (85% win rate) on SPX. "
            "Max 3–5% account risk per trade. 2–4 spreads per week.",
            4,
        )
    if weekly_return_needed <= GOAL_AGGRESSIVE_MAX_WEEKLY:
        return (
            "AGGRESSIVE",
            0.22,
            0.78,
            "Tighter Credit Spreads with Directional Bias",
            "Sell credit spreads at 0.20–0.25 delta. Accept lower win rate (78%) "
            "for higher premium. Requires active management and adjustment rules.",
            6,
        )
    # Very aggressive
    return (
        "VERY_AGGRESSIVE",
        0.35,
        0.65,
        "High-Delta Spreads + Debit Spreads",
        "Short strikes at 0.30–0.35 delta with high premium but only ~65% win rate. "
        "Mix in directional debit spreads when trend is clear. "
        "WARNING: Requires expert-level management. High drawdown risk.",
        9,
    )


def _feasibility(weekly_return_needed: float) -> tuple[str, str, float]:
    """Returns (label, note, probability_pct)."""
    if weekly_return_needed <= GOAL_MODERATE_MAX_WEEKLY:
        return (
            "REALISTIC",
            "This goal is achievable with consistent iron condor selling at standard parameters.",
            70.0,
        )
    if weekly_return_needed <= GOAL_AGGRESSIVE_MAX_WEEKLY:
        return (
            "STRETCH",
            "Achievable but requires above-average execution and active position management. "
            "Drawdowns are likely. Prepare for a bumpy path.",
            45.0,
        )
    return (
        "UNLIKELY",
        "This return target requires near-perfect win rate OR significant leverage. "
        "Options math makes returns above 3%/week very difficult to sustain. "
        "Consider extending your time horizon to reduce required weekly return.",
        20.0,
    )


def _generate_milestones(
    target_usd: float,
    account: float,
    horizon_days: int,
) -> list[dict]:
    """Generate weekly progress milestones for gamification."""
    weeks = max(1, horizon_days // 5)
    per_week = target_usd / weeks
    milestones = []
    cumulative = 0.0
    for week in range(1, weeks + 1):
        cumulative += per_week
        pct_of_account = (cumulative / account) * 100
        milestones.append({
            "week": week,
            "target_usd": round(cumulative, 2),
            "pct_of_account": round(pct_of_account, 2),
            "label": f"Week {week}: ${cumulative:,.0f} ({pct_of_account:.1f}% of account)",
        })
    return milestones


def calculate_goal_profile(
    target_profit_usd: float,
    horizon_days: int,
    account_size: float = CAPITAL,
) -> GoalProfile:
    # Clamp horizon to valid options
    valid_horizons = [h for h in HORIZON_OPTIONS if h <= 365]
    if horizon_days not in valid_horizons:
        # Round to nearest
        horizon_days = min(valid_horizons, key=lambda h: abs(h - horizon_days))

    required_return_pct = (target_profit_usd / account_size) * 100
    trading_days = horizon_days * 5 / 7  # approximate market days
    daily_return_needed = required_return_pct / trading_days if trading_days > 0 else 0.0
    weekly_return_needed = daily_return_needed * 5

    # Classify risk
    profile, target_delta, win_prob, strategy, description, risk_score = _classify_risk(
        weekly_return_needed / 100  # convert to decimal for comparison
    )

    # Feasibility
    feasibility_label, feasibility_note, prob_of_goal = _feasibility(
        weekly_return_needed / 100
    )

    # Daily / weekly targets
    daily_target = round(target_profit_usd / trading_days, 2) if trading_days > 0 else 0.0
    weekly_target = round(daily_target * 5, 2)

    # Position sizing
    max_risk = round(account_size * 0.05, 2)  # 5% max per trade
    # Rough contracts estimate: $5 SPX spread, credit ~ $1.00, max loss ~ $400/contract
    contracts_est = max(1, int(max_risk / 400))
    trades_per_week = 2 if profile == "CONSERVATIVE" else (3 if profile == "MODERATE" else 4)

    milestones = _generate_milestones(target_profit_usd, account_size, horizon_days)

    return GoalProfile(
        target_profit_usd=target_profit_usd,
        account_size=account_size,
        horizon_days=horizon_days,
        required_return_pct=round(required_return_pct, 2),
        daily_return_needed=round(daily_return_needed, 4),
        weekly_return_needed=round(weekly_return_needed, 4),
        risk_profile=profile,
        risk_score=risk_score,
        target_delta=target_delta,
        recommended_strategy=strategy,
        strategy_description=description,
        win_probability_estimate=win_prob * 100,
        daily_target_usd=daily_target,
        weekly_target_usd=weekly_target,
        feasibility=feasibility_label,
        feasibility_note=feasibility_note,
        probability_of_goal=prob_of_goal,
        max_risk_per_trade_usd=max_risk,
        trades_per_week=trades_per_week,
        contracts_estimate=contracts_est,
        milestones=milestones,
    )
