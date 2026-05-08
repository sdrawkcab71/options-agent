"""
Market pulse: SPX price, VIX, IV vs realized vol edge, overnight gap risk, economic events.
Primary data from Massive.com; fallbacks via yfinance.
"""
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

from src.massive_client import MassiveClient
from src.expected_move import calc_realized_vol
from src.config import VIX_LOW, VIX_NORMAL, VIX_ELEVATED

# ── Upcoming macro events (update monthly) ────────────────────────────────────
_MACRO_EVENTS: list[tuple[str, str]] = [
    ("2026-05-28", "PCE Inflation"),
    ("2026-06-04", "FOMC Meeting"),
    ("2026-06-11", "CPI Release"),
    ("2026-06-11", "FOMC Rate Decision"),
    ("2026-06-06", "NFP Jobs Report"),
]

# ── Finnhub economic calendar (free, 60 req/min) ─────────────────────────────
_FINNHUB_KEY_ENV = "FINNHUB_API_KEY"
_FINNHUB_CALENDAR_URL = "https://finnhub.io/api/v1/calendar/economic"

_HIGH_IMPACT_KEYWORDS = {
    "fomc", "federal reserve", "fed", "cpi", "inflation", "nfp",
    "nonfarm", "gdp", "pce", "jobs", "unemployment", "rate decision",
}


@dataclass
class MarketPulse:
    # Index benchmarks
    spy: float = 0.0
    spy_chg: float = 0.0
    qqq: float = 0.0
    qqq_chg: float = 0.0
    iwm: float = 0.0
    iwm_chg: float = 0.0
    spx: float = 0.0
    spx_chg: float = 0.0

    # Volatility
    vix: float = 0.0
    vix_label: str = "UNKNOWN"

    # IV vs Realized Vol edge (positive = sellers have edge)
    iv_30d: float = 0.0    # VIX / 100 as proxy for 30d IV
    rv_20d: float = 0.0    # 20-day historical realized vol
    iv_rv_edge: float = 0.0
    has_seller_edge: bool = False

    # Gap risk
    overnight_gap_pct: float = 0.0
    gap_risk_label: str = "LOW"

    # Macro
    regime: str = "UNKNOWN"
    events_today: list[str] = field(default_factory=list)
    events_this_week: list[str] = field(default_factory=list)
    event_density: int = 0  # count of high-impact events in next 7 days


def _vix_label(vix: float) -> str:
    if vix < VIX_LOW:
        return "LOW"
    if vix < VIX_NORMAL:
        return "NORMAL"
    if vix < VIX_ELEVATED:
        return "ELEVATED"
    return "CRISIS"


def _gap_label(gap_pct: float) -> str:
    abs_gap = abs(gap_pct)
    if abs_gap < 0.5:
        return "LOW"
    if abs_gap < 1.5:
        return "MODERATE"
    return "HIGH"


def _regime(pulse: MarketPulse) -> str:
    if pulse.vix < VIX_NORMAL and pulse.spy_chg > -0.5:
        return "RISK-ON"
    if pulse.vix >= VIX_ELEVATED:
        return "RISK-OFF"
    return "CHOPPY"


