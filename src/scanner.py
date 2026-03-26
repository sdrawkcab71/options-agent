"""
Steps 2 & 3 — Options Flow Scanner + Technical Confirmation.

For each ticker in the watchlist:
  1. Fetch options chain via yfinance (free) and identify unusual vol/OI contracts
  2. Fetch daily bars via Polygon (free tier) and run technical analysis
  3. Only surface setups where flow direction matches technicals
"""

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import yfinance as yf

from src.polygon import PolygonClient
from src.technicals import TechSummary, analyze
from src.config import (
    WATCHLIST, MIN_VOL_OI_RATIO, MIN_PREMIUM_USD, MIN_DTE, MAX_DTE,
    MAX_OTM_PCT, MAX_BID_ASK_SPREAD, DELTA_MIN, DELTA_MAX,
)


@dataclass
class FlowSignal:
    """A single unusual options contract flagged by the flow scanner."""
    ticker: str
    direction: str         # BULLISH or BEARISH
    strike: float
    expiry: str
    contract_type: str     # call / put
    stock_price: float
    iv: float
    delta: float
    volume: int
    open_interest: int
    vol_oi_ratio: float
    estimated_premium: float
    dte: int
    ask: float
    bid: float
    spread: float


@dataclass
class TradeSetup:
    """A confirmed setup where flow signal aligns with technical direction."""
    flow: FlowSignal
    tech: TechSummary
    target_low: float      # +75% on contract
    target_high: float     # +100% on contract
    stop_loss: float       # -50% on contract


