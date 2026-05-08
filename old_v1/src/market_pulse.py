"""
Step 1 — Market Pulse.

Fetches SPY/QQQ/IWM prices, VIX, upcoming earnings, and known macro events.
Uses yfinance for all price data (free, no Polygon subscription required).
Polygon is used only for technical analysis (daily bars — free tier).
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import yfinance as yf

from src.config import VIX_LOW, VIX_NEUTRAL, VIX_ELEVATED

BENCHMARK_TICKERS = ["SPY", "QQQ", "IWM"]

# Hard-coded near-term macro events — update each month.
# Format: (date_str, description)
MACRO_EVENTS: list[tuple[str, str]] = [
    ("2026-04-30", "FOMC Rate Decision"),
    ("2026-04-10", "CPI Release"),
    ("2026-04-11", "PPI Release"),
    ("2026-05-07", "FOMC Rate Decision"),
]

# High-profile earnings to flag as landmines this cycle
EARNINGS_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "TSLA", "META", "AMZN", "GOOGL",
    "AMD", "NFLX", "JPM", "BAC", "COIN", "PLTR",
]


@dataclass
class MarketPulse:
    """Snapshot of macro market conditions for a single session."""
    spy: float = 0.0
    spy_chg: float = 0.0
    qqq: float = 0.0
    qqq_chg: float = 0.0
    iwm: float = 0.0
    iwm_chg: float = 0.0
    vix: float = 0.0
    vix_label: str = "UNKNOWN"
    regime: str = "UNKNOWN"
    events: list[str] = field(default_factory=list)
    earnings: list[str] = field(default_factory=list)


def _vix_label(vix: float) -> str:
    """Classify VIX level into a human-readable bucket."""
    if vix < VIX_LOW:
        return "LOW"
    if vix < VIX_NEUTRAL:
        return "NEUTRAL"
    if vix < VIX_ELEVATED:
        return "ELEVATED"
    return "FEAR"


def _market_regime(spy_chg: float, qqq_chg: float, vix: float) -> str:
    """Determine broad market regime from index moves and VIX."""
    if vix > VIX_ELEVATED:
        return "RISK-OFF"
    if spy_chg > 0.5 and qqq_chg > 0.5:
        return "RISK-ON"
    if spy_chg < -0.5 and qqq_chg < -0.5:
        return "RISK-OFF"
    return "CHOPPY"


def _fetch_vix() -> float:
    """
    Fetch current VIX level via yfinance.

    Returns:
        VIX float value, or 0.0 on failure.
    """
    try:
        df = yf.download("^VIX", period="2d", progress=False, auto_adjust=True)
        if not df.empty:
            # yfinance 1.x returns multi-level columns for single tickers too
            close = df["Close"]
            if hasattr(close, "iloc") and close.ndim == 2:
                close = close.iloc[:, 0]  # flatten multi-ticker column
            return float(close.dropna().iloc[-1])
    except Exception:
        pass
    return 0.0


def _fetch_benchmarks(api_key: str = "") -> dict[str, tuple[float, float]]:
    """
    Fetch price and % daily change for SPY, QQQ, IWM.

    Tries Polygon snapshots first (real-time on paid plan); falls back to yfinance.

    Returns:
        Dict mapping ticker → (price, change_pct).
    """
    if api_key:
        try:
            from src.polygon import PolygonClient
            client = PolygonClient(api_key)
            data = client.stock_snapshots(BENCHMARK_TICKERS)
            results: dict[str, tuple[float, float]] = {}
            for snap in data.get("tickers", []):
                ticker = snap.get("ticker", "")
                day = snap.get("day", {})
                price = float(day.get("c") or 0)
                prev = float(snap.get("prevDay", {}).get("c") or 1)
                chg = ((price - prev) / prev * 100) if prev else 0.0
                results[ticker] = (price, chg)
            if results:
                return results
        except Exception:
            pass  # fall through to yfinance

    # yfinance fallback
    results = {}
    try:
        data = yf.download(
            " ".join(BENCHMARK_TICKERS), period="2d", progress=False, auto_adjust=True,
        )
        close = data["Close"]
        for ticker in BENCHMARK_TICKERS:
            if ticker not in close.columns:
                continue
            series = close[ticker].dropna()
            if len(series) >= 2:
                prev, curr = float(series.iloc[-2]), float(series.iloc[-1])
                chg = (curr - prev) / prev * 100 if prev else 0.0
            elif len(series) == 1:
                curr, chg = float(series.iloc[-1]), 0.0
            else:
                continue
            results[ticker] = (curr, chg)
    except Exception:
        pass
    return results


def _upcoming_earnings(days_ahead: int = 7) -> list[str]:
    """
    Return list of tickers from EARNINGS_WATCHLIST with earnings in the next N days.

    Uses yfinance calendar data; returns empty list if unavailable.
    """
    today = date.today()
    cutoff = today + timedelta(days=days_ahead)
    flagged: list[str] = []

    for ticker in EARNINGS_WATCHLIST:
        try:
            info = yf.Ticker(ticker).calendar
            if info is None or info.empty:
                continue
            # calendar index contains 'Earnings Date' row in some yfinance versions
            if "Earnings Date" in info.columns:
                earn_dates = info["Earnings Date"]
            elif "Earnings Date" in info.index:
                earn_dates = [info.loc["Earnings Date"]]
            else:
                continue
            for d in earn_dates:
                if d is not None:
                    earn_date = d.date() if hasattr(d, "date") else d
                    if today <= earn_date <= cutoff:
                        flagged.append(f"{ticker} (~{earn_date})")
                        break
        except Exception:
            continue
    return flagged


def _upcoming_macro() -> list[str]:
    """Return macro events in the next 14 days from MACRO_EVENTS."""
    today = date.today()
    cutoff = today + timedelta(days=14)
    events: list[str] = []
    for date_str, desc in MACRO_EVENTS:
        event_date = date.fromisoformat(date_str)
        if today <= event_date <= cutoff:
            events.append(f"{desc} ({date_str})")
    return events


def get_market_pulse(api_key: str) -> MarketPulse:
    """
    Build a full MarketPulse snapshot for the current session.

    Args:
        api_key: Polygon.io API key (unused for pulse; kept for consistent CLI signature).

    Returns:
        Populated MarketPulse dataclass.
    """
    pulse = MarketPulse()

    benchmarks = _fetch_benchmarks(api_key)
    pulse.spy, pulse.spy_chg = benchmarks.get("SPY", (0.0, 0.0))
    pulse.qqq, pulse.qqq_chg = benchmarks.get("QQQ", (0.0, 0.0))
    pulse.iwm, pulse.iwm_chg = benchmarks.get("IWM", (0.0, 0.0))

    pulse.vix = _fetch_vix()
    pulse.vix_label = _vix_label(pulse.vix)
    pulse.regime = _market_regime(pulse.spy_chg, pulse.qqq_chg, pulse.vix)

    pulse.events = _upcoming_macro()
    pulse.earnings = _upcoming_earnings()

    return pulse
