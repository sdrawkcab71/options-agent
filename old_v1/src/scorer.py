"""
Step 4 — Trade Scoring & Position Sizing.

Scores each TradeSetup on 5 factors (max 5 each = 25 total).
Applies non-negotiable sizing rules from account config.
"""

from dataclasses import dataclass

from src.scanner import TradeSetup
from src.config import (
    CAPITAL, MAX_SINGLE_TRADE_PCT, MIN_SCORE_TO_TRADE,
    SMALL_TRADE_SCORE_MAX, SMALL_TRADE_PCT, STANDARD_TRADE_PCT,
    VIX_ELEVATED,
)


@dataclass
class ScoredTrade:
    """A TradeSetup with a computed conviction score and sizing."""
    setup: TradeSetup
    score: int
    score_breakdown: dict[str, int]
    position_size_usd: float
    position_size_contracts: int
    pop_estimate: float    # probability of profit (rough)
    expected_value: float
    why: list[str]
    risk_flags: list[str]
    no_trade_reason: str   # non-empty if score < threshold


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _score_flow_conviction(setup: TradeSetup) -> tuple[int, str]:
    """Score 1–5 based on vol/OI ratio and estimated premium."""
    ratio = setup.flow.vol_oi_ratio
    premium = setup.flow.estimated_premium
    if ratio >= 10 and premium >= 200_000:
        return 5, f"Vol/OI {ratio}x with ${premium:,.0f} estimated premium (very strong)"
    if ratio >= 7 or premium >= 150_000:
        return 4, f"Vol/OI {ratio}x, ${premium:,.0f} premium (strong flow)"
    if ratio >= 5 or premium >= 100_000:
        return 3, f"Vol/OI {ratio}x, ${premium:,.0f} premium (moderate flow)"
    if ratio >= 3:
        return 2, f"Vol/OI {ratio}x (minimum threshold met)"
    return 1, f"Vol/OI {ratio}x (weak flow)"


def _score_technical_alignment(setup: TradeSetup) -> tuple[int, str]:
    """Score 1–5: how cleanly do technicals support the trade direction."""
    tech = setup.tech
    score = 0
    reasons: list[str] = []

    if tech.trend != "NEUTRAL":
        score += 2
        reasons.append(f"Trend {tech.trend}")

    if tech.momentum == "OVERSOLD" and tech.direction == "BULLISH":
        score += 2
        reasons.append("RSI oversold (mean-reversion setup)")
    elif tech.momentum == "OVERBOUGHT" and tech.direction == "BEARISH":
        score += 2
        reasons.append("RSI overbought (reversal setup)")
    elif tech.momentum == "NEUTRAL":
        score += 1
        reasons.append("RSI neutral")

    if tech.volume_signal == "HIGH":
        score += 1
        reasons.append("Volume confirming (>1.5x avg)")

    score = min(score, 5)
    return score, " | ".join(reasons) if reasons else "Weak technical confirmation"


def _score_risk_reward(setup: TradeSetup) -> tuple[int, str]:
    """Score 1–5: potential gain vs max loss (want at least 1:2)."""
    ask = setup.flow.ask
    if ask <= 0:
        return 1, "Cannot calculate R/R (invalid ask)"
    max_loss = ask * 100
    target_gain = (setup.target_high - ask) * 100
    rr = target_gain / max_loss if max_loss > 0 else 0

    if rr >= 2.0:
        return 5, f"R/R {rr:.1f}:1 (excellent)"
    if rr >= 1.5:
        return 4, f"R/R {rr:.1f}:1 (good)"
    if rr >= 1.0:
        return 3, f"R/R {rr:.1f}:1 (acceptable)"
    if rr >= 0.5:
        return 2, f"R/R {rr:.1f}:1 (below target)"
    return 1, f"R/R {rr:.1f}:1 (poor — skip)"


def _score_iv_environment(setup: TradeSetup, vix: float) -> tuple[int, str]:
    """Score 1–5: IV relative to environment. Low IV = better for buying."""
    iv = setup.flow.iv * 100  # convert to percentage
    if iv < 25 and vix < VIX_ELEVATED:
        return 5, f"IV {iv:.0f}% is low — cheap premium (ideal for buying)"
    if iv < 35 and vix < VIX_ELEVATED:
        return 4, f"IV {iv:.0f}% is moderate — fair premium"
    if iv < 50:
        return 3, f"IV {iv:.0f}% is elevated — premium is pricey"
    if iv < 70:
        return 2, f"IV {iv:.0f}% is high — watch for IV crush"
    return 1, f"IV {iv:.0f}% is very high — strong IV crush risk"