def _norm_cdf(x: float) -> float:
    """Abramowitz & Stegun approximation of the standard normal CDF."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    d = 0.3989423 * math.exp(-x * x / 2.0)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.7814779 + t * (-1.8212560 + t * 1.3302744))))
    return (1.0 - p) if x >= 0 else p


def _bs_delta(S: float, K: float, dte: int, iv: float, contract_type: str) -> float:
    """
    Black-Scholes delta approximation.

    Args:
        S: Current stock price.
        K: Strike price.
        dte: Days to expiration.
        iv: Implied volatility as a decimal (e.g. 0.30 = 30%).
        contract_type: 'call' or 'put'.

    Returns:
        Delta value (0–1 for calls, -1–0 for puts).
    """
    if S <= 0 or K <= 0 or iv <= 0 or dte <= 0:
        return 0.5 if contract_type == "call" else -0.5
    T = dte / 365.0
    r = 0.045  # approximate risk-free rate
    d1 = (math.log(S / K) + (r + 0.5 * iv ** 2) * T) / (iv * math.sqrt(T))
    return _norm_cdf(d1) if contract_type == "call" else _norm_cdf(d1) - 1.0


def _yf_options_to_chain(ticker: str) -> list[dict]:
    """
    Fetch options chain via yfinance and normalize to our internal format.

    Filters to contracts with expiry in [MIN_DTE, MAX_DTE] from today.

    Args:
        ticker: Stock symbol.

    Returns:
        List of normalized contract dicts compatible with _parse_flow_signals.
    """
    yf_ticker = yf.Ticker(ticker)
    try:
        stock_price = float(yf_ticker.fast_info.last_price or 0)
    except Exception:
        stock_price = 0.0

    if stock_price <= 0:
        return []

    today = date.today()
    min_exp = today + timedelta(days=MIN_DTE)
    max_exp = today + timedelta(days=MAX_DTE)

    contracts: list[dict] = []
    for exp_str in (yf_ticker.options or []):
        try:
            exp_date = date.fromisoformat(exp_str)
        except ValueError:
            continue
        if not (min_exp <= exp_date <= max_exp):
            continue

        try:
            chain = yf_ticker.option_chain(exp_str)
        except Exception:
            continue

        dte = (exp_date - today).days
        for df, ctype in [(chain.calls, "call"), (chain.puts, "put")]:
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                iv = float(row.get("impliedVolatility") or 0)
                strike = float(row.get("strike") or 0)
                ask = float(row.get("ask") or 0)
                bid = float(row.get("bid") or 0)
                raw_vol = row.get("volume")
                raw_oi = row.get("openInterest")
                volume = 0 if (raw_vol is None or raw_vol != raw_vol) else int(raw_vol)
                oi = 0 if (raw_oi is None or raw_oi != raw_oi) else int(raw_oi)
                delta = _bs_delta(stock_price, strike, dte, iv, ctype)
                contracts.append({
                    "details": {
                        "contract_type": ctype,
                        "strike_price": strike,
                        "expiration_date": exp_str,
                    },
                    "greeks": {"delta": delta},
                    "day": {"volume": volume},
                    "implied_volatility": iv,
                    "last_quote": {"ask": ask, "bid": bid},
                    "open_interest": oi,
                    "underlying_asset": {"price": stock_price},
                })
    return contracts


def _parse_flow_signals(
    ticker: str,
    chain_results: list[dict],
) -> list[FlowSignal]:
    """
    Extract unusual flow signals from an options chain snapshot.

    Args:
        ticker: Underlying symbol.
        chain_results: List of contract dicts from Polygon /v3/snapshot/options.

    Returns:
        Filtered list of FlowSignal objects sorted by vol/OI ratio descending.
    """
    today = date.today()
    signals: list[FlowSignal] = []

    for contract in chain_results:
        details = contract.get("details", {})
        greeks = contract.get("greeks", {})
        day = contract.get("day", {})
        quote = contract.get("last_quote", {})
        underlying = contract.get("underlying_asset", {})

        contract_type: str = details.get("contract_type", "")
        strike: float = float(details.get("strike_price") or 0)
        expiry_str: str = details.get("expiration_date", "")
        iv: float = float(contract.get("implied_volatility") or 0)
        delta: float = float(greeks.get("delta") or 0)
        volume: int = int(day.get("volume") or 0)
        oi: int = int(contract.get("open_interest") or 0)
        ask: float = float(quote.get("ask") or 0)
        bid: float = float(quote.get("bid") or 0)
        stock_price: float = float(underlying.get("price") or 0)

        # Skip contracts with missing critical data
        if not all([strike, expiry_str, ask, stock_price]):
            continue

        # DTE filter
        try:
            expiry_date = date.fromisoformat(expiry_str)
        except ValueError:
            continue
        dte = (expiry_date - today).days
        if not (MIN_DTE <= dte <= MAX_DTE):
            continue

        # OTM filter
        if contract_type == "call":
            otm_pct = (strike - stock_price) / stock_price
        else:
            otm_pct = (stock_price - strike) / stock_price
        if otm_pct < 0 or otm_pct > MAX_OTM_PCT:
            continue

        # Delta filter
        abs_delta = abs(delta)
        if not (DELTA_MIN <= abs_delta <= DELTA_MAX):
            continue

        # Spread filter
        spread = ask - bid
        if spread > MAX_BID_ASK_SPREAD:
            continue

        # Vol/OI filter
        if oi == 0 or volume == 0:
            continue
        vol_oi = volume / oi
        if vol_oi < MIN_VOL_OI_RATIO:
            continue

        # Premium filter (estimated notional)
        est_premium = ask * 100 * volume
        if est_premium < MIN_PREMIUM_USD:
            continue

        direction = "BULLISH" if contract_type == "call" else "BEARISH"

        signals.append(FlowSignal(
            ticker=ticker,
            direction=direction,
            strike=strike,
            expiry=expiry_str,
            contract_type=contract_type,
            stock_price=stock_price,
            iv=iv,
            delta=abs_delta,
            volume=volume,
            open_interest=oi,
            vol_oi_ratio=round(vol_oi, 1),
            estimated_premium=est_premium,
            dte=dte,
            ask=ask,
            bid=bid,
            spread=spread,
        ))

    signals.sort(key=lambda s: s.vol_oi_ratio, reverse=True)
    return signals


def _build_trade_setup(flow: FlowSignal, tech: TechSummary) -> TradeSetup:
    """Combine flow + tech into a TradeSetup with price targets."""
    target_low = round(flow.ask * 1.75, 2)   # +75%
    target_high = round(flow.ask * 2.00, 2)  # +100%
    stop_loss = round(flow.ask * 0.50, 2)    # -50%
    return TradeSetup(flow=flow, tech=tech, target_low=target_low,
                      target_high=target_high, stop_loss=stop_loss)


def scan_ticker(ticker: str, client: PolygonClient) -> Optional[TradeSetup]:
    """
    Run flow scan + technical check for a single ticker.

    Args:
        ticker: Stock symbol to analyze.
        client: Authenticated PolygonClient.

    Returns:
        Best TradeSetup if a confirmed signal is found, else None.
    """
    chain_results = _yf_options_to_chain(ticker)
    if not chain_results:
        return None

    flow_signals = _parse_flow_signals(ticker, chain_results)
    if not flow_signals:
        return None

    # Technicals
    try:
        bars_data = client.daily_bars(ticker)
        bars = bars_data.get("results", [])
        tech = analyze(ticker, bars)
    except (RuntimeError, ValueError):
        return None

    # Confirmation: flow direction must match technical direction
    best_flow = flow_signals[0]
    if tech.direction == "NEUTRAL":
        return None
    if best_flow.direction != tech.direction:
        return None

    return _build_trade_setup(best_flow, tech)


def run_full_scan(api_key: str, tickers: Optional[list[str]] = None) -> list[TradeSetup]:
    """
    Run the full flow + technical scan across the watchlist.

    Args:
        api_key: Polygon.io API key.
        tickers: Override list; defaults to WATCHLIST from config.

    Returns:
        List of confirmed TradeSetups, best opportunities first.
    """
    from src.polygon import PolygonClient as PC
    client = PC(api_key)
    universe = tickers or WATCHLIST
    setups: list[TradeSetup] = []

    for ticker in universe:
        setup = scan_ticker(ticker, client)
        if setup:
            setups.append(setup)

    # Sort by vol/OI ratio as a proxy for conviction
    setups.sort(key=lambda s: s.flow.vol_oi_ratio, reverse=True)
    return setups


def get_ticker_flow(api_key: str, ticker: str) -> list[FlowSignal]:
    """
    Return all unusual flow signals for a specific ticker (no tech filter).

    Args:
        api_key: Polygon.io API key (unused; kept for consistent CLI signature).
        ticker: Stock symbol.

    Returns:
        Sorted list of FlowSignal objects.
    """
    chain_results = _yf_options_to_chain(ticker)
    if not chain_results:
        raise RuntimeError(
            f"No options data found for {ticker}. "
            "Check that the ticker is valid and options are available."
        )
    return _parse_flow_signals(ticker, chain_results)


def get_ticker_tech(api_key: str, ticker: str) -> TechSummary:
    """
    Return technical analysis for a specific ticker.

    Args:
        api_key: Polygon.io API key.
        ticker: Stock symbol.

    Returns:
        TechSummary with trend, RSI, volume signals.
    """
    from src.polygon import PolygonClient as PC
    client = PC(api_key)
    bars_data = client.daily_bars(ticker)
    bars = bars_data.get("results", [])
    return analyze(ticker, bars)
