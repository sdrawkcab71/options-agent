"""
Signal analysis engine.

Translates technical indicators into plain-English buy/sell/wait verdicts
designed to be understood by both advanced traders AND a 10-year-old.

Each signal returns a structured dict containing:
  - verdict:     'bull' | 'bear' | 'neutral'
  - strength:    1 (weak) | 2 (moderate) | 3 (strong)
  - headline:    plain English one-liner for an adult
  - kid_explain: analogy a 10-year-old would understand
  - value_label: the actual technical number (e.g. 'RSI: 62')

Signals calculated:
  1. TREND      — SMA 20/50 alignment (price vs its average path)
  2. MOMENTUM   — RSI 14 (too fast? too slow? just right?)
  3. VOLUME     — Volume vs 20-day average (are big players moving?)
  4. VOLATILITY — Bollinger Band width (coiled spring / options cost)
  5. MACD       — MACD crossovers (momentum engine turning on/off)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# ── Thresholds ────────────────────────────────────────────────────────────────
RSI_STRONG_OS  = 25.0   # extreme oversold
RSI_OVERSOLD   = 35.0   # oversold
RSI_OB         = 65.0   # overbought
RSI_STRONG_OB  = 75.0   # extreme overbought
VOL_HIGH       = 1.5    # 1.5× avg = notable
VOL_VERY_HIGH  = 2.5    # 2.5× avg = significant
BB_SQUEEZE     = 0.05   # < 5% band width  = squeeze
BB_WIDE        = 0.12   # > 12% band width = high vol


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Signal:
    """A single technical signal with verdicts at two reading levels."""
    name:        str
    category:    str     # trend | momentum | volume | volatility | macd
    emoji:       str
    verdict:     str     # bull | bear | neutral
    strength:    int     # 1 | 2 | 3
    headline:    str     # plain English for adults
    kid_explain: str     # analogy a 10-year-old understands
    value_label: str     # e.g. "RSI 62" or "SMA20 $450"

    def score(self) -> int:
        """Return +strength for bull, −strength for bear, 0 for neutral."""
        if self.verdict == "bull":
            return self.strength
        if self.verdict == "bear":
            return -self.strength
        return 0


@dataclass
class SignalReport:
    """Complete signal analysis for one ticker, ready for the dashboard."""
    ticker:           str
    price:            float
    price_change_pct: float
    overall:          str     # bull | bear | neutral
    score:            int     # 0–10 (displayed to user)
    action:           str     # BUY CALLS | BUY PUTS | WAIT
    confidence:       str     # High | Medium | Low
    headline:         str
    signals:          list[Signal] = field(default_factory=list)
    candles:          list[dict]   = field(default_factory=list)
    sma20_line:       list[dict]   = field(default_factory=list)
    sma50_line:       list[dict]   = field(default_factory=list)
    volume_bars:      list[dict]   = field(default_factory=list)
    macd_data:        list[dict]   = field(default_factory=list)
    markers:          list[dict]   = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to JSON-safe dict (signals converted from dataclasses)."""
        d = asdict(self)
        d["signals"] = [asdict(s) for s in self.signals]
        return d


# ── Indicator helpers ─────────────────────────────────────────────────────────

def _sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window).mean()


def _ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=span, adjust=False).mean()


