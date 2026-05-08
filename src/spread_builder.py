"""
Spread Builder — Strategy 1.
Builds put and call credit spreads; orchestrates the full daily scan pipeline.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from src.config import (
    CAPITAL,
    MAX_RISK_PCT,
    MIN_CREDIT,
    DEFAULT_SPREAD_WIDTH,
    STOP_LOSS_MULTIPLIER,
    PROFIT_TARGET_PCT,
    RISK_FREE_RATE,
)
from src.market_pulse import MarketPulse, get_market_pulse
from src.market_classifier import MarketVerdict, classify_market
from src.expected_move import (
    ExpectedMoveResult,
    calc_expected_move,
    bs_delta,
    bs_call_price,
    bs_put_price,
    prob_expire_otm,
)
from src.strike_selector import StrikeRecommendation, select_strikes
from src.massive_client import MassiveClient


@dataclass
class SpreadLeg:
    role: str               # "short" | "long"
    contract_type: str      # "put" | "call"
    strike: float
    mid_price: float
    delta: float
    iv: float
    option_ticker: str = ""


@dataclass
class CreditSpread:
    spread_type: str        # "put_credit_spread" | "call_credit_spread"
    underlying: str
    current_price: float
    expiry: str
    dte: int
    short_leg: SpreadLeg
    long_leg: SpreadLeg
    spread_width: float
    credit: float           # net premium per share
    max_loss: float         # per share = width - credit
    max_loss_usd: float     # per contract (× 100)
    breakeven: float
    pop: float              # probability short leg expires OTM (%)
    profit_target: float    # credit × PROFIT_TARGET_PCT
    stop_loss: float        # credit × STOP_LOSS_MULTIPLIER (value of spread = stop)
    win_rate_estimate: float
    contracts: int
    total_credit_usd: float
    passes_filters: bool
    filter_failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class DailySpreadResult:
    timestamp: str
    pulse: MarketPulse
    classifier: MarketVerdict
    expected_move: Optional[ExpectedMoveResult]
    strike_rec: Optional[StrikeRecommendation]
    put_spread: Optional[CreditSpread]
    call_spread: Optional[CreditSpread]
    recommended_action: str   # "TRADE BOTH" | "PUT ONLY" | "CALL ONLY" | "IRON CONDOR" | "SKIP"
    skip_reason: str = ""


def _find_next_expiry(dte_target: int = 14) -> str:
    """Returns the nearest Friday expiry that is dte_target days away."""
    today = date.today()
    target = today + timedelta(days=dte_target)
    # roll to nearest Friday
    days_to_friday = (4 - target.weekday()) % 7
    expiry = target + timedelta(days=days_to_friday)
    return expiry.isoformat()


def _get_mid_price(chain: list[dict], strike: float, contract_type: str) -> tuple[float, float, str]:
    """Find mid price and IV for a specific strike from options chain data."""
    ct_map = {"put": "put", "call": "call"}
    target_ct = ct_map[contract_type]
    best = None
    best_diff = float("inf")

    for contract in chain:
        det = contract.get("details", {})
        if det.get("contract_type", "").lower() != target_ct:
            continue
        k = det.get("strike_price", 0.0)
        diff = abs(float(k) - strike)
        if diff < best_diff:
            best_diff = diff
            best = contract

    if not best or best_diff > 2.5:
        return 0.0, 0.0, ""

    day = best.get("day", {})
    bid = day.get("last_ask", best.get("last_quote", {}).get("ask", 0.0))
    ask = day.get("last_ask", best.get("last_quote", {}).get("ask", 0.0))
    bid_q = best.get("last_quote", {}).get("bid", bid)
    ask_q = best.get("last_quote", {}).get("ask", ask)
    mid = (float(bid_q) + float(ask_q)) / 2.0 if bid_q and ask_q else 0.0

    iv = best.get("implied_volatility", 0.0) or 0.0
    ticker = best.get("details", {}).get("ticker", "")
    return round(mid, 2), float(iv), ticker


def _bs_mid_price(
    current_price: float,
    strike: float,
    dte: int,
    iv: float,
    contract_type: str,
) -> float:
    """Fallback: use BS to estimate mid price."""
    T = max(dte / 365.0, 1 / 365.0)
    if contract_type == "put":
        return round(bs_put_price(current_price, strike, T, RISK_FREE_RATE, iv), 2)
    return round(bs_call_price(current_price, strike, T, RISK_FREE_RATE, iv), 2)


def build_credit_spread(
    spread_type: str,               # "put_credit_spread" | "call_credit_spread"
    underlying: str,
    current_price: float,
    expiry: str,
    dte: int,
    short_strike: float,
    long_strike: float,
    iv: float,
    account_size: float = CAPITAL,
    chain: Optional[list[dict]] = None,
    r: float = RISK_FREE_RATE,
) -> CreditSpread:
    contract_type = "put" if "put" in spread_type else "call"
    T = max(dte / 365.0, 1 / 365.0)

    # ── Get mid prices ────────────────────────────────────────────────────────
    chain = chain or []
    short_mid, short_iv, short_ticker = _get_mid_price(chain, short_strike, contract_type)
    long_mid, long_iv, long_ticker = _get_mid_price(chain, long_strike, contract_type)

    # BS fallback when chain data unavailable
    if short_mid == 0.0:
        short_mid = _bs_mid_price(current_price, short_strike, dte, iv, contract_type)
    if long_mid == 0.0:
        long_mid = _bs_mid_price(current_price, long_strike, dte, iv, contract_type)

    credit = round(short_mid - long_mid, 2)
    spread_width = abs(short_strike - long_strike)
    max_loss_per_share = round(spread_width - credit, 2)
    max_loss_usd = round(max_loss_per_share * 100, 2)  # per contract

    # ── Contracts sizing: max 5% of account ──────────────────────────────────
    max_risk_dollars = account_size * MAX_RISK_PCT
    contracts = max(1, int(max_risk_dollars / max_loss_usd)) if max_loss_usd > 0 else 1

    # ── Greeks ────────────────────────────────────────────────────────────────
    short_delta = bs_delta(current_price, short_strike, T, r, iv, contract_type)
    pop = prob_expire_otm(short_delta) * 100

    if contract_type == "put":
        breakeven = round(short_strike - credit, 2)
    else:
        breakeven = round(short_strike + credit, 2)

    # ── Filter checks ─────────────────────────────────────────────────────────
    filter_failures: list[str] = []
    if credit < MIN_CREDIT:
        filter_failures.append(f"Credit ${credit:.2f} < minimum ${MIN_CREDIT:.2f}")
    if max_loss_usd <= 0:
        filter_failures.append("Invalid max loss (credit exceeds spread width)")
    if dte < 1:
        filter_failures.append("DTE too short")

    short_leg = SpreadLeg(
        role="short",
        contract_type=contract_type,
        strike=short_strike,
        mid_price=short_mid,
        delta=round(short_delta, 4),
        iv=round(short_iv or iv, 4),
        option_ticker=short_ticker,
    )
    long_leg = SpreadLeg(
        role="long",
        contract_type=contract_type,
        strike=long_strike,
        mid_price=long_mid,
        delta=round(bs_delta(current_price, long_strike, T, r, iv, contract_type), 4),
        iv=round(long_iv or iv, 4),
        option_ticker=long_ticker,
    )

    return CreditSpread(
        spread_type=spread_type,
        underlying=underlying,
        current_price=current_price,
        expiry=expiry,
        dte=dte,
        short_leg=short_leg,
        long_leg=long_leg,
        spread_width=spread_width,
        credit=credit,
        max_loss=max_loss_per_share,
        max_loss_usd=max_loss_usd,
        breakeven=breakeven,
        pop=round(pop, 1),
        profit_target=round(credit * PROFIT_TARGET_PCT, 2),
        stop_loss=round(credit * STOP_LOSS_MULTIPLIER, 2),
        win_rate_estimate=round(pop, 1),
        contracts=contracts,
        total_credit_usd=round(credit * contracts * 100, 2),
        passes_filters=len(filter_failures) == 0,
        filter_failures=filter_failures,
    )


def find_best_credit_spread(api_key: str, account_size: float = CAPITAL) -> DailySpreadResult:
    client = MassiveClient(api_key)
    timestamp = date.today().isoformat()

    # Step 1: Market pulse
    pulse = get_market_pulse(api_key)

    # Step 2: Classify market
    verdict = classify_market(pulse)

    if verdict.signal == "RED":
        return DailySpreadResult(
            timestamp=timestamp,
            pulse=pulse,
            classifier=verdict,
            expected_move=None,
            strike_rec=None,
            put_spread=None,
            call_spread=None,
            recommended_action="SKIP",
            skip_reason=verdict.reasons[0] if verdict.reasons else "RED market conditions",
        )

    # Step 3: Target expiry
    expiry = _find_next_expiry(dte_target=14)
    dte = (date.fromisoformat(expiry) - date.today()).days

    # Step 4: Expected move
    iv = pulse.iv_30d or 0.18
    spx_price = pulse.spx or (pulse.spy * 10 if pulse.spy else 5000.0)
    em = calc_expected_move(spx_price, iv, dte)

    # Step 5: Fetch SPX options chain
    chain: list[dict] = []
    try:
        chain = client.options_chain(
            "SPX",
            min_expiry=(date.today() + timedelta(days=7)).isoformat(),
            max_expiry=(date.today() + timedelta(days=21)).isoformat(),
        )
    except Exception:
        pass

    # Step 6: Strike selection
    strike_rec = select_strikes(
        underlying="SPX",
        current_price=spx_price,
        expiry=expiry,
        dte=dte,
        iv=iv,
        target_win_rate=0.84,
        events_today=pulse.events_today,
        chain=chain,
    )

    # Step 7: Build spreads
    put_spread = build_credit_spread(
        spread_type="put_credit_spread",
        underlying="SPX",
        current_price=spx_price,
        expiry=expiry,
        dte=dte,
        short_strike=strike_rec.put_short_strike,
        long_strike=strike_rec.put_long_strike,
        iv=iv,
        account_size=account_size,
        chain=chain,
    )

    call_spread = build_credit_spread(
        spread_type="call_credit_spread",
        underlying="SPX",
        current_price=spx_price,
        expiry=expiry,
        dte=dte,
        short_strike=strike_rec.call_short_strike,
        long_strike=strike_rec.call_long_strike,
        iv=iv,
        account_size=account_size,
        chain=chain,
    )

    # Step 8: Action recommendation
    put_ok = put_spread.passes_filters
    call_ok = call_spread.passes_filters

    if put_ok and call_ok:
        action = "IRON CONDOR"
    elif put_ok:
        action = "PUT ONLY"
    elif call_ok:
        action = "CALL ONLY"
    else:
        action = "SKIP"
        reasons = put_spread.filter_failures + call_spread.filter_failures
        skip_reason = "; ".join(reasons[:2])
        return DailySpreadResult(
            timestamp=timestamp,
            pulse=pulse,
            classifier=verdict,
            expected_move=em,
            strike_rec=strike_rec,
            put_spread=put_spread,
            call_spread=call_spread,
            recommended_action=action,
            skip_reason=skip_reason,
        )

    return DailySpreadResult(
        timestamp=timestamp,
        pulse=pulse,
        classifier=verdict,
        expected_move=em,
        strike_rec=strike_rec,
        put_spread=put_spread,
        call_spread=call_spread,
        recommended_action=action,
    )
