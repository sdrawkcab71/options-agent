"""
Technical analysis calculations.

All functions operate on pandas Series of closing prices or OHLCV DataFrames.
No external TA library required — pure pandas/numpy.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class TechSummary:
    """Result of a full technical analysis pass on one ticker."""
    ticker: str
    price: float
    sma20: float
    sma50: float
    rsi: float
    avg_volume: float
    last_volume: float
    volume_ratio: float
    bb_width_pct: float      # Bollinger Band width as % of price
    trend: str               # BULLISH / BEARISH / NEUTRAL
    momentum: str            # OVERBOUGHT / OVERSOLD / NEUTRAL
    volume_signal: str       # HIGH / NORMAL
    squeeze: bool            # True if BB width < 5%
    direction: str           # BULLISH or BEARISH (overall bias)
    summary: str             # one-line description


def calc_sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=window).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> float:
    """
    RSI using simple rolling average (Cutler's RSI variant).

    Args:
        series: Closing price series (sorted oldest → newest).
        period: Lookback period, default 14.

    Returns:
        RSI value 0–100, or 50.0 if insufficient data.
    """
    if len(series) < period + 1:
        return 50.0
    delta = series.diff().dropna()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    last_loss = loss.iloc[-1]
    if last_loss == 0:
        return 100.0
    rs = gain.iloc[-1] / last_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def calc_bb_width(series: pd.Series, window: int = 20, num_std: float = 2.0) -> float:
    """
    Bollinger Band width as a fraction of price (e.g. 0.04 = 4%).

    Args:
        series: Closing price series.
        window: Lookback period for the middle band.
        num_std: Number of standard deviations for upper/lower bands.

    Returns:
        BB width / middle band price, or 0.0 if insufficient data.
    """
    if len(series) < window:
        return 0.0
    sma = series.rolling(window).mean()
    std = series.rolling(window).std()
    upper = sma + num_std * std
    lower = sma - num_std * std
    mid = sma.iloc[-1]
    if mid == 0:
        return 0.0
    return float((upper.iloc[-1] - lower.iloc[-1]) / mid)


def analyze(ticker: str, bars: list[dict]) -> TechSummary:
    """
    Run full technical analysis on OHLCV bar data.

    Args:
        ticker: Ticker symbol for labeling.
        bars: List of Polygon bar dicts with keys: o, h, l, c, v.

    Returns:
        TechSummary with trend, momentum, volume, and squeeze signals.

    Raises:
        ValueError: If fewer than 22 bars are provided.
    """
    if len(bars) < 22:
        raise ValueError(f"Need at least 22 bars for {ticker}, got {len(bars)}")

    df = pd.DataFrame(bars)
    close = df["c"].astype(float)
    volume = df["v"].astype(float)

    sma20 = float(calc_sma(close, 20).iloc[-1])
    sma50_series = calc_sma(close, 50)
    sma50 = float(sma50_series.iloc[-1]) if not np.isnan(sma50_series.iloc[-1]) else sma20
    rsi = calc_rsi(close)
    bb_width = calc_bb_width(close)
    price = float(close.iloc[-1])
    avg_vol = float(volume.rolling(20).mean().iloc[-1])
    last_vol = float(volume.iloc[-1])
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    # Trend: price vs SMAs
    if price > sma20 > sma50:
        trend = "BULLISH"
    elif price < sma20 < sma50:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    # Momentum: RSI
    if rsi > 65:
        momentum = "OVERBOUGHT"
    elif rsi < 35:
        momentum = "OVERSOLD"
    else:
        momentum = "NEUTRAL"

    # Volume
    volume_signal = "HIGH" if vol_ratio > 1.5 else "NORMAL"

    # Overall bias: trend takes precedence, momentum can flip it
    if trend == "BULLISH" and momentum != "OVERBOUGHT":
        direction = "BULLISH"
    elif trend == "BEARISH" and momentum != "OVERSOLD":
        direction = "BEARISH"
    elif momentum == "OVERSOLD":
        direction = "BULLISH"   # mean-reversion potential
    elif momentum == "OVERBOUGHT":
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    summary = (
        f"Price ${price:.2f} | SMA20 ${sma20:.2f} | SMA50 ${sma50:.2f} | "
        f"RSI {rsi:.1f} | Vol {vol_ratio:.1f}x avg"
    )

    return TechSummary(
        ticker=ticker,
        price=price,
        sma20=sma20,
        sma50=sma50,
        rsi=rsi,
        avg_volume=avg_vol,
        last_volume=last_vol,
        volume_ratio=vol_ratio,
        bb_width_pct=bb_width,
        trend=trend,
        momentum=momentum,
        volume_signal=volume_signal,
        squeeze=bb_width < 0.05,
        direction=direction,
        summary=summary,
    )