def _rsi_series(series: pd.Series, period: int = 14) -> pd.Series:
    """Full RSI time series (not just the last value)."""
    delta = series.diff()
    gain  = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss  = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Standard MACD (12, 26, 9).

    Returns:
        (macd_line, signal_line, histogram)
    """
    fast   = _ema(close, 12)
    slow   = _ema(close, 26)
    line   = fast - slow
    sig    = _ema(line, 9)
    hist   = line - sig
    return line, sig, hist


def _bb_width(close: pd.Series, window: int = 20) -> float:
    """Bollinger Band width as fraction of mid price (e.g. 0.06 = 6%)."""
    sma = close.rolling(window).mean()
    std = close.rolling(window).std()
    mid = sma.iloc[-1]
    if not mid or np.isnan(mid):
        return 0.0
    return float((4.0 * std.iloc[-1]) / mid)


# ── Individual signal builders ────────────────────────────────────────────────

def _signal_trend(close: pd.Series) -> Signal:
    """SMA-20 / SMA-50 alignment vs current price."""
    price  = float(close.iloc[-1])
    sma20  = float(_sma(close, 20).iloc[-1])
    s50    = _sma(close, 50)
    sma50  = float(s50.iloc[-1]) if not np.isnan(s50.iloc[-1]) else sma20
    pct    = (price - sma20) / sma20 * 100

    if price > sma20 > sma50:
        strength    = 3 if (price - sma50) / sma50 > 0.03 else 2
        return Signal("Trend", "trend", "🧭", "bull", strength,
                      "Uptrend — price above both moving averages",
                      "Like a ball rolling uphill! It's above its average path — that's a good sign. 🏔️",
                      f"${price:.2f} vs SMA20 ${sma20:.2f} ({pct:+.1f}%)")

    if price > sma20 and sma20 <= sma50:
        return Signal("Trend", "trend", "🧭", "neutral", 1,
                      "Short-term recovery, still below 50-day average",
                      "Getting better lately, but hasn't fully climbed back yet. Like healing from a fall. 🩹",
                      f"${price:.2f} vs SMA20 ${sma20:.2f} ({pct:+.1f}%)")

    if price < sma20 < sma50:
        strength    = 3 if (sma50 - price) / sma50 > 0.03 else 2
        return Signal("Trend", "trend", "🧭", "bear", strength,
                      "Downtrend — price below both moving averages",
                      "Like a ball rolling downhill. It's below its average path — watch out! ⛷️",
                      f"${price:.2f} vs SMA20 ${sma20:.2f} ({pct:+.1f}%)")

    return Signal("Trend", "trend", "🧭", "neutral", 1,
                  "Choppy — no clean trend direction",
                  "Like a car stuck in traffic — moving a little but going nowhere fast. 🚗",
                  f"${price:.2f} vs SMA20 ${sma20:.2f} ({pct:+.1f}%)")


def _signal_momentum(close: pd.Series) -> Signal:
    """RSI-14 based momentum signal."""
    rsi_s = _rsi_series(close)
    rsi   = float(rsi_s.iloc[-1])
    if np.isnan(rsi):
        rsi = 50.0

    if rsi < RSI_STRONG_OS:
        return Signal("Momentum (RSI)", "momentum", "⚡", "bull", 3,
                      f"Extremely oversold (RSI {rsi:.0f}) — high-probability bounce zone",
                      "Stretched SO far down, like a super-compressed spring. When it lets go… POW! 🌀",
                      f"RSI {rsi:.1f}")
    if rsi < RSI_OVERSOLD:
        return Signal("Momentum (RSI)", "momentum", "⚡", "bull", 2,
                      f"Oversold (RSI {rsi:.0f}) — potential reversal setup",
                      "Fell too far, too fast — like a rubber band pulled way too hard. It usually snaps back! 🤸",
                      f"RSI {rsi:.1f}")
    if rsi > RSI_STRONG_OB:
        return Signal("Momentum (RSI)", "momentum", "⚡", "bear", 3,
                      f"Extremely overbought (RSI {rsi:.0f}) — likely to pull back",
                      "Gone up WAY too fast. Like a balloon overinflated — pop incoming! 🎈",
                      f"RSI {rsi:.1f}")
    if rsi > RSI_OB:
        return Signal("Momentum (RSI)", "momentum", "⚡", "bear", 2,
                      f"Overbought (RSI {rsi:.0f}) — losing steam",
                      "Sprinted too fast. Like a runner at full speed — they can't keep it up forever. 🏃",
                      f"RSI {rsi:.1f}")
    if rsi > 55:
        return Signal("Momentum (RSI)", "momentum", "⚡", "bull", 1,
                      f"Healthy momentum (RSI {rsi:.0f}) — building steam",
                      "Moving at a good pace — like a jogger who's found their rhythm. 🏅",
                      f"RSI {rsi:.1f}")
    if rsi < 45:
        return Signal("Momentum (RSI)", "momentum", "⚡", "bear", 1,
                      f"Weakening momentum (RSI {rsi:.0f}) — losing energy",
                      "Slowing down like a car running low on gas. ⛽",
                      f"RSI {rsi:.1f}")

    return Signal("Momentum (RSI)", "momentum", "⚡", "neutral", 1,
                  f"Neutral momentum (RSI {rsi:.0f}) — no extremes",
                  "Moving at a normal speed. No warning lights in either direction. 🟡",
                  f"RSI {rsi:.1f}")


def _signal_volume(close: pd.Series, volume: pd.Series) -> Signal:
    """Volume vs 20-day average, direction-aware."""
    avg_vol  = float(volume.rolling(20).mean().iloc[-1])
    last_vol = float(volume.iloc[-1])
    ratio    = last_vol / avg_vol if avg_vol > 0 else 1.0
    up       = float(close.iloc[-1]) > float(close.iloc[-2])
    r_str    = f"{ratio:.1f}×"

    if ratio >= VOL_VERY_HIGH and up:
        return Signal("Volume", "volume", "📊", "bull", 3,
                      f"Massive volume ({r_str} avg) with price rising — institutions buying",
                      "A HUGE crowd all bought at once AND the price shot up. The big money is ALL IN! 🚀",
                      f"{r_str} avg volume")
    if ratio >= VOL_HIGH and up:
        return Signal("Volume", "volume", "📊", "bull", 2,
                      f"Above-average volume ({r_str}) confirming upward move",
                      "More people than normal are buying. When a crowd wants something, the price goes UP! 📈",
                      f"{r_str} avg volume")
    if ratio >= VOL_VERY_HIGH and not up:
        return Signal("Volume", "volume", "📊", "bear", 3,
                      f"Massive volume ({r_str} avg) with price falling — institutions selling",
                      "A HUGE crowd all sold at once AND the price crashed. The big money is running away! 🏃‍♂️",
                      f"{r_str} avg volume")
    if ratio >= VOL_HIGH and not up:
        return Signal("Volume", "volume", "📊", "bear", 2,
                      f"High volume ({r_str}) on down day — distribution",
                      "More people than normal are selling. That's a warning sign. ⚠️",
                      f"{r_str} avg volume")

    return Signal("Volume", "volume", "📊", "neutral", 1,
                  f"Normal volume ({r_str} avg) — no unusual activity",
                  "Just a regular trading day. The big players are watching, not acting yet. 👀",
                  f"{r_str} avg volume")


def _signal_volatility(close: pd.Series) -> Signal:
    """Bollinger Band width — squeeze detection and IV proxy."""
    width = _bb_width(close)

    if width < BB_SQUEEZE:
        return Signal("Volatility", "volatility", "💨", "neutral", 2,
                      "BB Squeeze — stock is coiled, BIG move incoming soon",
                      "The stock has been super calm and boring for days. That usually means a GIANT move is about to happen. It's a coiled spring! 🌀",
                      f"BB Width {width*100:.1f}% — SQUEEZED")
    if width > BB_WIDE:
        return Signal("Volatility", "volatility", "💨", "bear", 1,
                      "High volatility — options are expensive right now",
                      "Wild swings! The stock is unpredictable. Options cost more when things are crazy, so this isn't the best time to buy them. 🎢",
                      f"BB Width {width*100:.1f}% — wide")

    return Signal("Volatility", "volatility", "💨", "neutral", 1,
                  "Normal volatility — options fairly priced",
                  "The stock is moving at a normal pace. Options aren't too expensive or too cheap right now. 👍",
                  f"BB Width {width*100:.1f}%")


def _signal_macd(close: pd.Series) -> Signal:
    """MACD crossover and histogram trend."""
    ml, sl, hl = _macd(close)
    if len(ml) < 3:
        return Signal("MACD", "macd", "🔀", "neutral", 1,
                      "Insufficient data for MACD",
                      "Need more price history for this signal. 📅",
                      "N/A")

    mv, sv, hv         = float(ml.iloc[-1]), float(sl.iloc[-1]), float(hl.iloc[-1])
    mv_p, sv_p, hv_p   = float(ml.iloc[-2]), float(sl.iloc[-2]), float(hl.iloc[-2])

    # Fresh crossovers = strongest
    if mv_p < sv_p and mv >= sv:
        return Signal("MACD", "macd", "🔀", "bull", 3,
                      "MACD bullish crossover — fresh BUY signal firing NOW",
                      "The 'fast speed line' just crossed ABOVE the 'slow line'. It's like a traffic light turning GREEN — GO! 🟢",
                      f"MACD {mv:+.3f} crossed signal {sv:+.3f}")
    if mv_p > sv_p and mv <= sv:
        return Signal("MACD", "macd", "🔀", "bear", 3,
                      "MACD bearish crossover — fresh SELL signal firing NOW",
                      "The 'fast speed line' just crossed BELOW the 'slow line'. Red light just turned on — STOP! 🔴",
                      f"MACD {mv:+.3f} crossed signal {sv:+.3f}")
    if mv > sv and mv > 0 and hv > hv_p:
        return Signal("MACD", "macd", "🔀", "bull", 2,
                      "MACD above signal and gaining strength",
                      "The engine is running and getting FASTER. Things are trending up with real momentum. 🚗💨",
                      f"MACD {mv:+.3f} / Hist {hv:+.3f}")
    if mv < sv and mv < 0 and hv < hv_p:
        return Signal("MACD", "macd", "🔀", "bear", 2,
                      "MACD below signal and weakening further",
                      "The engine is running in reverse and slowing down. Things are heading lower. 🔻",
                      f"MACD {mv:+.3f} / Hist {hv:+.3f}")
    if mv > 0:
        return Signal("MACD", "macd", "🔀", "bull", 1,
                      "MACD positive — mild upside bias",
                      "The speed meter is in positive territory. Leaning bullish, but not strongly. 📈",
                      f"MACD {mv:+.3f}")
    if mv < 0:
        return Signal("MACD", "macd", "🔀", "bear", 1,
                      "MACD negative — mild downside bias",
                      "The speed meter is negative. Leaning bearish, but not strongly. 📉",
                      f"MACD {mv:+.3f}")

    return Signal("MACD", "macd", "🔀", "neutral", 1,
                  "MACD near zero — no clear momentum",
                  "The speed meter is at zero. Waiting to see which way it breaks. ⏳",
                  f"MACD {mv:+.3f}")


# ── Chart data builder ────────────────────────────────────────────────────────

def _build_chart_data(
    df:         pd.DataFrame,
    sma20:      pd.Series,
    sma50:      pd.Series,
    rsi_s:      pd.Series,
    macd_l:     pd.Series,
    signal_l:   pd.Series,
    hist_l:     pd.Series,
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict], list[dict]]:
    """
    Convert OHLCV + indicator series into Lightweight Charts compatible arrays.

    Returns:
        candles, sma20_line, sma50_line, volume_bars, macd_data, markers
    """
    candles:    list[dict] = []
    sma20_data: list[dict] = []
    sma50_data: list[dict] = []
    vol_data:   list[dict] = []
    macd_data:  list[dict] = []
    markers:    list[dict] = []

    # Polygon timestamps are in milliseconds — convert to ISO date string for LW Charts
    def _ts(ms: float) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    close   = df["c"].astype(float)
    rsi_arr = rsi_s.values
    ml_arr  = macd_l.values
    sl_arr  = signal_l.values

    ts_list: list[str] = []

    for idx, row in enumerate(df.itertuples(index=False)):
        t   = _ts(float(row.t))
        o   = round(float(row.o), 2)
        h   = round(float(row.h), 2)
        l   = round(float(row.l), 2)
        c   = round(float(row.c), 2)
        v   = float(row.v)

        ts_list.append(t)
        candles.append({"time": t, "open": o, "high": h, "low": l, "close": c})
        vol_data.append({"time": t, "value": v,
                         "color": "#10B98166" if c >= o else "#EF444466"})

        s20 = sma20.iloc[idx]
        s50 = sma50.iloc[idx]
        if not np.isnan(s20):
            sma20_data.append({"time": t, "value": round(float(s20), 2)})
        if not np.isnan(s50):
            sma50_data.append({"time": t, "value": round(float(s50), 2)})

        m = macd_l.iloc[idx]
        s = signal_l.iloc[idx]
        hh = hist_l.iloc[idx]
        if not any(np.isnan(x) for x in [m, s, hh]):
            macd_data.append({
                "time":   t,
                "macd":   round(float(m), 4),
                "signal": round(float(s), 4),
                "hist":   round(float(hh), 4),
            })

    # ── Buy / sell markers ────────────────────────────────────────────────────
    for i in range(1, min(len(rsi_arr), len(ts_list))):
        pr, cr = rsi_arr[i-1], rsi_arr[i]
        if np.isnan(pr) or np.isnan(cr):
            continue
        if pr < RSI_OVERSOLD and cr >= RSI_OVERSOLD:     # oversold bounce
            markers.append({"time": ts_list[i], "position": "belowBar",
                             "color": "#10B981", "shape": "arrowUp",   "text": "RSI ↑"})
        elif pr > RSI_OB and cr <= RSI_OB:                # overbought fade
            markers.append({"time": ts_list[i], "position": "aboveBar",
                             "color": "#EF4444", "shape": "arrowDown", "text": "RSI ↓"})

    for i in range(1, min(len(ml_arr), len(sl_arr), len(ts_list))):
        if any(np.isnan(x) for x in [ml_arr[i-1], ml_arr[i], sl_arr[i-1], sl_arr[i]]):
            continue
        if ml_arr[i-1] < sl_arr[i-1] and ml_arr[i] >= sl_arr[i]:   # bullish cross
            markers.append({"time": ts_list[i], "position": "belowBar",
                             "color": "#3B82F6", "shape": "arrowUp",   "text": "MACD ↑"})
        elif ml_arr[i-1] > sl_arr[i-1] and ml_arr[i] <= sl_arr[i]: # bearish cross
            markers.append({"time": ts_list[i], "position": "aboveBar",
                             "color": "#8B5CF6", "shape": "arrowDown", "text": "MACD ↓"})

    markers.sort(key=lambda x: x["time"])
    return candles, sma20_data, sma50_data, vol_data, macd_data, markers


# ── Main entry point ──────────────────────────────────────────────────────────

def build_report(ticker: str, bars: list[dict]) -> SignalReport:
    """
    Build a complete SignalReport from Polygon OHLCV bar data.

    Args:
        ticker: Stock symbol (e.g. 'NVDA').
        bars:   List of Polygon bar dicts with keys: t, o, h, l, c, v.
                Minimum 30 bars required; 60–75 recommended.

    Returns:
        SignalReport with all signals, chart data, and an overall verdict.

    Raises:
        ValueError: If fewer than 30 bars are provided.
    """
    if len(bars) < 30:
        raise ValueError(f"Need at least 30 bars for {ticker}, got {len(bars)}")

    df     = pd.DataFrame(bars).reset_index(drop=True)
    close  = df["c"].astype(float)
    volume = df["v"].astype(float)

    # Pre-compute all indicator series
    sma20  = _sma(close, 20)
    sma50  = _sma(close, 50)
    rsi_s  = _rsi_series(close)
    ml, sl, hl = _macd(close)

    # Build the 5 signals
    signals: list[Signal] = [
        _signal_trend(close),
        _signal_momentum(close),
        _signal_volume(close, volume),
        _signal_volatility(close),
        _signal_macd(close),
    ]

    # ── Score → 0-10 display scale ────────────────────────────────────────────
    raw       = sum(s.score() for s in signals)
    max_pts   = sum(s.strength for s in signals)
    # Normalise [-max_pts, +max_pts] → [0, 10]
    norm      = (raw + max_pts) / (2 * max_pts) * 10 if max_pts > 0 else 5.0
    score     = max(0, min(10, round(norm)))

    bull_n    = sum(1 for s in signals if s.verdict == "bull")
    bear_n    = sum(1 for s in signals if s.verdict == "bear")

    if score >= 7:
        overall  = "bull"
        action   = "BUY CALLS"
        headline = f"Bullish! {bull_n}/{len(signals)} signals pointing UP"
    elif score <= 3:
        overall  = "bear"
        action   = "BUY PUTS"
        headline = f"Bearish. {bear_n}/{len(signals)} signals pointing DOWN"
    else:
        overall  = "neutral"
        action   = "WAIT"
        headline = "Mixed signals — no clear edge right now"

    confidence = (
        "High"   if abs(raw) >= max_pts * 0.6 else
        "Medium" if abs(raw) >= max_pts * 0.3 else
        "Low"
    )

    # Price change
    price      = float(close.iloc[-1])
    prev       = float(close.iloc[-2]) if len(close) >= 2 else price
    pct_chg    = (price - prev) / prev * 100 if prev else 0.0

    # Chart data
    candles, sma20_line, sma50_line, vol_bars, macd_data, markers = _build_chart_data(
        df, sma20, sma50, rsi_s, ml, sl, hl
    )

    return SignalReport(
        ticker=ticker,
        price=round(price, 2),
        price_change_pct=round(pct_chg, 2),
        overall=overall,
        score=score,
        action=action,
        confidence=confidence,
        headline=headline,
        signals=signals,
        candles=candles,
        sma20_line=sma20_line,
        sma50_line=sma50_line,
        volume_bars=vol_bars,
        macd_data=macd_data,
        markers=markers,
    )
