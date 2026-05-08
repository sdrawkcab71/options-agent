"""
Spread Scorer — conviction scoring for credit spreads and iron condors.
Max 25 points across 5 factors.
"""
from dataclasses import dataclass, field
from typing import Union

from src.market_classifier import MarketVerdict
from src.spread_builder import CreditSpread
from src.condor_builder import IronCondor
_MIN_SCORE_TO_TRADE = 15
_SMALL_TRADE_SCORE_MAX = 19


@dataclass
class ScoredSpread:
    trade: Union[CreditSpread, IronCondor]
    score: int                          # 0–25
    score_breakdown: dict[str, int]
    verdict: str                        # "STRONG TRADE" | "ACCEPTABLE" | "WEAK" | "NO TRADE"
    why: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    no_trade_reason: str = ""
    size_recommendation: str = ""


def _score_market_verdict(verdict: MarketVerdict) -> tuple[int, str]:
    """Factor 1: Market classifier (max 5)."""
    scores = {"GREEN": 5, "YELLOW": 3, "RED": 0}
    s = scores.get(verdict.signal, 0)
    why = f"Market {verdict.signal}: {verdict.reasons[0] if verdict.reasons else ''}"
    return s, why


def _score_strike_probability(trade: Union[CreditSpread, IronCondor]) -> tuple[int, str]:
    """Factor 2: Win probability from delta (max 5)."""
    if isinstance(trade, IronCondor):
        # Use worst (lower) of the two sides
        pop = min(trade.put_spread.pop, trade.call_spread.pop)
    else:
        pop = trade.pop

    if pop >= 90:
        s, label = 5, f"PoP {pop:.0f}% — excellent (0.10Δ)"
    elif pop >= 85:
        s, label = 4, f"PoP {pop:.0f}% — strong (0.15Δ)"
    elif pop >= 80:
        s, label = 3, f"PoP {pop:.0f}% — acceptable (0.20Δ)"
    else:
        s, label = 0, f"PoP {pop:.0f}% — below minimum 80%"
    return s, label


def _score_iv_edge(verdict: MarketVerdict) -> tuple[int, str]:
    """Factor 3: IV vs realized vol edge (max 5)."""
    edge_pct = verdict.iv_edge * 100
    if edge_pct >= 5:
        s, why = 5, f"IV-RV edge +{edge_pct:.1f}% — strong seller environment"
    elif edge_pct >= 2:
        s, why = 4, f"IV-RV edge +{edge_pct:.1f}% — good seller edge"
    elif edge_pct > 0:
        s, why = 3, f"IV-RV edge +{edge_pct:.1f}% — marginal edge"
    elif edge_pct > -2:
        s, why = 2, f"IV-RV edge {edge_pct:.1f}% — no edge but not extreme"
    else:
        s, why = 1, f"IV-RV edge {edge_pct:.1f}% — unfavorable for sellers"
    return s, why


def _score_credit_quality(trade: Union[CreditSpread, IronCondor]) -> tuple[int, str]:
    """Factor 4: Credit as % of spread width — want > 20% (max 5)."""
    if isinstance(trade, IronCondor):
        credit = trade.total_credit
        width = max(trade.put_spread.spread_width, trade.call_spread.spread_width)
    else:
        credit = trade.credit
        width = trade.spread_width

    credit_pct = (credit / width * 100) if width > 0 else 0

    if credit_pct >= 30:
        s, why = 5, f"Credit {credit:.2f} = {credit_pct:.0f}% of width — excellent"
    elif credit_pct >= 25:
        s, why = 4, f"Credit {credit:.2f} = {credit_pct:.0f}% of width — good"
    elif credit_pct >= 20:
        s, why = 3, f"Credit {credit:.2f} = {credit_pct:.0f}% of width — acceptable"
    elif credit_pct >= 15:
        s, why = 2, f"Credit {credit:.2f} = {credit_pct:.0f}% of width — marginal"
    else:
        s, why = 0, f"Credit {credit:.2f} = {credit_pct:.0f}% of width — too low"
    return s, why


def _score_dte(trade: Union[CreditSpread, IronCondor]) -> tuple[int, str]:
    """Factor 5: DTE sweet spot for theta decay (max 5)."""
    dte = trade.dte
    if 10 <= dte <= 21:
        s, why = 5, f"DTE {dte} — sweet spot for theta decay"
    elif 7 <= dte < 10:
        s, why = 4, f"DTE {dte} — short but still good theta"
    elif 21 < dte <= 35:
        s, why = 3, f"DTE {dte} — longer expiry, slower theta"
    elif 5 <= dte < 7:
        s, why = 2, f"DTE {dte} — very short, gamma accelerating"
    else:
        s, why = 1, f"DTE {dte} — outside optimal theta window"
    return s, why


def _risk_flags(trade: Union[CreditSpread, IronCondor], verdict: MarketVerdict) -> list[str]:
    flags = []
    if verdict.vix >= 25:
        flags.append(f"VIX {verdict.vix:.1f} — elevated, use spreads not naked options")
    if verdict.gap_risk == "HIGH":
        flags.append("Overnight gap risk HIGH — consider reducing size")
    if trade.dte <= 7:
        flags.append(f"DTE {trade.dte} — gamma acceleration zone")
    if verdict.event_density >= 2:
        flags.append(f"{verdict.event_density} macro events this week — elevated pin risk")
    return flags


def score_trade(
    trade: Union[CreditSpread, IronCondor],
    verdict: MarketVerdict,
) -> ScoredSpread:
    factors: dict[str, int] = {}
    why: list[str] = []

    s1, w1 = _score_market_verdict(verdict)
    s2, w2 = _score_strike_probability(trade)
    s3, w3 = _score_iv_edge(verdict)
    s4, w4 = _score_credit_quality(trade)
    s5, w5 = _score_dte(trade)

    factors = {
        "Market Verdict": s1,
        "Strike Probability": s2,
        "IV Edge": s3,
        "Credit Quality": s4,
        "DTE Optimization": s5,
    }
    why = [w1, w2, w3, w4, w5]
    total = sum(factors.values())

    flags = _risk_flags(trade, verdict)

    if total >= 20:
        trade_verdict = "STRONG TRADE"
        size = "Standard size: 3–5% account risk"
    elif total >= 15:
        trade_verdict = "ACCEPTABLE"
        size = "Small size: 2–3% account risk"
    elif total >= 10:
        trade_verdict = "WEAK"
        size = "Very small or skip"
    else:
        trade_verdict = "NO TRADE"
        size = ""

    no_trade_reason = ""
    if total < _MIN_SCORE_TO_TRADE or verdict.signal == "RED":
        trade_verdict = "NO TRADE"
        no_trade_reason = (
            "RED market conditions" if verdict.signal == "RED"
            else f"Score {total}/25 below minimum {_MIN_SCORE_TO_TRADE}"
        )

    return ScoredSpread(
        trade=trade,
        score=total,
        score_breakdown=factors,
        verdict=trade_verdict,
        why=why,
        risk_flags=flags,
        no_trade_reason=no_trade_reason,
        size_recommendation=size,
    )