def _score_catalyst(setup: TradeSetup) -> tuple[int, str]:
    """
    Score 1–5 for known upcoming catalysts.

    Without a live catalyst feed we approximate from IV and DTE:
    high IV + near expiry often implies the market is pricing a catalyst.
    """
    iv = setup.flow.iv * 100
    dte = setup.flow.dte
    # High IV on a short-dated contract often means catalyst is expected
    if iv > 50 and dte <= 14:
        return 4, "Elevated IV implies potential catalyst within expiry window"
    if iv > 35 and dte <= 14:
        return 3, "Moderate IV suggests possible catalyst"
    if dte <= 10:
        return 2, "Short DTE — limited time for catalyst to materialize"
    return 1, "No obvious catalyst signal identified"


# ── Main scoring function ─────────────────────────────────────────────────────

def score_trade(setup: TradeSetup, vix: float = 15.0) -> ScoredTrade:
    """
    Score a TradeSetup and compute sizing, PoP, and EV.

    Args:
        setup: A confirmed TradeSetup from the scanner.
        vix: Current VIX level (used for IV environment scoring).

    Returns:
        ScoredTrade with all fields populated.
    """
    f1, why1 = _score_flow_conviction(setup)
    f2, why2 = _score_technical_alignment(setup)
    f3, why3 = _score_risk_reward(setup)
    f4, why4 = _score_iv_environment(setup, vix)
    f5, why5 = _score_catalyst(setup)

    total = f1 + f2 + f3 + f4 + f5
    breakdown = {
        "Flow Conviction": f1,
        "Technical Alignment": f2,
        "Risk/Reward": f3,
        "IV Environment": f4,
        "Catalyst": f5,
    }
    why = [why1, why2, why3, why4, why5]
    risk_flags: list[str] = []

    if setup.flow.iv * 100 > 60:
        risk_flags.append(f"Very high IV ({setup.flow.iv*100:.0f}%) — premium decay risk")
    if vix > VIX_ELEVATED:
        risk_flags.append("VIX > 25 — consider a spread instead of naked long")
    if setup.flow.spread > 0.15:
        risk_flags.append(f"Bid/ask spread ${setup.flow.spread:.2f} — liquidity concern")
    if setup.flow.dte <= 8:
        risk_flags.append(f"Only {setup.flow.dte} DTE — theta decay accelerating")

    # Sizing
    no_trade_reason = ""
    if total < MIN_SCORE_TO_TRADE:
        no_trade_reason = f"Score {total}/25 below minimum threshold of {MIN_SCORE_TO_TRADE}"
        position_usd = 0.0
    elif total <= SMALL_TRADE_SCORE_MAX:
        position_usd = round(CAPITAL * SMALL_TRADE_PCT, 0)
    else:
        position_usd = round(CAPITAL * STANDARD_TRADE_PCT, 0)

    position_usd = min(position_usd, CAPITAL * MAX_SINGLE_TRADE_PCT)
    contract_cost = setup.flow.ask * 100
    contracts = max(1, int(position_usd // contract_cost)) if contract_cost > 0 else 0

    # Probability of profit: use delta as rough ITM probability proxy
    pop = round(setup.flow.delta * 100, 1)

    # Expected value: (PoP × target midpoint gain) – ((1-PoP) × max loss)
    target_mid = (setup.target_low + setup.target_high) / 2
    gain_per_contract = (target_mid - setup.flow.ask) * 100
    loss_per_contract = setup.flow.ask * 100
    ev = round(
        (pop / 100 * gain_per_contract * contracts)
        - ((1 - pop / 100) * loss_per_contract * contracts),
        0,
    )

    return ScoredTrade(
        setup=setup,
        score=total,
        score_breakdown=breakdown,
        position_size_usd=position_usd,
        position_size_contracts=contracts,
        pop_estimate=pop,
        expected_value=ev,
        why=why,
        risk_flags=risk_flags,
        no_trade_reason=no_trade_reason,
    )


def calculate_size(ticker: str, score: int) -> dict[str, object]:
    """
    Quick position size lookup without a full trade setup.

    Args:
        ticker: For display purposes.
        score: Signal score 1–25.

    Returns:
        Dict with size_usd, size_pct, and verdict string.
    """
    if score < MIN_SCORE_TO_TRADE:
        return {
            "ticker": ticker, "score": score,
            "verdict": f"NO TRADE — score {score} < {MIN_SCORE_TO_TRADE}",
            "size_usd": 0, "size_pct": 0,
        }
    if score <= SMALL_TRADE_SCORE_MAX:
        pct = SMALL_TRADE_PCT
    else:
        pct = STANDARD_TRADE_PCT
    usd = min(CAPITAL * pct, CAPITAL * MAX_SINGLE_TRADE_PCT)
    return {
        "ticker": ticker, "score": score,
        "verdict": "TRADE",
        "size_usd": round(usd, 0),
        "size_pct": round(pct * 100, 1),
    }
