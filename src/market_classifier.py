"""
Market Classifier — Strategy 2.
Reads MarketPulse and returns GREEN / YELLOW / RED verdict with strategy recommendation.
"""
from dataclasses import dataclass, field

from src.market_pulse import MarketPulse
from src.config import VIX_LOW, VIX_NORMAL, VIX_ELEVATED

_HIGH_IMPACT_EVENTS = {"fomc", "cpi", "nfp", "nonfarm", "pce", "rate decision", "fed"}


@dataclass
class MarketVerdict:
    signal: str             # "GREEN" | "YELLOW" | "RED"
    vix_regime: str         # "LOW" | "NORMAL" | "ELEVATED" | "CRISIS"
    vix: float
    iv_edge: float          # IV - RV (positive = sellers favored)
    has_seller_edge: bool
    gap_risk: str           # "LOW" | "MODERATE" | "HIGH"
    overnight_gap_pct: float
    events_today: list[str] = field(default_factory=list)
    event_density: int = 0
    reasons: list[str] = field(default_factory=list)
    strategy_recommendation: str = ""
    strike_adjustment: str = "NORMAL"  # "NORMAL" | "WIDER" | "NONE"


def _has_major_event_today(events_today: list[str]) -> bool:
    for ev in events_today:
        if any(kw in ev.lower() for kw in _HIGH_IMPACT_EVENTS):
            return True
    return False


def classify_market(pulse: MarketPulse) -> MarketVerdict:
    reasons: list[str] = []
    signal = "GREEN"

    vix = pulse.vix
    vix_regime = pulse.vix_label  # already computed in market_pulse

    # ── RED conditions (first match wins) ─────────────────────────────────────
    if vix >= VIX_ELEVATED:
        signal = "RED"
        reasons.append(f"VIX {vix:.1f} ≥ {VIX_ELEVATED} — crisis regime")

    elif _has_major_event_today(pulse.events_today):
        signal = "RED"
        ev_list = ", ".join(pulse.events_today[:3])
        reasons.append(f"Major macro event today: {ev_list}")

    elif abs(pulse.overnight_gap_pct) >= 1.5:
        signal = "RED"
        reasons.append(
            f"Overnight gap {pulse.overnight_gap_pct:+.2f}% — excessive gap risk"
        )

    # ── YELLOW conditions ─────────────────────────────────────────────────────
    if signal == "GREEN":
        yellow_triggers = []

        if VIX_NORMAL <= vix < VIX_ELEVATED:
            yellow_triggers.append(f"VIX {vix:.1f} elevated ({VIX_NORMAL}–{VIX_ELEVATED})")

        if 0.75 <= abs(pulse.overnight_gap_pct) < 1.5:
            yellow_triggers.append(
                f"Overnight gap {pulse.overnight_gap_pct:+.2f}% — moderate risk"
            )

        if not pulse.has_seller_edge:
            yellow_triggers.append(
                f"IV ({pulse.iv_30d*100:.1f}%) ≤ Realized Vol ({pulse.rv_20d*100:.1f}%) — no seller edge"
            )

        if pulse.event_density >= 2:
            yellow_triggers.append(
                f"{pulse.event_density} macro events this week — elevated uncertainty"
            )

        if yellow_triggers:
            signal = "YELLOW"
            reasons.extend(yellow_triggers)

    # ── GREEN confirmation ────────────────────────────────────────────────────
    if signal == "GREEN":
        if pulse.has_seller_edge:
            reasons.append(
                f"IV ({pulse.iv_30d*100:.1f}%) > RV ({pulse.rv_20d*100:.1f}%) — seller edge confirmed"
            )
        if vix < VIX_NORMAL:
            reasons.append(f"VIX {vix:.1f} — low volatility regime")
        if abs(pulse.overnight_gap_pct) < 0.5:
            reasons.append("Overnight gap minimal — clean entry conditions")

    # ── Strike adjustment ─────────────────────────────────────────────────────
    strike_adjustment = {"GREEN": "NORMAL", "YELLOW": "WIDER", "RED": "NONE"}[signal]

    # ── Strategy recommendation ───────────────────────────────────────────────
    if signal == "RED":
        strategy = "Sit in cash — no new positions. Wait for VIX to drop below 30 and events to clear."
    elif signal == "YELLOW":
        strategy = (
            "Trade carefully. Use wider strikes (move 1–2 strikes further OTM), "
            "reduce position size by 25%. Prefer iron condors over naked credit spreads."
        )
    else:
        iv_str = f"{pulse.iv_30d*100:.0f}%"
        strategy = (
            f"Trade aggressively. IV ({iv_str}) elevated vs realized vol — strong seller edge. "
            "Target 0.15 delta short strikes on SPX iron condor. Standard position size."
        )

    return MarketVerdict(
        signal=signal,
        vix_regime=vix_regime,
        vix=vix,
        iv_edge=pulse.iv_rv_edge,
        has_seller_edge=pulse.has_seller_edge,
        gap_risk=pulse.gap_risk_label,
        overnight_gap_pct=pulse.overnight_gap_pct,
        events_today=pulse.events_today,
        event_density=pulse.event_density,
        reasons=reasons,
        strategy_recommendation=strategy,
        strike_adjustment=strike_adjustment,
    )
