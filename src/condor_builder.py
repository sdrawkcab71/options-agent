"""
Condor Builder — Strategy 5.
Selects best index and builds a complete iron condor setup.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from src.config import (
    CAPITAL,
    MAX_RISK_PCT,
    ADJUSTMENT_TRIGGER_PCT,
    INDEX_PREFERENCE,
    RISK_FREE_RATE,
    GAMMA_RISK_DTE,
)
from src.massive_client import MassiveClient
from src.technicals import analyze as tech_analyze
from src.spread_builder import CreditSpread, build_credit_spread, _find_next_expiry
from src.strike_selector import select_strikes
from src.market_pulse import MarketPulse
from src.expected_move import calc_expected_move, bs_delta, bs_put_price, bs_call_price


@dataclass
class IronCondor:
    underlying: str
    current_price: float
    expiry: str
    dte: int
    put_spread: CreditSpread
    call_spread: CreditSpread

    total_credit: float
    max_loss: float             # per share
    max_loss_usd: float         # per contract
    breakeven_low: float
    breakeven_high: float
    profit_zone_width: float    # breakeven_high - breakeven_low

    win_probability: float      # rough: put_pop × call_pop / 100
    profit_target: float        # 50% of total credit
    stop_loss: float            # 2× total credit (spread value hits this → exit)

    adjustment_trigger_low: float   # underlying falls to here → adjust
    adjustment_trigger_high: float  # underlying rises to here → adjust

    contracts: int
    max_risk_usd: float
    account_risk_pct: float

    best_underlying: str
    selection_rationale: str
    passes_filters: bool
    filter_failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _score_underlying(
    ticker: str,
    client: MassiveClient,
    pulse: Optional[MarketPulse] = None,
) -> tuple[float, float, float, str]:
    """
    Returns (score, price, iv, rationale) for an index.
    Higher score = better condor candidate.
    Prefers: higher IV, neutral/range-bound trend.
    """
    try:
        bars = client.daily_bars(ticker, lookback_days=30)
        if len(bars) < 22:
            return 0.0, 0.0, 0.0, f"{ticker}: insufficient history"

        closes = [b["c"] for b in bars]
        tech = tech_analyze(ticker, bars)
        price = tech.price

        # Get ATM IV from options chain (use front-month)
        expiry = _find_next_expiry(14)
        chain = client.options_chain(
            ticker,
            min_expiry=(date.today() + timedelta(days=7)).isoformat(),
            max_expiry=(date.today() + timedelta(days=21)).isoformat(),
        )

        # Find ATM call IV
        atm_iv = 0.0
        best_diff = float("inf")
        for c in chain:
            det = c.get("details", {})
            if det.get("contract_type", "").lower() != "call":
                continue
            k = float(det.get("strike_price", 0))
            raw_iv = c.get("implied_volatility", 0.0)
            if raw_iv and abs(k - price) < best_diff:
                best_diff = abs(k - price)
                atm_iv = float(raw_iv)

        if atm_iv == 0.0:
            atm_iv = 0.18  # fallback

        # Score: IV weight (60%) + neutrality weight (40%)
        iv_score = min(atm_iv * 200, 60.0)  # cap at 60
        direction = getattr(tech, "direction", "NEUTRAL")
        neutrality_score = 40.0 if direction == "NEUTRAL" else (20.0 if direction in ("BULLISH", "BEARISH") else 30.0)

        # Prefer SPX (cash-settled, 60/40 tax, no assignment)
        tax_bonus = 10.0 if ticker == "SPX" else 0.0

        total_score = iv_score + neutrality_score + tax_bonus
        rationale = (
            f"{ticker}: IV={atm_iv*100:.1f}%, trend={direction}, score={total_score:.0f}"
        )
        return total_score, price, atm_iv, rationale

    except Exception as e:
        return 0.0, 0.0, 0.0, f"{ticker}: error — {e}"


def select_best_underlying(
    client: MassiveClient,
    universe: list[str] = INDEX_PREFERENCE,
    pulse: Optional[MarketPulse] = None,
) -> tuple[str, float, float, str]:
    """Return (ticker, price, iv, rationale) for the best condor underlying."""
    best_score = -1.0
    best = ("SPX", 5000.0, 0.18, "default fallback")

    for ticker in universe:
        score, price, iv, rationale = _score_underlying(ticker, client, pulse)
        if score > best_score and price > 0:
            best_score = score
            best = (ticker, price, iv, rationale)

    return best


def build_iron_condor(
    underlying: str,
    current_price: float,
    iv: float,
    expiry: str,
    dte: int,
    account_size: float = CAPITAL,
    spread_width: float = 5.0,
    target_delta: float = 0.15,
    chain: Optional[list[dict]] = None,
    events_today: Optional[list[str]] = None,
    r: float = RISK_FREE_RATE,
) -> IronCondor:
    events_today = events_today or []
    chain = chain or []

    # Select strikes
    strike_rec = select_strikes(
        underlying=underlying,
        current_price=current_price,
        expiry=expiry,
        dte=dte,
        iv=iv,
        target_win_rate=0.84,
        events_today=events_today,
        chain=chain,
        spread_width=spread_width,
    )

    # Build both spreads
    put_spread = build_credit_spread(
        spread_type="put_credit_spread",
        underlying=underlying,
        current_price=current_price,
        expiry=expiry,
        dte=dte,
        short_strike=strike_rec.put_short_strike,
        long_strike=strike_rec.put_long_strike,
        iv=iv,
        account_size=account_size,
        chain=chain,
        r=r,
    )

    call_spread = build_credit_spread(
        spread_type="call_credit_spread",
        underlying=underlying,
        current_price=current_price,
        expiry=expiry,
        dte=dte,
        short_strike=strike_rec.call_short_strike,
        long_strike=strike_rec.call_long_strike,
        iv=iv,
        account_size=account_size,
        chain=chain,
        r=r,
    )

    total_credit = round(put_spread.credit + call_spread.credit, 2)
    # Max loss is the wider wing minus total credit
    max_loss_per_share = round(max(put_spread.spread_width, call_spread.spread_width) - total_credit, 2)
    max_loss_usd = round(max_loss_per_share * 100, 2)

    breakeven_low = round(put_spread.short_leg.strike - total_credit, 2)
    breakeven_high = round(call_spread.short_leg.strike + total_credit, 2)
    profit_zone = round(breakeven_high - breakeven_low, 2)

    # Combined win probability (rough: assumes independence)
    win_prob = round(put_spread.pop * call_spread.pop / 100, 1)

    # Position sizing: use the more conservative of the two spreads
    contracts = min(put_spread.contracts, call_spread.contracts)
    max_risk_total = round(max_loss_usd * contracts, 2)
    risk_pct = round(max_risk_total / account_size * 100, 2)

    # Adjustment triggers: 30% of the distance from current price to short strike
    put_distance = current_price - put_spread.short_leg.strike
    call_distance = call_spread.short_leg.strike - current_price
    adj_low = round(put_spread.short_leg.strike + put_distance * ADJUSTMENT_TRIGGER_PCT, 2)
    adj_high = round(call_spread.short_leg.strike - call_distance * ADJUSTMENT_TRIGGER_PCT, 2)

    notes: list[str] = []
    if dte <= GAMMA_RISK_DTE:
        notes.append(f"WARNING: DTE={dte} is in gamma risk zone (≤{GAMMA_RISK_DTE})")

    filter_failures: list[str] = []
    if not put_spread.passes_filters:
        filter_failures.extend([f"Put spread: {f}" for f in put_spread.filter_failures])
    if not call_spread.passes_filters:
        filter_failures.extend([f"Call spread: {f}" for f in call_spread.filter_failures])

    return IronCondor(
        underlying=underlying,
        current_price=current_price,
        expiry=expiry,
        dte=dte,
        put_spread=put_spread,
        call_spread=call_spread,
        total_credit=total_credit,
        max_loss=max_loss_per_share,
        max_loss_usd=max_loss_usd,
        breakeven_low=breakeven_low,
        breakeven_high=breakeven_high,
        profit_zone_width=profit_zone,
        win_probability=win_prob,
        profit_target=round(total_credit * 0.50, 2),
        stop_loss=round(total_credit * 2.0, 2),
        adjustment_trigger_low=adj_low,
        adjustment_trigger_high=adj_high,
        contracts=contracts,
        max_risk_usd=max_risk_total,
        account_risk_pct=risk_pct,
        best_underlying=underlying,
        selection_rationale="",
        passes_filters=len(filter_failures) == 0,
        filter_failures=filter_failures,
        notes=notes,
    )


def find_best_condor(
    api_key: str,
    account_size: float = CAPITAL,
    dte_preference: str = "WEEKLY",  # "WEEKLY" | "MONTHLY"
    pulse: Optional[MarketPulse] = None,
) -> IronCondor:
    client = MassiveClient(api_key)
    dte_target = 7 if dte_preference == "WEEKLY" else 30
    expiry = _find_next_expiry(dte_target)
    dte = (date.fromisoformat(expiry) - date.today()).days

    # Select best underlying
    ticker, price, iv, rationale = select_best_underlying(client, pulse=pulse)

    # Fetch chain
    chain: list[dict] = []
    try:
        chain = client.options_chain(
            ticker,
            min_expiry=(date.today() + timedelta(days=dte_target - 3)).isoformat(),
            max_expiry=(date.today() + timedelta(days=dte_target + 7)).isoformat(),
        )
    except Exception:
        pass

    events_today = pulse.events_today if pulse else []

    condor = build_iron_condor(
        underlying=ticker,
        current_price=price,
        iv=iv,
        expiry=expiry,
        dte=dte,
        account_size=account_size,
        events_today=events_today,
        chain=chain,
    )
    condor.best_underlying = ticker
    condor.selection_rationale = rationale
    return condor
