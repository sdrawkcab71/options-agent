"""
Strike Selector — Strategy 4.
Probability-based framework: maps delta to win rate and finds exact strikes.
"""
from dataclasses import dataclass, field
from typing import Optional

from src.config import (
    DEFAULT_SHORT_DELTA,
    DEFAULT_SPREAD_WIDTH,
    MIN_WIN_RATE,
    DELTA_WIN_RATE_TABLE,
    RISK_FREE_RATE,
    WIDER_STRIKE_EVENTS,
)
from src.expected_move import (
    ExpectedMoveResult,
    calc_expected_move,
    find_strike_for_delta,
    bs_delta,
    prob_expire_otm,
)


@dataclass
class StrikeRecommendation:
    underlying: str
    current_price: float
    expiry: str
    dte: int
    iv: float                   # decimal

    # Expected move
    expected_move: ExpectedMoveResult

    # Put spread strikes
    put_short_strike: float
    put_long_strike: float
    put_delta: float
    put_win_probability: float  # %

    # Call spread strikes
    call_short_strike: float
    call_long_strike: float
    call_delta: float
    call_win_probability: float  # %

    # Adjustments
    target_delta: float
    target_win_rate: float
    effective_win_rate: float
    put_skew_adjustment: float = 0.0
    event_widening: float = 0.0  # extra points added due to events
    wider_strikes: bool = False
    notes: list[str] = field(default_factory=list)


def _nearest_spx_strike(price: float, step: float = 5.0) -> float:
    return round(price / step) * step


def _get_put_skew_adjustment(chain: list[dict]) -> float:
    """
    Compare IV at OTM puts vs OTM calls at similar delta.
    If put skew > 1.5 vol points → return extra OTM distance in points.
    """
    if not chain:
        return 0.0
    try:
        put_ivs = []
        call_ivs = []
        for contract in chain:
            det = contract.get("details", {})
            greeks = contract.get("greeks", {})
            iv = contract.get("implied_volatility") or greeks.get("delta", None)
            ct = det.get("contract_type", "")
            delta = abs(greeks.get("delta", 0.5))
            raw_iv = contract.get("implied_volatility", 0.0)

            if 0.12 <= delta <= 0.18 and raw_iv:
                if ct == "put":
                    put_ivs.append(raw_iv)
                elif ct == "call":
                    call_ivs.append(raw_iv)

        if put_ivs and call_ivs:
            skew = sum(put_ivs) / len(put_ivs) - sum(call_ivs) / len(call_ivs)
            if skew > 0.03:  # >3 vol points skew
                return 5.0   # move put strike 1 width further OTM
    except Exception:
        pass
    return 0.0


def select_strikes(
    underlying: str,
    current_price: float,
    expiry: str,
    dte: int,
    iv: float,                        # decimal
    target_win_rate: float = 0.84,
    events_today: Optional[list[str]] = None,
    chain: Optional[list[dict]] = None,
    spread_width: float = DEFAULT_SPREAD_WIDTH,
    r: float = RISK_FREE_RATE,
) -> StrikeRecommendation:
    events_today = events_today or []
    notes: list[str] = []

    # ── Map target win rate to delta ──────────────────────────────────────────
    target_delta = DEFAULT_SHORT_DELTA
    effective_win_rate = target_win_rate

    for delta, win_rate in sorted(DELTA_WIN_RATE_TABLE.items()):
        if win_rate >= target_win_rate:
            target_delta = delta
            effective_win_rate = win_rate
            break

    if effective_win_rate < MIN_WIN_RATE:
        target_delta = 0.20
        effective_win_rate = 0.80
        notes.append(f"Forced to 0.20 delta to meet {MIN_WIN_RATE*100:.0f}% minimum win rate")

    # ── Widen on major event days ─────────────────────────────────────────────
    event_widening = 0.0
    wider_strikes = False
    if events_today:
        for ev in events_today:
            if any(kw in ev.lower() for kw in [w.lower() for w in WIDER_STRIKE_EVENTS]):
                event_widening = spread_width  # one extra width further OTM
                wider_strikes = True
                notes.append(f"Wider strikes applied: major event today ({ev})")
                target_delta = max(target_delta - 0.05, 0.08)
                break

    # ── Expected move ─────────────────────────────────────────────────────────
    em = calc_expected_move(current_price, iv, dte, r)

    # ── Put skew adjustment ───────────────────────────────────────────────────
    put_skew_adj = _get_put_skew_adjustment(chain or [])
    if put_skew_adj > 0:
        notes.append(f"Put skew detected — short put moved {put_skew_adj:.0f}pts further OTM")

    # ── Find strikes via BS delta search ─────────────────────────────────────
    T = max(dte / 365.0, 1 / 365.0)

    # Put side: short strike OTM below current price
    put_short_raw = find_strike_for_delta(
        current_price, target_delta, iv, dte, "put", r, step=5.0
    )
    put_short_strike = _nearest_spx_strike(put_short_raw - put_skew_adj - event_widening)
    put_long_strike = put_short_strike - spread_width

    # Call side: short strike OTM above current price
    call_short_raw = find_strike_for_delta(
        current_price, target_delta, iv, dte, "call", r, step=5.0
    )
    call_short_strike = _nearest_spx_strike(call_short_raw + event_widening)
    call_long_strike = call_short_strike + spread_width

    # ── Calculate actual deltas at chosen strikes ─────────────────────────────
    put_delta = bs_delta(current_price, put_short_strike, T, r, iv, "put")
    call_delta = bs_delta(current_price, call_short_strike, T, r, iv, "call")

    put_win_prob = prob_expire_otm(put_delta) * 100
    call_win_prob = prob_expire_otm(call_delta) * 100

    # ── Validate: short strikes must be OTM ──────────────────────────────────
    if put_short_strike >= current_price:
        put_short_strike = _nearest_spx_strike(current_price - spread_width)
        put_long_strike = put_short_strike - spread_width
        notes.append("Put short strike clamped below current price")

    if call_short_strike <= current_price:
        call_short_strike = _nearest_spx_strike(current_price + spread_width)
        call_long_strike = call_short_strike + spread_width
        notes.append("Call short strike clamped above current price")

    return StrikeRecommendation(
        underlying=underlying,
        current_price=current_price,
        expiry=expiry,
        dte=dte,
        iv=iv,
        expected_move=em,
        put_short_strike=put_short_strike,
        put_long_strike=put_long_strike,
        put_delta=round(put_delta, 4),
        put_win_probability=round(put_win_prob, 1),
        call_short_strike=call_short_strike,
        call_long_strike=call_long_strike,
        call_delta=round(call_delta, 4),
        call_win_probability=round(call_win_prob, 1),
        target_delta=target_delta,
        target_win_rate=target_win_rate,
        effective_win_rate=effective_win_rate,
        put_skew_adjustment=put_skew_adj,
        event_widening=event_widening,
        wider_strikes=wider_strikes,
        notes=notes,
    )