def _fetch_yf_prices() -> dict[str, tuple[float, float]]:
    """Returns {ticker: (price, daily_chg_pct)} via yfinance."""
    if not _YF_AVAILABLE:
        return {}
    try:
        tickers = ["SPY", "QQQ", "IWM", "^VIX", "^GSPC", "/ES=F"]
        data = yf.download(
            tickers,
            period="2d",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
        closes = data["Close"]
        result = {}
        for t in ["SPY", "QQQ", "IWM", "^VIX", "^GSPC", "/ES=F"]:
            if t in closes.columns and len(closes[t].dropna()) >= 2:
                vals = closes[t].dropna().values
                price = float(vals[-1])
                prev = float(vals[-2])
                chg = ((price - prev) / prev) * 100 if prev else 0.0
                result[t] = (price, chg)
        return result
    except Exception:
        return {}


def _fetch_macro_events() -> tuple[list[str], list[str], int]:
    """Returns (events_today, events_this_week, density) from hard-coded list + Finnhub."""
    today_str = date.today().isoformat()
    week_end = (date.today() + timedelta(days=7)).isoformat()

    today_events: list[str] = []
    week_events: list[str] = []

    # Hard-coded events
    for ev_date, ev_name in _MACRO_EVENTS:
        if ev_date == today_str:
            today_events.append(ev_name)
        if today_str <= ev_date <= week_end:
            week_events.append(ev_name)

    # Finnhub (if key present)
    finnhub_key = os.environ.get(_FINNHUB_KEY_ENV, "")
    if finnhub_key:
        try:
            import requests as _r
            resp = _r.get(
                _FINNHUB_CALENDAR_URL,
                params={"from": today_str, "to": week_end, "token": finnhub_key},
                timeout=5,
            )
            if resp.ok:
                for ev in resp.json().get("economicCalendar", []):
                    name = ev.get("event", "").strip()
                    ev_date = ev.get("time", "")[:10]
                    impact = ev.get("impact", "").lower()
                    if impact in ("high", "medium") or any(
                        kw in name.lower() for kw in _HIGH_IMPACT_KEYWORDS
                    ):
                        if ev_date == today_str and name not in today_events:
                            today_events.append(name)
                        if today_str <= ev_date <= week_end and name not in week_events:
                            week_events.append(name)
        except Exception:
            pass

    return today_events, week_events, len(week_events)


def get_market_pulse(api_key: str) -> MarketPulse:
    pulse = MarketPulse()
    client = MassiveClient(api_key)

    # ── Benchmark prices via Massive ──────────────────────────────────────────
    try:
        snaps = client.stock_snapshots(["SPY", "QQQ", "IWM"])
        for ticker, attr_price, attr_chg in [
            ("SPY", "spy", "spy_chg"),
            ("QQQ", "qqq", "qqq_chg"),
            ("IWM", "iwm", "iwm_chg"),
        ]:
            snap = snaps.get(ticker, {})
            day = snap.get("day", {})
            prev = snap.get("prevDay", {})
            price = day.get("c") or snap.get("lastTrade", {}).get("p", 0.0)
            prev_close = prev.get("c", 0.0)
            chg = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
            setattr(pulse, attr_price, round(float(price), 2))
            setattr(pulse, attr_chg, round(float(chg), 2))
    except Exception:
        yf_prices = _fetch_yf_prices()
        pulse.spy, pulse.spy_chg = yf_prices.get("SPY", (0.0, 0.0))
        pulse.qqq, pulse.qqq_chg = yf_prices.get("QQQ", (0.0, 0.0))
        pulse.iwm, pulse.iwm_chg = yf_prices.get("IWM", (0.0, 0.0))

    # ── SPX index price ───────────────────────────────────────────────────────
    try:
        spx_snap = client.index_snapshot("I:SPX")
        spx_val = (
            spx_snap.get("value")
            or spx_snap.get("session", {}).get("close")
            or 0.0
        )
        pulse.spx = round(float(spx_val), 2)
        prev_spx = spx_snap.get("session", {}).get("previous_close", 0.0)
        if prev_spx:
            pulse.spx_chg = round((pulse.spx - prev_spx) / prev_spx * 100, 2)
    except Exception:
        yf_prices = _fetch_yf_prices()
        spx_raw, spx_chg = yf_prices.get("^GSPC", (0.0, 0.0))
        pulse.spx = round(spx_raw, 2)
        pulse.spx_chg = round(spx_chg, 2)

    # Fallback: SPX ≈ SPY × 10
    if pulse.spx == 0.0 and pulse.spy:
        pulse.spx = round(pulse.spy * 10, 2)

    # ── VIX ───────────────────────────────────────────────────────────────────
    try:
        vix_snap = client.index_snapshot("I:VIX")
        vix_val = (
            vix_snap.get("value")
            or vix_snap.get("session", {}).get("close")
            or 0.0
        )
        pulse.vix = round(float(vix_val), 2)
    except Exception:
        pass

    if pulse.vix == 0.0 and _YF_AVAILABLE:
        try:
            yf_prices = _fetch_yf_prices()
            pulse.vix, _ = yf_prices.get("^VIX", (20.0, 0.0))
        except Exception:
            pulse.vix = 20.0

    pulse.vix_label = _vix_label(pulse.vix)

    # ── Realized vol vs IV edge ───────────────────────────────────────────────
    pulse.iv_30d = round(pulse.vix / 100, 4)
    try:
        spy_bars = client.daily_bars("SPY", lookback_days=30)
        closes = [b["c"] for b in spy_bars if "c" in b]
        pulse.rv_20d = round(calc_realized_vol(closes, period=20), 4)
    except Exception:
        pulse.rv_20d = pulse.iv_30d * 0.85  # rough estimate

    pulse.iv_rv_edge = round(pulse.iv_30d - pulse.rv_20d, 4)
    pulse.has_seller_edge = pulse.iv_rv_edge > 0.01  # at least 1 vol point edge

    # ── Overnight gap risk ────────────────────────────────────────────────────
    try:
        es_prices = _fetch_yf_prices()
        es_price, es_chg = es_prices.get("/ES=F", (0.0, 0.0))
        pulse.overnight_gap_pct = round(es_chg, 2)
    except Exception:
        pulse.overnight_gap_pct = 0.0
    pulse.gap_risk_label = _gap_label(pulse.overnight_gap_pct)

    # ── Macro events ──────────────────────────────────────────────────────────
    pulse.events_today, pulse.events_this_week, pulse.event_density = _fetch_macro_events()

    # ── Overall regime ────────────────────────────────────────────────────────
    pulse.regime = _regime(pulse)

    return pulse
