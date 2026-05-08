#!/usr/bin/env python3
"""
Options Agent web dashboard — SharpEdge design language.
Single-page app with real-time alerts, ticker search, and recommendations panel.

Usage:
    python server.py   →   http://localhost:7823
"""

import html as _esc
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Railway injects PORT as an env var; fall back to 7823 for local dev
PORT            = int(os.environ.get("PORT", 7823))
BASE_DIR        = Path(__file__).parent

# Load .env.local / .env for local dev (Railway sets vars directly)
try:
    from dotenv import load_dotenv as _ld
    _ld(BASE_DIR / ".env.local")
    _ld(BASE_DIR / ".env")
except ImportError:
    pass

# ── Market-bar cache (avoid hammering yfinance on every page load) ─────────────
_market_bar_cache: dict = {"data": None, "ts": 0.0}
_MARKET_BAR_TTL = 60   # seconds — refresh every minute for near-real-time quotes
RECS_CACHE_FILE = BASE_DIR / ".recs_cache.json"
MAX_ALERTS      = 20

# ── Alert levels ──────────────────────────────────────────────────────────────
ALERT_INFO   = "info"
ALERT_WARN   = "warn"
ALERT_DANGER = "danger"

# ── Market thresholds ─────────────────────────────────────────────────────────
VIX_FEAR_THRESHOLD = 30.0
VIX_WARN_THRESHOLD = 25.0
MOVE_THRESHOLD_PCT = 1.5   # % intraday move to alert on

# ── In-memory state ──────────────────────────────────────────────────────────
_alerts: list[dict] = []
_alerts_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _add_alert(message: str, level: str = ALERT_INFO, ticker: str = "") -> None:
    """Prepend a new alert to the in-memory queue (capped at MAX_ALERTS)."""
    with _alerts_lock:
        _alerts.insert(0, {
            "id":      int(time.time() * 1000),
            "ts":      datetime.now().strftime("%H:%M"),
            "message": message,
            "level":   level,
            "ticker":  ticker,
        })
        del _alerts[MAX_ALERTS:]


_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _run(args: list[str]) -> str:
    """Run agent.py with given args; return ANSI-stripped combined output."""
    try:
        res = subprocess.run(
            [sys.executable, "agent.py"] + args,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            cwd=BASE_DIR, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "ERROR: agent timed out after 120 s."
    raw = res.stdout + (res.stderr if res.returncode != 0 else "")
    return _ANSI_RE.sub("", raw).strip()


def _load_positions() -> list[dict]:
    """Load real positions from positions.json (skip comment-only placeholders)."""
    try:
        data = json.loads((BASE_DIR / "positions.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return [p for p in data if isinstance(p, dict) and "ticker" in p and "_comment" not in p]


def _get_polygon_key() -> str:
    """Return the Polygon API key from env, or empty string if not set."""
    return os.environ.get("POLYGON_API_KEY", "")


def _fetch_market_bar() -> dict:
    """
    Return a near-real-time market snapshot for the dashboard top bar.
    Cached for _MARKET_BAR_TTL seconds.

    Symbols fetched: SPY, QQQ, IWM, VIX, ^DJI (Dow), ^IXIC (Nasdaq Composite).

    Returns:
        Dict with price + % change for each symbol, keyed as e.g. spy / spy_chg.
    """
    now = time.monotonic()
    if _market_bar_cache["data"] and now - _market_bar_cache["ts"] < _MARKET_BAR_TTL:
        return _market_bar_cache["data"]  # type: ignore[return-value]

    result: dict = {}
    SYMBOLS = [
        ("SPY",   "spy"),
        ("QQQ",   "qqq"),
        ("IWM",   "iwm"),
        ("^VIX",  "vix"),
        ("^DJI",  "dji"),
        ("^IXIC", "ixic"),
    ]
    try:
        import yfinance as yf  # type: ignore[import]
        for sym, key in SYMBOLS:
            last, prev = 0.0, 0.0
            try:
                # fast_info is the lightweight yfinance path
                fi   = yf.Ticker(sym).fast_info
                last = float(fi.last_price   or 0)
                prev = float(fi.previous_close or last)
            except Exception:
                last = 0.0
            # Fallback: pull last 2 daily bars when fast_info returns zero
            if last <= 0:
                try:
                    hist = yf.Ticker(sym).history(period="2d")
                    if not hist.empty:
                        last = float(hist["Close"].iloc[-1])
                        prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last
                except Exception:
                    pass
            if last > 0:
                chg = (last - prev) / prev * 100 if prev else 0.0
                result[key]          = round(last, 2)
                result[key + "_chg"] = round(chg,  2)
    except Exception:
        pass

    if result:
        _market_bar_cache["data"] = result
        _market_bar_cache["ts"]   = now
    return result


def _fetch_signals(ticker: str) -> dict:
    """
    Build a full SignalReport for ticker using Polygon daily bars.
    Returns the report as a JSON-safe dict, or an error dict on failure.

    Args:
        ticker: Stock symbol (e.g. 'NVDA').
    Returns:
        JSON-safe dict from SignalReport.to_dict(), or {"error": "..."}.
    """
    key = _get_polygon_key()
    if not key:
        return {"error": "POLYGON_API_KEY not configured."}
    try:
        sys.path.insert(0, str(BASE_DIR))
        from src.polygon import PolygonClient   # type: ignore[import]
        from src.signals import build_report    # type: ignore[import]

        client = PolygonClient(key)
        resp   = client.daily_bars(ticker, lookback_days=90)
        bars   = resp.get("results", [])
        if len(bars) < 30:
            return {"error": f"Not enough price history for {ticker} ({len(bars)} bars)."}
        report = build_report(ticker, bars)
        return report.to_dict()
    except Exception as exc:
        return {"error": str(exc)}


def _fetch_scan_json() -> dict:
    """
    Run the full scan pipeline and return structured JSON for the visual
    scan-cards UI.  Calls src modules directly (no subprocess) so we get
    typed objects back instead of terminal text.

    Returns:
        Dict with keys: trades (list[dict]), vix (float), error (str|None).
    """
    key = _get_polygon_key()
    if not key:
        return {"error": "POLYGON_API_KEY not configured.", "trades": [], "vix": 0.0}
    try:
        sys.path.insert(0, str(BASE_DIR))
        from src.scanner import run_full_scan   # type: ignore[import]
        from src.scorer  import score_trade     # type: ignore[import]

        # Grab live VIX for the scorer's IV-environment factor
        vix = 15.0
        try:
            import yfinance as yf  # type: ignore[import]
            vix = float(yf.Ticker("^VIX").fast_info.get("last_price") or 15.0)
        except Exception:
            pass

        setups = run_full_scan(key)
        trades: list[dict] = []
        for setup in setups:
            scored = score_trade(setup, vix)
            flow   = scored.setup.flow
            tech   = scored.setup.tech
            trades.append({
                "ticker":            flow.ticker,
                "direction":         flow.direction,
                "score":             scored.score,
                "score_breakdown":   scored.score_breakdown,
                "stock_price":       flow.stock_price,
                "strike":            flow.strike,
                "contract_type":     flow.contract_type,
                "expiry":            flow.expiry,
                "dte":               flow.dte,
                "ask":               flow.ask,
                "bid":               flow.bid,
                "spread":            round(flow.spread, 2),
                "iv":                round(flow.iv * 100, 1),
                "delta":             round(flow.delta, 2),
                "vol_oi_ratio":      flow.vol_oi_ratio,
                "est_premium":       flow.estimated_premium,
                "contracts":         scored.position_size_contracts,
                "position_size_usd": scored.position_size_usd,
                "max_loss":          round(flow.ask * 100 * scored.position_size_contracts, 0),
                "target_low":        scored.setup.target_low,
                "target_high":       scored.setup.target_high,
                "stop_loss":         scored.setup.stop_loss,
                "pop_estimate":      scored.pop_estimate,
                "expected_value":    scored.expected_value,
                "why":               scored.why,
                "risk_flags":        scored.risk_flags,
                "no_trade_reason":   scored.no_trade_reason,
                "tech": {
                    "trend":          tech.trend,
                    "momentum":       tech.momentum,
                    "direction":      tech.direction,
                    "rsi":            round(tech.rsi, 1),
                    "volume_ratio":   round(tech.volume_ratio, 1),
                    "volume_signal":  tech.volume_signal,
                    "squeeze":        tech.squeeze,
                    "bb_width_pct":   round(tech.bb_width_pct * 100, 1),
                    "sma20":          round(tech.sma20, 2),
                    "sma50":          round(tech.sma50, 2),
                    "price":          round(tech.price, 2),
                    "summary":        tech.summary,
                },
            })

        # Keep the recommendations panel fresh with the latest scan
        recs = [
            {
                "ticker":    t["ticker"],
                "direction": t["direction"],
                "score":     t["score"],
                "trade":     (f"BUY {t['contracts']}x {t['ticker']} "
                              f"${t['strike']:.0f} {t['contract_type'].upper()} "
                              f"exp {t['expiry']}"),
                "ask":       str(t["ask"]),
                "max_loss":  str(int(t["max_loss"])),
                "pop":       str(int(t["pop_estimate"])),
            }
            for t in trades
            if not t["no_trade_reason"]
        ][:5]
        if recs:
            _save_recs(recs)

        return {"trades": trades, "vix": round(vix, 1), "error": None}
    except Exception as exc:
        return {"error": str(exc), "trades": [], "vix": 0.0}


def _fetch_news(tickers: list[str] | None = None) -> dict:
    """
    Fetch the latest market news from Polygon.io /v2/reference/news.
    Optionally scoped to specific ticker(s); falls back to general market news.

    Args:
        tickers: Optional list of ticker symbols to filter news by.
    Returns:
        Dict with keys: results (list[dict]), error (str|None).
    """
    key = _get_polygon_key()
    if not key:
        return {"error": "POLYGON_API_KEY not configured.", "results": []}
    try:
        import urllib.request
        if tickers:
            qs = f"ticker={tickers[0]}&limit=12&order=desc&apiKey={key}"
        else:
            qs = f"limit=15&order=desc&apiKey={key}"
        url = f"https://api.polygon.io/v2/reference/news?{qs}"
        with urllib.request.urlopen(url, timeout=12) as resp:  # noqa: S310
            raw = json.loads(resp.read())
        results = []
        for item in raw.get("results", []):
            # Trim description to keep payload light
            desc = (item.get("description") or "")[:200]
            if len(item.get("description") or "") > 200:
                desc += "…"
            results.append({
                "title":     item.get("title", ""),
                "url":       item.get("article_url", ""),
                "published": (item.get("published_utc") or "")[:16].replace("T", " "),
                "tickers":   item.get("tickers", [])[:5],
                "publisher": (item.get("publisher") or {}).get("name", ""),
                "description": desc,
            })
        return {"results": results, "error": None}
    except Exception as exc:
        return {"error": str(exc), "results": []}


def _save_positions(positions: list[dict]) -> None:
    """
    Persist the positions list to positions.json.

    Args:
        positions: List of position dicts to save.
    """
    (BASE_DIR / "positions.json").write_text(
        json.dumps(positions, indent=2),
        encoding="utf-8",
    )


def _load_recs() -> list[dict]:
    """Load cached trade recommendations from last scan."""
    try:
        return json.loads(RECS_CACHE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save_recs(recs: list[dict]) -> None:
    """Persist parsed recommendations to disk cache."""
    try:
        RECS_CACHE_FILE.write_text(json.dumps(recs, indent=2), encoding="utf-8")
    except OSError:
        pass


def _parse_recs(output: str) -> list[dict]:
    """
    Extract up to 5 trade cards from agent scan output.

    Args:
        output: Raw text from agent.py scan command.
    Returns:
        List of dicts with keys: ticker, direction, score, trade, ask, max_loss, pop.
    """
    recs: list[dict] = []
    for block in re.split(r"━{10,}", output):
        m_ticker = re.search(r"TRADE\s*#\d+\s*[—–-]+\s*([A-Z]{1,6})\s+(BULLISH|BEARISH)", block, re.I)
        m_score  = re.search(r"Signal\s+Score\s*:\s*(\d+)\s*/\s*25",                       block, re.I)
        m_trade  = re.search(r"Trade\s*:\s*(.+?)(?:\n|$)",                                  block, re.I)
        m_ask    = re.search(r"Ask\s+Price\s*:\s*\$?([\d.]+)",                               block, re.I)
        m_loss   = re.search(r"Max\s+Loss\s*:\s*\$?([\d,]+)",                               block, re.I)
        m_pop    = re.search(r"Probability\s+of\s+Profit[^:]*:\s*(\d+)%",                   block, re.I)
        if m_ticker and m_score:
            recs.append({
                "ticker":    m_ticker.group(1).upper(),
                "direction": m_ticker.group(2).upper(),
                "score":     int(m_score.group(1)),
                "trade":     m_trade.group(1).strip() if m_trade else "",
                "ask":       m_ask.group(1)  if m_ask  else "—",
                "max_loss":  m_loss.group(1) if m_loss else "—",
                "pop":       m_pop.group(1)  if m_pop  else "—",
            })
            if len(recs) >= 5:
                break
    return recs


# ─────────────────────────────────────────────────────────────────────────────
# Alert watchdog (background thread)
# ─────────────────────────────────────────────────────────────────────────────

def _watchdog() -> None:
    """Daemon thread: poll market data every 60 s and push threshold alerts."""
    time.sleep(20)   # let server fully start
    while True:
        try:
            _check_market()
        except Exception:
            pass
        time.sleep(60)


def _check_market() -> None:
    """Check VIX + SPY/QQQ for alert conditions via yfinance."""
    try:
        import yfinance as yf  # type: ignore[import]

        vix = float(yf.Ticker("^VIX").fast_info.get("last_price") or 0)
        if vix >= VIX_FEAR_THRESHOLD:
            _add_alert(
                f"VIX {vix:.1f} — FEAR regime. Naked longs prohibited; switch to spreads.",
                ALERT_DANGER, "VIX",
            )
        elif vix >= VIX_WARN_THRESHOLD:
            _add_alert(
                f"VIX {vix:.1f} — elevated. Favor defined-risk spreads.",
                ALERT_WARN, "VIX",
            )

        for sym in ("SPY", "QQQ"):
            fi   = yf.Ticker(sym).fast_info
            prev = float(fi.get("previous_close") or 0)
            last = float(fi.get("last_price")     or 0)
            if prev and last:
                chg = (last - prev) / prev * 100
                if abs(chg) >= MOVE_THRESHOLD_PCT:
                    arrow = "▲" if chg > 0 else "▼"
                    lvl   = ALERT_INFO if chg > 0 else ALERT_WARN
                    _add_alert(
                        f"{sym} {arrow} {abs(chg):.1f}% intraday — significant move.",
                        lvl, sym,
                    )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# HTML template (full SPA)
# ─────────────────────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Options Agent</title>
  <!-- TradingView Lightweight Charts v4 -->
  <script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    /* ── Design tokens (SharpEdge palette) ── */
    :root {
      --bg:      #080D18;
      --surface: #0F1623;
      --card:    #141D2E;
      --border:  #1E2D42;
      --border2: #2D3F56;
      --text:    #F1F5F9;
      --muted:   #94A3B8;
      --faint:   #475569;
      --blue:    #3B82F6;
      --green:   #10B981;
      --red:     #EF4444;
      --yellow:  #F59E0B;
      --purple:  #8B5CF6;
      --cyan:    #06B6D4;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      min-height: 100dvh; display: flex; flex-direction: column;
      overflow-x: hidden;
    }

    /* ── Header ── */
    header {
      background: var(--surface); border-bottom: 1px solid var(--border);
      padding: 12px 16px; display: flex; justify-content: space-between;
      align-items: center; flex-shrink: 0; position: sticky; top: 0; z-index: 20;
    }
    .logo-row { display: flex; align-items: center; gap: 10px; }
    .logo-icon {
      width: 34px; height: 34px; border-radius: 9px;
      background: linear-gradient(135deg, #3B82F6, #8B5CF6);
      display: flex; align-items: center; justify-content: center;
      font-size: 17px; box-shadow: 0 0 14px #3B82F655; flex-shrink: 0;
    }
    .logo-name { font-weight: 800; font-size: 15px; letter-spacing: 0.3px; }
    .logo-sub  { color: var(--faint); font-size: 10px; margin-top: 1px; }
    .header-right { display: flex; align-items: center; gap: 8px; }
    .alert-bell {
      position: relative; background: var(--card); border: 1px solid var(--border);
      border-radius: 8px; width: 36px; height: 36px; cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      font-size: 16px; transition: border-color 0.15s;
    }
    .alert-bell:hover { border-color: var(--blue); }
    .alert-badge {
      position: absolute; top: -5px; right: -5px;
      background: var(--red); color: white; border-radius: 10px;
      font-size: 9px; font-weight: 800; min-width: 16px; height: 16px;
      display: none; align-items: center; justify-content: center; padding: 0 3px;
    }
    .alert-badge.visible { display: flex; }
    .live-chip {
      background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3);
      color: var(--green); border-radius: 8px; padding: 5px 10px;
      font-size: 10px; font-weight: 700; display: flex; align-items: center; gap: 5px;
    }
    .live-dot {
      width: 6px; height: 6px; border-radius: 50%; background: var(--green);
      animation: pulse 2s infinite;
    }

    /* ── Main scroll area ── */
    main {
      flex: 1; overflow-y: auto; padding: 16px;
      padding-bottom: 84px;
    }

    /* ── Section title ── */
    .section-title {
      color: var(--muted); font-size: 10px; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.8px;
      margin: 20px 0 10px;
    }
    .section-title:first-child { margin-top: 0; }

    /* ── Command grid ── */
    .cmd-grid {
      display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
    }
    .cmd-card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 15px 13px; cursor: pointer;
      transition: all 0.18s ease;
    }
    .cmd-card:hover {
      border-color: var(--blue); background: #1A2640;
      transform: translateY(-1px); box-shadow: 0 4px 20px rgba(0,0,0,0.4);
    }
    .cmd-card.wide { grid-column: span 2; }
    .cmd-icon  { font-size: 22px; margin-bottom: 8px; }
    .cmd-title { font-weight: 700; font-size: 13px; margin-bottom: 3px; }
    .cmd-desc  { color: var(--muted); font-size: 11px; line-height: 1.4; }
    .cmd-card.wide .inner { display: flex; align-items: center; gap: 12px; }
    .cmd-card.wide .cmd-icon { margin-bottom: 0; font-size: 22px; }

    /* ── Ticker input row ── */
    #ticker-section { display: none; margin: 12px 0 4px; }
    .ticker-row { display: flex; gap: 8px; }
    .ticker-input {
      flex: 1; background: var(--card); border: 1px solid var(--border2);
      border-radius: 10px; padding: 10px 14px; color: var(--text);
      font-size: 14px; font-weight: 700; text-transform: uppercase; outline: none;
      transition: border-color 0.15s;
    }
    .ticker-input:focus   { border-color: var(--blue); }
    .ticker-input::placeholder { color: var(--faint); font-weight: 400; text-transform: none; }
    .ticker-btn {
      background: var(--blue); border: none; border-radius: 10px;
      padding: 0 20px; color: white; font-weight: 700; font-size: 13px;
      cursor: pointer; transition: background 0.15s;
    }
    .ticker-btn:hover { background: #2563EB; }

    /* ── Recommendation cards ── */
    .rec-card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 14px 15px; margin-bottom: 8px;
      transition: all 0.15s;
    }
    .rec-card:hover { border-color: var(--border2); }
    .rec-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
    .rec-ticker { font-weight: 800; font-size: 17px; }
    .rec-dir {
      font-size: 10px; font-weight: 700; padding: 3px 10px;
      border-radius: 20px; text-transform: uppercase; letter-spacing: 0.5px;
    }
    .rec-dir.bullish { background: rgba(16,185,129,0.15); color: var(--green); }
    .rec-dir.bearish { background: rgba(239,68,68,0.15);  color: var(--red);   }
    .rec-score-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
    .rec-score-label { color: var(--faint); font-size: 10px; width: 40px; flex-shrink: 0; }
    .rec-score-bar   { flex: 1; height: 5px; background: var(--border2); border-radius: 3px; overflow: hidden; }
    .rec-score-fill  { height: 100%; border-radius: 3px; background: linear-gradient(90deg, #3B82F6, #8B5CF6); }
    .rec-score-val   { font-weight: 800; font-size: 12px; width: 32px; text-align: right; flex-shrink: 0; }
    .rec-trade { color: var(--muted); font-size: 11px; margin-bottom: 8px; line-height: 1.4; }
    .rec-stats { display: flex; gap: 0; }
    .rec-stat { flex: 1; padding: 7px 0; border-top: 1px solid var(--border); }
    .rec-stat-label { color: var(--faint); font-size: 9px; font-weight: 600; text-transform: uppercase; margin-bottom: 2px; }
    .rec-stat-val   { font-size: 12px; font-weight: 700; }
    .empty-recs {
      background: var(--card); border: 1px dashed var(--border2);
      border-radius: 12px; padding: 24px; text-align: center; color: var(--faint);
      font-size: 12px; line-height: 1.7;
    }

    /* ── Position cards ── */
    .pos-card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 13px 15px; margin-bottom: 8px;
    }
    .pos-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
    .pos-ticker { font-weight: 800; font-size: 15px; }
    .pos-type   {
      font-size: 10px; font-weight: 700; padding: 3px 9px;
      border-radius: 5px; text-transform: uppercase;
    }
    .pos-type.call { background: rgba(16,185,129,0.15); color: var(--green); }
    .pos-type.put  { background: rgba(239,68,68,0.15);  color: var(--red);   }
    .pos-meta { display: flex; gap: 16px; }
    .pos-stat { flex: 1; }
    .pos-stat-label { color: var(--faint); font-size: 9px; font-weight: 600; text-transform: uppercase; margin-bottom: 2px; }
    .pos-stat-val   { font-size: 12px; font-weight: 700; }
    .empty-state { text-align: center; padding: 30px; color: var(--faint); font-size: 12px; }

    /* ── Guardrails card ── */
    .guardrail-card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 14px 15px;
      display: flex; flex-direction: column; gap: 8px;
    }
    .guardrail-row { display: flex; justify-content: space-between; align-items: center; }
    .guardrail-label { color: var(--muted); font-size: 12px; }
    .guardrail-val   { font-weight: 700; font-size: 12px; }

    /* ── Scan visual cards ── */
    .scan-empty {
      text-align: center; padding: 60px 20px; color: var(--faint); font-size: 13px;
    }
    .scan-card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 14px; margin-bottom: 16px; overflow: hidden;
      animation: fadeUp 0.25s ease both;
    }
    .scan-card.bull    { border-left: 3px solid var(--green);  }
    .scan-card.bear    { border-left: 3px solid var(--red);    }
    .scan-card.notrade { border-left: 3px solid var(--yellow); opacity: 0.78; }
    .scan-card-header {
      padding: 13px 16px 11px; display: flex; align-items: center; gap: 10px;
      border-bottom: 1px solid var(--border); flex-wrap: wrap;
    }
    .scan-ticker      { font-size: 20px; font-weight: 900; letter-spacing: 0.3px; }
    .scan-trade-line  { font-size: 11px; color: var(--muted); margin-top: 2px; }
    .scan-dir-chip {
      display: inline-flex; align-items: center; gap: 5px;
      padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 800;
    }
    .scan-dir-chip.bull { background: rgba(16,185,129,0.15); color: var(--green); }
    .scan-dir-chip.bear { background: rgba(239,68,68,0.15);  color: var(--red);   }
    .scan-score-chip {
      margin-left: auto; font-size: 12px; font-weight: 800;
      background: var(--surface); border: 1px solid var(--border2);
      padding: 3px 10px; border-radius: 8px; white-space: nowrap;
    }
    .scan-card-body {
      display: grid; grid-template-columns: 104px 1fr;
    }
    .scan-gauges {
      padding: 14px 6px 10px 14px; display: flex; flex-direction: column;
      align-items: center; gap: 10px; border-right: 1px solid var(--border);
    }
    .scan-gauge-label {
      font-size: 9px; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.5px; color: var(--faint); text-align: center;
    }
    .scan-metrics {
      padding: 12px 16px 8px; display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px;
    }
    .scan-metric-label { font-size: 9px; text-transform: uppercase; color: var(--faint); font-weight: 700; letter-spacing: 0.4px; }
    .scan-metric-val   { font-size: 13px; font-weight: 800; margin-top: 1px; line-height: 1.3; }
    .scan-tech-row {
      display: flex; gap: 10px; flex-wrap: wrap; padding: 2px 16px 12px;
    }
    .scan-tech-chip { font-size: 10px; color: var(--faint); display: flex; gap: 4px; align-items: center; }
    .scan-tech-chip-val { font-weight: 700; color: var(--text); }
    .scan-breakdown {
      padding: 10px 16px 12px; border-top: 1px solid var(--border);
    }
    .scan-breakdown-title {
      font-size: 9px; font-weight: 700; text-transform: uppercase;
      color: var(--faint); letter-spacing: 0.5px; margin-bottom: 8px;
    }
    .scan-factor { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }
    .scan-factor-name  { font-size: 10px; color: var(--muted); width: 86px; flex-shrink: 0; }
    .scan-factor-bar   { flex: 1; height: 6px; background: var(--border2); border-radius: 3px; overflow: hidden; }
    .scan-factor-fill  { height: 100%; border-radius: 3px; transition: width 0.55s ease; }
    .scan-factor-val   { font-size: 10px; font-weight: 700; width: 22px; text-align: right; color: var(--muted); }
    .scan-notrade-banner {
      padding: 10px 16px; background: rgba(245,158,11,0.08);
      border-top: 1px solid rgba(245,158,11,0.2);
      color: var(--yellow); font-size: 12px; font-weight: 700;
    }
    .scan-footer {
      padding: 10px 16px 13px; border-top: 1px solid var(--border);
      font-size: 11px; color: var(--muted); line-height: 1.65;
    }
    .scan-why-item  { display: flex; gap: 6px; margin-bottom: 2px; }
    .scan-why-bullet { color: var(--blue); flex-shrink: 0; }
    .scan-risk-item  { display: flex; gap: 6px; color: var(--yellow); margin-bottom: 2px; }

    /* ── Output view ── */
    #output-view { display: none; }
    .output-header {
      display: flex; align-items: center; gap: 12px; margin-bottom: 14px;
    }
    .back-btn {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 8px; padding: 7px 13px; color: var(--muted);
      font-size: 12px; cursor: pointer; transition: all 0.15s; flex-shrink: 0;
    }
    .back-btn:hover { border-color: var(--blue); color: var(--text); }
    .output-title    { font-weight: 800; font-size: 15px; }
    .output-subtitle { color: var(--muted); font-size: 11px; }

    /* ── Loading spinner ── */
    .loading-wrap {
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; padding: 64px 20px; gap: 16px;
    }
    .spinner {
      width: 36px; height: 36px; border-radius: 50%;
      border: 3px solid var(--border2); border-top-color: var(--blue);
      animation: spin 0.75s linear infinite;
    }
    .loading-text { color: var(--muted); font-size: 13px; }

    /* ── Output pre ── */
    .output-pre {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 18px; overflow-x: auto;
      white-space: pre-wrap; line-height: 1.7; font-size: 12.5px;
      font-family: 'SFMono-Regular', 'Consolas', 'Menlo', monospace;
      color: var(--text); word-break: break-word;
    }
    .output-pre.error { border-color: rgba(239,68,68,0.35); }
    .hi-green  { color: #10B981; font-weight: 600; }
    .hi-red    { color: #EF4444; font-weight: 600; }
    .hi-yellow { color: #F59E0B; font-weight: 600; }
    .hi-blue   { color: #3B82F6; font-weight: 600; }
    .hi-purple { color: #8B5CF6; font-weight: 600; }
    .hi-cyan   { color: #06B6D4; font-weight: 600; }

    /* ── Alerts drawer ── */
    .alerts-overlay {
      display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5);
      z-index: 40;
    }
    .alerts-overlay.open { display: block; }
    .alerts-drawer {
      position: fixed; top: 0; right: 0; height: 100%;
      width: min(380px, 92vw); background: var(--surface);
      border-left: 1px solid var(--border); z-index: 50;
      display: flex; flex-direction: column;
      transform: translateX(100%); transition: transform 0.3s cubic-bezier(0.4,0,0.2,1);
    }
    .alerts-drawer.open { transform: translateX(0); }
    .alerts-drawer-header {
      padding: 14px 18px; border-bottom: 1px solid var(--border);
      display: flex; justify-content: space-between; align-items: center; flex-shrink: 0;
    }
    .alerts-drawer-title { font-weight: 800; font-size: 15px; }
    .alerts-close {
      background: none; border: none; color: var(--muted); font-size: 22px;
      cursor: pointer; line-height: 1;
    }
    .alerts-list { flex: 1; overflow-y: auto; padding: 12px 16px; }
    .alert-item {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 10px; padding: 10px 13px; margin-bottom: 8px;
      display: flex; align-items: flex-start; gap: 10px;
      animation: fadeUp 0.2s ease;
    }
    .alert-dot {
      width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; margin-top: 4px;
    }
    .alert-dot.info   { background: var(--blue);   }
    .alert-dot.warn   { background: var(--yellow);  }
    .alert-dot.danger { background: var(--red);     }
    .alert-body   { flex: 1; }
    .alert-msg    { font-size: 12px; line-height: 1.5; }
    .alert-meta   { color: var(--faint); font-size: 10px; margin-top: 3px; }
    .no-alerts    { text-align: center; padding: 40px; color: var(--faint); font-size: 12px; }

    /* ── Alerts drawer tabs ── */
    .drawer-tabs { display: flex; border-bottom: 1px solid var(--border); flex-shrink: 0; }
    .drawer-tab {
      flex: 1; padding: 10px; font-size: 12px; font-weight: 700;
      color: var(--faint); background: none; border: none; cursor: pointer;
      border-bottom: 2px solid transparent; transition: all 0.15s;
    }
    .drawer-tab.active { color: var(--blue); border-bottom-color: var(--blue); }
    .drawer-tab-panel { display: none; flex: 1; overflow-y: auto; padding: 12px 16px; }
    .drawer-tab-panel.active { display: block; }

    /* ── News items ── */
    .news-item {
      padding: 11px 0; border-bottom: 1px solid var(--border); cursor: pointer;
    }
    .news-item:last-child { border-bottom: none; }
    .news-title { font-size: 12px; font-weight: 700; line-height: 1.4; color: var(--text); margin-bottom: 4px; }
    .news-meta  { display: flex; gap: 8px; font-size: 10px; color: var(--faint); flex-wrap: wrap; }
    .news-ticker-chip {
      background: rgba(59,130,246,0.15); color: var(--blue);
      padding: 1px 5px; border-radius: 4px; font-weight: 700;
    }
    .news-desc  { font-size: 11px; color: var(--muted); margin-top: 4px; line-height: 1.5; }
    .news-loading { text-align: center; padding: 30px; color: var(--faint); font-size: 12px; }

    /* ── Onboarding banner ── */
    .onboarding-banner {
      background: linear-gradient(135deg, rgba(59,130,246,0.12), rgba(139,92,246,0.1));
      border: 1px solid rgba(59,130,246,0.3); border-radius: 14px;
      padding: 16px; margin-bottom: 16px; position: relative;
    }
    .onboarding-header {
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 12px;
    }
    .onboarding-title { font-size: 14px; font-weight: 800; }
    .onboarding-dismiss {
      background: none; border: none; color: var(--faint); font-size: 18px;
      cursor: pointer; line-height: 1; padding: 2px 4px;
    }
    .onboarding-tips { display: flex; flex-direction: column; gap: 8px; }
    .onboarding-tip  { display: flex; gap: 10px; align-items: flex-start; font-size: 12px; line-height: 1.5; }
    .onboarding-tip-num {
      width: 20px; height: 20px; border-radius: 50%;
      background: rgba(59,130,246,0.2); color: var(--blue);
      font-size: 10px; font-weight: 900; display: flex; align-items: center;
      justify-content: center; flex-shrink: 0; margin-top: 1px;
    }
    .onboarding-visits {
      margin-top: 12px; font-size: 10px; color: var(--faint);
      display: flex; align-items: center; gap: 8px;
    }
    .onboarding-dots { display: flex; gap: 4px; }
    .onboarding-dot {
      width: 6px; height: 6px; border-radius: 50%;
      background: var(--border2); transition: background 0.2s;
    }
    .onboarding-dot.seen { background: var(--blue); }

    /* ── Toast notifications ── */
    #toast-container {
      position: fixed; top: 64px; right: 12px; z-index: 60;
      display: flex; flex-direction: column; gap: 8px; pointer-events: none;
      max-width: 320px;
    }
    .toast {
      background: var(--surface); border: 1px solid var(--border2);
      border-radius: 10px; padding: 11px 14px; display: flex;
      align-items: flex-start; gap: 10px; pointer-events: all;
      animation: slideIn 0.25s ease; box-shadow: 0 4px 20px rgba(0,0,0,0.5);
    }
    .toast.warn   { border-color: rgba(245,158,11,0.4); }
    .toast.danger { border-color: rgba(239,68,68,0.4);  }
    .toast-icon  { font-size: 15px; flex-shrink: 0; }
    .toast-msg   { font-size: 12px; line-height: 1.5; flex: 1; }
    .toast-close { background: none; border: none; color: var(--faint); cursor: pointer; font-size: 16px; }

    /* ── Bottom nav ── */
    .bottom-nav {
      position: fixed; bottom: 0; left: 0; right: 0;
      background: var(--surface); border-top: 1px solid var(--border);
      display: flex; z-index: 30;
      padding-bottom: env(safe-area-inset-bottom, 0px);
    }
    .nav-btn {
      flex: 1; background: none; border: none; cursor: pointer;
      padding: 10px 2px 7px; display: flex; flex-direction: column;
      align-items: center; gap: 2px; color: var(--faint); transition: color 0.15s;
    }
    .nav-btn.active     { color: var(--blue); }
    .nav-btn:hover:not(.active) { color: var(--muted); }
    .nav-icon  { font-size: 17px; line-height: 1; }
    .nav-label { font-size: 9px; font-weight: 500; letter-spacing: 0.2px; }
    .nav-indicator { width: 16px; height: 2px; background: var(--blue); border-radius: 1px; margin-top: 1px; display: none; }
    .nav-btn.active .nav-indicator { display: block; }

    /* ── Animations ── */
    @keyframes spin    { to { transform: rotate(360deg); } }
    @keyframes pulse   { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
    @keyframes fadeUp  { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
    @keyframes slideIn { from { opacity: 0; transform: translateX(20px); } to { opacity: 1; transform: translateX(0); } }

    /* ── Market bar (top strip) ── */
    .market-bar {
      background: var(--surface); border-bottom: 1px solid var(--border);
      padding: 7px 16px; display: flex; gap: 0; overflow-x: auto;
      flex-shrink: 0; scrollbar-width: none;
    }
    .market-bar::-webkit-scrollbar { display: none; }
    .market-chip {
      display: flex; align-items: center; gap: 6px; padding: 4px 14px 4px 0;
      border-right: 1px solid var(--border); margin-right: 14px; flex-shrink: 0;
      cursor: pointer; transition: opacity 0.15s;
    }
    .market-chip:last-child { border-right: none; }
    .market-chip:hover { opacity: 0.8; }
    .market-chip-sym  { font-weight: 800; font-size: 12px; }
    .market-chip-px   { font-size: 12px; }
    .market-chip-chg  { font-size: 11px; font-weight: 700; padding: 1px 6px; border-radius: 4px; }
    .chg-up   { color: var(--green); background: rgba(16,185,129,0.12); }
    .chg-down { color: var(--red);   background: rgba(239,68,68,0.12);  }
    .chg-flat { color: var(--muted); background: rgba(148,163,184,0.1); }

    /* ── Signals view ── */
    #signals-view { display: none; }
    .sig-search-row { display: flex; gap: 8px; margin-bottom: 14px; }

    /* ── Verdict card ── */
    .verdict-card {
      border-radius: 14px; padding: 18px; margin-bottom: 14px;
      border: 1px solid; position: relative; overflow: hidden;
    }
    .verdict-card.bull { background: rgba(16,185,129,0.08);  border-color: rgba(16,185,129,0.3); }
    .verdict-card.bear { background: rgba(239,68,68,0.08);   border-color: rgba(239,68,68,0.3);  }
    .verdict-card.neutral { background: rgba(245,158,11,0.07); border-color: rgba(245,158,11,0.3); }
    .verdict-ticker { font-size: 22px; font-weight: 900; letter-spacing: 0.5px; }
    .verdict-price  { font-size: 14px; color: var(--muted); margin-top: 2px; }
    .verdict-badge  {
      display: inline-flex; align-items: center; gap: 6px;
      font-size: 20px; font-weight: 900; margin: 12px 0 6px; letter-spacing: 0.5px;
    }
    .verdict-badge.bull    { color: var(--green);  }
    .verdict-badge.bear    { color: var(--red);    }
    .verdict-badge.neutral { color: var(--yellow); }
    .verdict-headline { color: var(--muted); font-size: 13px; margin-bottom: 12px; }
    .verdict-score-row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
    .verdict-score-bar { flex: 1; height: 8px; background: var(--border2); border-radius: 4px; overflow: hidden; }
    .verdict-score-fill { height: 100%; border-radius: 4px; transition: width 0.6s ease; }
    .verdict-score-val { font-weight: 900; font-size: 16px; width: 48px; text-align: right; }
    .verdict-action {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 8px 16px; border-radius: 10px; font-weight: 800; font-size: 13px;
      margin-top: 4px; border: 1px solid;
    }
    .verdict-action.bull    { background: rgba(16,185,129,0.15); border-color: rgba(16,185,129,0.4); color: var(--green); }
    .verdict-action.bear    { background: rgba(239,68,68,0.15);  border-color: rgba(239,68,68,0.4);  color: var(--red);   }
    .verdict-action.neutral { background: rgba(245,158,11,0.12); border-color: rgba(245,158,11,0.35); color: var(--yellow); }
    .verdict-conf { color: var(--faint); font-size: 11px; margin-left: auto; }

    /* ── Chart panels ── */
    .chart-section { margin-bottom: 14px; }
    .chart-label {
      color: var(--faint); font-size: 10px; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 5px;
    }
    .chart-legend { display: flex; gap: 14px; margin-bottom: 6px; flex-wrap: wrap; }
    .legend-item  { display: flex; align-items: center; gap: 5px; font-size: 10px; color: var(--muted); }
    .legend-dot   { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    #main-chart, #macd-chart {
      border-radius: 10px; overflow: hidden;
      border: 1px solid var(--border);
    }

    /* ── Signal cards grid ── */
    .signal-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .signal-card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 13px 13px 11px; transition: all 0.15s;
    }
    .signal-card:hover { border-color: var(--border2); }
    .signal-card.full-width { grid-column: span 2; }
    .sig-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
    .sig-name  { font-size: 11px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; }
    .sig-badge {
      font-size: 9px; font-weight: 800; padding: 2px 8px;
      border-radius: 10px; text-transform: uppercase; letter-spacing: 0.4px;
    }
    .sig-badge.bull    { background: rgba(16,185,129,0.15); color: var(--green);  }
    .sig-badge.bear    { background: rgba(239,68,68,0.15);  color: var(--red);    }
    .sig-badge.neutral { background: rgba(245,158,11,0.12); color: var(--yellow); }
    .sig-headline { font-size: 12px; font-weight: 600; margin-bottom: 6px; line-height: 1.4; }
    .sig-kid {
      font-size: 11px; color: var(--muted); line-height: 1.5;
      background: rgba(255,255,255,0.03); border-radius: 6px;
      padding: 7px 9px; margin-bottom: 7px;
    }
    .sig-value { color: var(--faint); font-size: 10px; font-family: monospace; }
    .sig-strength { display: flex; gap: 3px; margin-top: 7px; }
    .sig-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--border2); }
    .sig-dot.lit.bull    { background: var(--green);  }
    .sig-dot.lit.bear    { background: var(--red);    }
    .sig-dot.lit.neutral { background: var(--yellow); }

    /* ── Log Trade button ── */
    .pos-section-header { display: flex; justify-content: space-between; align-items: center; margin: 20px 0 10px; }
    .pos-section-header .section-title { margin: 0; }
    .log-trade-btn {
      background: rgba(59,130,246,0.12); border: 1px solid rgba(59,130,246,0.35);
      color: var(--blue); border-radius: 8px; padding: 5px 12px;
      font-size: 11px; font-weight: 700; cursor: pointer; transition: all 0.15s;
    }
    .log-trade-btn:hover { background: rgba(59,130,246,0.22); }

    /* ── Delete button on position card ── */
    .pos-delete {
      background: none; border: none; color: var(--faint);
      font-size: 18px; cursor: pointer; padding: 0; line-height: 1; transition: color 0.15s;
    }
    .pos-delete:hover { color: var(--red); }
    .pos-cost { color: var(--faint); font-size: 11px; margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border); }

    /* ── Trade modal (bottom sheet) ── */
    .modal-overlay {
      display: none; position: fixed; inset: 0;
      background: rgba(0,0,0,0.6); z-index: 50;
    }
    .modal {
      position: fixed; bottom: 0; left: 0; right: 0;
      background: var(--surface); border-top: 1px solid var(--border);
      border-radius: 16px 16px 0 0; z-index: 60;
      max-height: 88vh; overflow-y: auto;
      transform: translateY(100%);
      transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .modal.open { transform: translateY(0); }
    .modal-header {
      padding: 16px 18px 12px; border-bottom: 1px solid var(--border);
      display: flex; justify-content: space-between; align-items: center;
      position: sticky; top: 0; background: var(--surface); z-index: 1;
    }
    .modal-title { font-weight: 800; font-size: 16px; }
    .modal-close { background: none; border: none; color: var(--muted); font-size: 24px; cursor: pointer; line-height: 1; }
    .modal-body  { padding: 16px 18px 32px; }

    /* ── Form ── */
    .form-row   { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .form-group { display: flex; flex-direction: column; gap: 5px; margin-bottom: 14px; }
    .form-label { color: var(--muted); font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px; }
    .form-input {
      background: var(--card); border: 1px solid var(--border2);
      border-radius: 9px; padding: 10px 12px; color: var(--text);
      font-size: 14px; outline: none; transition: border-color 0.15s; width: 100%;
    }
    .form-input:focus { border-color: var(--blue); }
    .form-input.uc { text-transform: uppercase; }
    select.form-input { cursor: pointer; }
    select.form-input option { background: var(--surface); }
    input[type="date"].form-input::-webkit-calendar-picker-indicator { filter: invert(0.5); cursor: pointer; }

    /* ── Cost summary box ── */
    .cost-summary {
      background: rgba(59,130,246,0.07); border: 1px solid rgba(59,130,246,0.2);
      border-radius: 9px; padding: 12px 14px; margin-bottom: 14px;
      display: none; flex-direction: column; gap: 6px;
    }
    .cost-summary.visible { display: flex; }
    .cost-row { display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); }
    .cost-val  { font-weight: 700; color: var(--text); }
    .cost-warn { color: var(--yellow) !important; }
    .cost-over { color: var(--red)    !important; }

    /* ── Submit button ── */
    .submit-btn {
      width: 100%; background: var(--blue); border: none; border-radius: 10px;
      padding: 13px; color: white; font-weight: 700; font-size: 14px;
      cursor: pointer; transition: background 0.15s;
    }
    .submit-btn:hover    { background: #2563EB; }
    .submit-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    /* ── Scrollbar ── */
    ::-webkit-scrollbar { width: 3px; }
    ::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 2px; }
  </style>
</head>
<body>

<!-- ── Header ──────────────────────────────────────────────────────────────── -->
<header>
  <div class="logo-row">
    <div class="logo-icon">📈</div>
    <div>
      <div class="logo-name">Options Agent</div>
      <div class="logo-sub">$5,000 capital · Fidelity · 2-week horizon</div>
    </div>
  </div>
  <div class="header-right">
    <div class="live-chip"><div class="live-dot"></div>Live</div>
    <div class="alert-bell" onclick="toggleAlerts()" title="Alerts">
      🔔
      <div class="alert-badge" id="alert-badge">0</div>
    </div>
  </div>
</header>

<!-- ── Market Bar ────────────────────────────────────────────────────────── -->
<div class="market-bar" id="market-bar">
  <div class="market-chip" onclick="runCmd('pulse','Market Pulse','SPY · QQQ · VIX · Regime')">
    <span class="market-chip-sym">SPY</span>
    <span class="market-chip-px" id="mb-spy-px">—</span>
    <span class="market-chip-chg chg-flat" id="mb-spy-chg">—</span>
  </div>
  <div class="market-chip" onclick="runCmd('pulse','Market Pulse','SPY · QQQ · VIX · Regime')">
    <span class="market-chip-sym">QQQ</span>
    <span class="market-chip-px" id="mb-qqq-px">—</span>
    <span class="market-chip-chg chg-flat" id="mb-qqq-chg">—</span>
  </div>
  <div class="market-chip" onclick="runCmd('pulse','Market Pulse','SPY · QQQ · VIX · Regime')">
    <span class="market-chip-sym">IWM</span>
    <span class="market-chip-px" id="mb-iwm-px">—</span>
    <span class="market-chip-chg chg-flat" id="mb-iwm-chg">—</span>
  </div>
  <div class="market-chip" onclick="runCmd('pulse','Market Pulse','SPY · QQQ · VIX · Regime')">
    <span class="market-chip-sym">DOW</span>
    <span class="market-chip-px" id="mb-dji-px">—</span>
    <span class="market-chip-chg chg-flat" id="mb-dji-chg">—</span>
  </div>
  <div class="market-chip" onclick="runCmd('pulse','Market Pulse','SPY · QQQ · VIX · Regime')">
    <span class="market-chip-sym">COMP</span>
    <span class="market-chip-px" id="mb-ixic-px">—</span>
    <span class="market-chip-chg chg-flat" id="mb-ixic-chg">—</span>
  </div>
  <div class="market-chip">
    <span class="market-chip-sym">VIX</span>
    <span class="market-chip-px" id="mb-vix-px">—</span>
    <span class="market-chip-chg chg-flat" id="mb-vix-chg">—</span>
  </div>
</div>

<!-- ── Main ─────────────────────────────────────────────────────────────────── -->
<main id="main-scroll">

  <!-- HOME VIEW -->
  <div id="home-view">

    <!-- Onboarding banner (hidden by JS after 5 visits) -->
    <div class="onboarding-banner" id="onboarding-banner" style="display:none">
      <div class="onboarding-header">
        <div class="onboarding-title">👋 Welcome to Options Agent</div>
        <button class="onboarding-dismiss" onclick="dismissOnboarding()" title="Dismiss forever">×</button>
      </div>
      <div class="onboarding-tips">
        <div class="onboarding-tip">
          <div class="onboarding-tip-num">1</div>
          <div><strong>Run a Full Scan</strong> — it screens your entire watchlist for unusual options flow that agrees with the technical trend. Look for scores ≥ 15/25.</div>
        </div>
        <div class="onboarding-tip">
          <div class="onboarding-tip-num">2</div>
          <div><strong>Check Market Pulse</strong> first — if VIX &gt; 25 the agent shifts to spreads. If regime is RISK-OFF, stand down on new entries.</div>
        </div>
        <div class="onboarding-tip">
          <div class="onboarding-tip-num">3</div>
          <div><strong>Signals tab</strong> — enter any ticker for a plain-English verdict with interactive candlestick + MACD charts and buy/sell markers.</div>
        </div>
        <div class="onboarding-tip">
          <div class="onboarding-tip-num">4</div>
          <div><strong>Log your trades</strong> — tap the 📝 button next to Open Positions to record your Fidelity fills. The agent tracks P&amp;L and flags exits.</div>
        </div>
        <div class="onboarding-tip">
          <div class="onboarding-tip-num">5</div>
          <div><strong>Alerts &amp; News</strong> — the bell icon streams live VIX/SPY threshold alerts and the latest market news relevant to your positions.</div>
        </div>
      </div>
      <div class="onboarding-visits">
        <span>Auto-hides after 5 visits</span>
        <div class="onboarding-dots" id="onboarding-dots"></div>
      </div>
    </div>

    <p class="section-title">Quick Actions</p>
    <div class="cmd-grid">
      <div class="cmd-card" onclick="runCmd('pulse','Market Pulse','SPY · QQQ · VIX · Regime')">
        <div class="cmd-icon">📡</div>
        <div class="cmd-title">Market Pulse</div>
        <div class="cmd-desc">SPY, QQQ, VIX, regime + macro events</div>
      </div>
      <div class="cmd-card" onclick="runCmd('scan','Full Scan','Flow + Technicals + Trade Cards')">
        <div class="cmd-icon">🔍</div>
        <div class="cmd-title">Full Scan</div>
        <div class="cmd-desc">Complete pipeline — flow, scoring, trade cards</div>
      </div>
      <div class="cmd-card" onclick="showTickerInput('flow')">
        <div class="cmd-icon">🌊</div>
        <div class="cmd-title">Flow Scanner</div>
        <div class="cmd-desc">Unusual options activity for a ticker</div>
      </div>
      <div class="cmd-card" onclick="showTickerInput('chart')">
        <div class="cmd-icon">📊</div>
        <div class="cmd-title">Chart Analysis</div>
        <div class="cmd-desc">SMA, RSI, volume, Bollinger bands</div>
      </div>
    </div>

    <!-- Ticker input (hidden until Flow/Chart clicked) -->
    <div id="ticker-section">
      <div class="ticker-row">
        <input id="ticker-input" class="ticker-input" placeholder="Enter ticker…" maxlength="6" />
        <button class="ticker-btn" onclick="submitTicker()">Go →</button>
      </div>
    </div>

    <div class="cmd-grid" style="margin-top:10px;">
      <div class="cmd-card wide" onclick="runCmd('monitor','Position Monitor','P&L · Exit signals · Roll alerts')">
        <div class="inner">
          <div class="cmd-icon">👁️</div>
          <div>
            <div class="cmd-title">Monitor Positions</div>
            <div class="cmd-desc">P&L check · exit signals · roll recommendations</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Recommendations panel -->
    <p class="section-title">Latest Recommendations <span style="color:var(--faint);font-weight:400;text-transform:none;letter-spacing:0">(up to 5 · refreshes on Scan)</span></p>
    <div id="recs-panel">
      <div class="empty-recs">
        Run a <strong style="color:var(--text)">Full Scan</strong> to populate trade recommendations.<br>
        Results are cached and shown here between sessions.
      </div>
    </div>

    <!-- Open positions -->
    <div class="pos-section-header">
      <p class="section-title">Open Positions</p>
      <button class="log-trade-btn" onclick="openTradeModal()">+ Log Trade</button>
    </div>
    <div id="positions-panel"><div class="empty-state">Loading…</div></div>

    <!-- Guardrails -->
    <p class="section-title">Guardrails</p>
    <div class="guardrail-card">
      <div class="guardrail-row">
        <span class="guardrail-label">Max single trade</span>
        <span class="guardrail-val" style="color:var(--yellow)">$1,000 · 20% capital</span>
      </div>
      <div class="guardrail-row">
        <span class="guardrail-label">Max open positions</span>
        <span class="guardrail-val" style="color:var(--yellow)">4 trades</span>
      </div>
      <div class="guardrail-row">
        <span class="guardrail-label">Stop loss</span>
        <span class="guardrail-val" style="color:var(--red)">–50% on contract</span>
      </div>
      <div class="guardrail-row">
        <span class="guardrail-label">Take profit</span>
        <span class="guardrail-val" style="color:var(--green)">+75–100%</span>
      </div>
      <div class="guardrail-row">
        <span class="guardrail-label">Min score to trade</span>
        <span class="guardrail-val" style="color:var(--blue)">15 / 25</span>
      </div>
      <div class="guardrail-row">
        <span class="guardrail-label">No 0DTE options</span>
        <span class="guardrail-val" style="color:var(--red)">Never</span>
      </div>
    </div>

  </div><!-- /#home-view -->

  <!-- SIGNALS VIEW -->
  <div id="signals-view">

    <!-- Ticker search -->
    <div class="sig-search-row">
      <input id="sig-ticker" class="ticker-input" placeholder="Enter ticker (e.g. NVDA)" maxlength="6" />
      <button class="ticker-btn" onclick="analyzeSignals()">Analyze ⚡</button>
    </div>

    <!-- Loading / empty state -->
    <div id="sig-empty" style="text-align:center;padding:50px 20px;color:var(--faint);font-size:13px">
      Enter a ticker above to see signals, chart, and a plain-English verdict.<br>
      <span style="font-size:24px;display:block;margin-top:12px">📊</span>
    </div>

    <!-- Verdict card -->
    <div id="sig-verdict" style="display:none"></div>

    <!-- Main candlestick chart -->
    <div id="sig-chart-section" style="display:none">
      <div class="chart-section">
        <div class="chart-label">Price · SMA 20 · SMA 50</div>
        <div class="chart-legend">
          <div class="legend-item"><div class="legend-dot" style="background:#10B981"></div>Green candle = up day</div>
          <div class="legend-item"><div class="legend-dot" style="background:#EF4444"></div>Red candle = down day</div>
          <div class="legend-item"><div class="legend-dot" style="background:#3B82F6"></div>SMA 20-day</div>
          <div class="legend-item"><div class="legend-dot" style="background:#8B5CF6"></div>SMA 50-day</div>
          <div class="legend-item"><div class="legend-dot" style="background:#10B981"></div>↑ Buy signal</div>
          <div class="legend-item"><div class="legend-dot" style="background:#EF4444"></div>↓ Sell signal</div>
        </div>
        <div id="main-chart" style="height:260px"></div>
      </div>
      <div class="chart-section">
        <div class="chart-label">MACD — momentum engine</div>
        <div class="chart-legend">
          <div class="legend-item"><div class="legend-dot" style="background:#3B82F6"></div>MACD line</div>
          <div class="legend-item"><div class="legend-dot" style="background:#F59E0B"></div>Signal line</div>
          <div class="legend-item"><div class="legend-dot" style="background:#10B981"></div>Green bars = building momentum</div>
          <div class="legend-item"><div class="legend-dot" style="background:#EF4444"></div>Red bars = losing momentum</div>
        </div>
        <div id="macd-chart" style="height:110px"></div>
      </div>
    </div>

    <!-- Signal cards -->
    <div id="sig-cards" style="display:none">
      <p class="section-title" style="margin-top:4px">Signal Breakdown</p>
      <div class="signal-grid" id="sig-grid"></div>
    </div>

  </div><!-- /#signals-view -->

  <!-- OUTPUT VIEW -->
  <div id="output-view">
    <div class="output-header">
      <button class="back-btn" onclick="goHome()">← Back</button>
      <div>
        <div class="output-title" id="output-title">Loading…</div>
        <div class="output-subtitle" id="output-subtitle"></div>
      </div>
    </div>
    <div id="output-content"></div>
  </div>

</main>

<!-- ── Alerts Drawer ──────────────────────────────────────────────────────── -->
<div class="alerts-overlay" id="alerts-overlay" onclick="toggleAlerts()"></div>
<div class="alerts-drawer" id="alerts-drawer">
  <div class="alerts-drawer-header">
    <div class="alerts-drawer-title">🔔 Alerts &amp; News</div>
    <button class="alerts-close" onclick="toggleAlerts()">×</button>
  </div>
  <div class="drawer-tabs">
    <button class="drawer-tab active" id="tab-alerts" onclick="switchDrawerTab('alerts')">Alerts</button>
    <button class="drawer-tab" id="tab-news"   onclick="switchDrawerTab('news')">Market News</button>
  </div>
  <div class="drawer-tab-panel active" id="panel-alerts">
    <div class="alerts-list" id="alerts-list" style="padding:0">
      <div class="no-alerts" id="no-alerts-msg">No alerts yet.<br>Monitoring VIX and SPY/QQQ for threshold events.</div>
    </div>
  </div>
  <div class="drawer-tab-panel" id="panel-news">
    <div id="news-list"><div class="news-loading">⏳ Loading news…</div></div>
  </div>
</div>

<!-- ── Trade Log Modal ──────────────────────────────────────────────────────── -->
<div class="modal-overlay" id="modal-overlay" onclick="closeModal()"></div>
<div class="modal" id="trade-modal">
  <div class="modal-header">
    <div class="modal-title">📝 Log Trade</div>
    <button class="modal-close" onclick="closeModal()">×</button>
  </div>
  <div class="modal-body">
    <form id="trade-form" onsubmit="submitTrade(event)" autocomplete="off">

      <div class="form-row">
        <div class="form-group">
          <div class="form-label">Symbol</div>
          <input id="f-ticker" class="form-input uc" type="text" placeholder="NVDA" maxlength="6" required>
        </div>
        <div class="form-group">
          <div class="form-label">Type</div>
          <select id="f-type" class="form-input">
            <option value="call">Call ▲</option>
            <option value="put">Put ▼</option>
          </select>
        </div>
      </div>

      <div class="form-row">
        <div class="form-group">
          <div class="form-label">Strike ($)</div>
          <input id="f-strike" class="form-input" type="number" placeholder="175.00" step="0.50" min="0.01" required>
        </div>
        <div class="form-group">
          <div class="form-label">Expiry</div>
          <input id="f-expiry" class="form-input" type="date" required>
        </div>
      </div>

      <div class="form-row">
        <div class="form-group">
          <div class="form-label">Contracts</div>
          <input id="f-contracts" class="form-input" type="number" placeholder="1" min="1" value="1" required>
        </div>
        <div class="form-group">
          <div class="form-label">Fill Price ($/contract)</div>
          <input id="f-entry" class="form-input" type="number" placeholder="2.50" step="0.01" min="0.01" required>
        </div>
      </div>

      <div class="form-group">
        <div class="form-label">Notes <span style="color:var(--faint);text-transform:none;font-weight:400">(optional)</span></div>
        <input id="f-notes" class="form-input" type="text" placeholder="e.g. Flow signal, breakout play">
      </div>

      <!-- Live cost calculator -->
      <div class="cost-summary" id="cost-summary">
        <div class="cost-row">
          <span>Total cost</span>
          <span class="cost-val" id="cost-total">$0</span>
        </div>
        <div class="cost-row">
          <span>Max loss (if expires worthless)</span>
          <span class="cost-val cost-over" id="cost-maxloss">$0</span>
        </div>
        <div class="cost-row" id="cost-pct-row">
          <span>% of $5,000 capital</span>
          <span class="cost-val" id="cost-pct">0%</span>
        </div>
      </div>

      <button type="submit" class="submit-btn" id="submit-btn">Log Trade</button>
    </form>
  </div>
</div>

<!-- ── Toast container ─────────────────────────────────────────────────────── -->
<div id="toast-container"></div>

<!-- ── Bottom Nav ────────────────────────────────────────────────────────── -->
<nav class="bottom-nav">
  <button class="nav-btn active" id="nav-home" onclick="navTo('home')">
    <span class="nav-icon">🏠</span>
    <span class="nav-label">Home</span>
    <div class="nav-indicator"></div>
  </button>
  <button class="nav-btn" id="nav-pulse" onclick="runCmd('pulse','Market Pulse','SPY · QQQ · VIX · Regime')">
    <span class="nav-icon">📡</span>
    <span class="nav-label">Pulse</span>
    <div class="nav-indicator"></div>
  </button>
  <button class="nav-btn" id="nav-scan" onclick="runCmd('scan','Full Scan','Flow + Technicals + Trade Cards')">
    <span class="nav-icon">🔍</span>
    <span class="nav-label">Scan</span>
    <div class="nav-indicator"></div>
  </button>
  <button class="nav-btn" id="nav-monitor" onclick="runCmd('monitor','Monitor','Open positions · P&L · Exit signals')">
    <span class="nav-icon">👁️</span>
    <span class="nav-label">Monitor</span>
    <div class="nav-indicator"></div>
  </button>
  <button class="nav-btn" id="nav-signals" onclick="navTo('signals')">
    <span class="nav-icon">⚡</span>
    <span class="nav-label">Signals</span>
    <div class="nav-indicator"></div>
  </button>
  <button class="nav-btn" id="nav-alerts" onclick="toggleAlerts()">
    <span class="nav-icon">🔔</span>
    <span class="nav-label">Alerts</span>
    <div class="nav-indicator"></div>
  </button>
</nav>

<script>
  // ── State ──────────────────────────────────────────────────────────────────
  let _tickerMode  = null;   // 'flow' | 'chart'
  let _alertCount  = 0;
  let _lastAlertId = 0;
  let _activeNav   = 'home';

  // ── Cookie helpers ──────────────────────────────────────────────────────────
  function getCookie(name) {
    const m = document.cookie.match('(?:^|; )' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[1]) : null;
  }
  function setCookie(name, value, days) {
    const exp = new Date(Date.now() + days * 864e5).toUTCString();
    document.cookie = `${name}=${encodeURIComponent(value)};expires=${exp};path=/;SameSite=Lax`;
  }

  // ── Onboarding (cookie-tracked, hides after 5 visits) ──────────────────────
  function initOnboarding() {
    const MAX_VISITS = 5;
    const visits = parseInt(getCookie('oag_visits') || '0') + 1;
    setCookie('oag_visits', visits, 365);
    const banner = document.getElementById('onboarding-banner');
    const dots   = document.getElementById('onboarding-dots');
    // Render visit progress dots
    if (dots) {
      dots.innerHTML = Array.from({ length: MAX_VISITS }, (_, i) =>
        `<div class="onboarding-dot${i < visits ? ' seen' : ''}"></div>`
      ).join('');
    }
    if (visits <= MAX_VISITS) {
      banner.style.display = 'block';
    }
  }

  function dismissOnboarding() {
    document.getElementById('onboarding-banner').style.display = 'none';
    setCookie('oag_visits', '99', 365);   // skip forever
  }

  // ── Drawer tab switching ────────────────────────────────────────────────────
  function switchDrawerTab(tab) {
    ['alerts','news'].forEach(t => {
      document.getElementById('tab-' + t).classList.toggle('active', t === tab);
      document.getElementById('panel-' + t).classList.toggle('active', t === tab);
    });
    if (tab === 'news') loadNews();
  }

  // ── Market news (Polygon /api/news) ────────────────────────────────────────
  let _newsLoaded = false;
  async function loadNews(tickers) {
    const el = document.getElementById('news-list');
    if (!el) return;
    // Only show loading spinner on first fetch
    if (!_newsLoaded) el.innerHTML = '<div class="news-loading">⏳ Fetching market news…</div>';
    try {
      const qs = tickers ? tickers.map(t => 't=' + encodeURIComponent(t)).join('&') : '';
      const data = await fetch('/api/news' + (qs ? '?' + qs : '')).then(r => r.json());
      if (data.error) {
        el.innerHTML = `<div class="news-loading" style="color:var(--red)">⚠ ${data.error}</div>`;
        return;
      }
      const items = (data.results || []);
      if (items.length === 0) {
        el.innerHTML = '<div class="news-loading">No news found.</div>';
        return;
      }
      el.innerHTML = items.map(n => {
        const chips = (n.tickers || []).map(tk =>
          `<span class="news-ticker-chip">${tk}</span>`
        ).join('');
        return `<div class="news-item" onclick="window.open('${n.url}','_blank')">
          <div class="news-title">${n.title}</div>
          ${n.description ? `<div class="news-desc">${n.description}</div>` : ''}
          <div class="news-meta">
            ${n.publisher ? `<span>${n.publisher}</span>` : ''}
            ${n.published ? `<span>${n.published.replace('T',' ')}</span>` : ''}
            ${chips}
          </div>
        </div>`;
      }).join('');
      _newsLoaded = true;
    } catch (e) {
      el.innerHTML = `<div class="news-loading" style="color:var(--red)">Network error</div>`;
    }
  }

  // ── Boot: load recs + positions + market bar, start SSE ──────────────────
  window.addEventListener('DOMContentLoaded', () => {
    initOnboarding();
    loadRecs();
    loadPositions();
    loadMarketBar();
    startSSE();
    // Re-poll market bar every 60 seconds for real-time quotes
    setInterval(loadMarketBar, 60 * 1000);
    // Refresh news every 10 minutes when drawer is open
    setInterval(() => { if (_newsLoaded) loadNews(); }, 10 * 60 * 1000);
  });

  // ── Market bar ─────────────────────────────────────────────────────────────
  async function loadMarketBar() {
    try {
      const d = await fetch('/api/market-bar').then(r => r.json());

      // Generic setter — prefix='$' for ETFs, '' for large indices
      const setChip = (id, val, chg, prefix = '$', decimals = 2) => {
        const px = document.getElementById(id + '-px');
        const ch = document.getElementById(id + '-chg');
        if (!px || !ch) return;
        if (val) {
          // Use toLocaleString for large numbers (DJI, IXIC) to add commas
          px.textContent = prefix + (val >= 1000
            ? val.toLocaleString('en-US', { maximumFractionDigits: 0 })
            : val.toFixed(decimals));
        } else {
          px.textContent = '—';
        }
        if (chg !== undefined && chg !== null) {
          const sign = chg >= 0 ? '+' : '';
          ch.textContent = sign + chg.toFixed(2) + '%';
          ch.className = 'market-chip-chg ' + (chg > 0.05 ? 'chg-up' : chg < -0.05 ? 'chg-down' : 'chg-flat');
        }
      };

      setChip('mb-spy',  d.spy,  d.spy_chg);
      setChip('mb-qqq',  d.qqq,  d.qqq_chg);
      setChip('mb-iwm',  d.iwm,  d.iwm_chg);
      setChip('mb-dji',  d.dji,  d.dji_chg,  '', 0);   // Dow — no $, whole numbers
      setChip('mb-ixic', d.ixic, d.ixic_chg, '', 0);   // Nasdaq — no $, whole numbers

      // VIX: show level + regime label instead of % change
      const vixEl = document.getElementById('mb-vix-px');
      const vixCh = document.getElementById('mb-vix-chg');
      if (vixEl && d.vix) {
        vixEl.textContent = d.vix.toFixed(1);
        const label = d.vix >= 30 ? 'FEAR' : d.vix >= 25 ? 'HIGH' : d.vix >= 20 ? 'OK' : 'LOW';
        vixCh.textContent = label;
        vixCh.className = 'market-chip-chg ' + (d.vix >= 25 ? 'chg-down' : d.vix <= 15 ? 'chg-up' : 'chg-flat');
      }
    } catch (_) { /* silent — market bar is cosmetic */ }
  }

  // ── Signals page ───────────────────────────────────────────────────────────
  let _lastSigTicker = '';
  let _mainChart     = null;
  let _macdChart     = null;

  document.getElementById('sig-ticker').addEventListener('keydown', e => {
    if (e.key === 'Enter') analyzeSignals();
  });

  function analyzeSignals() {
    const t = document.getElementById('sig-ticker').value.trim().toUpperCase();
    if (!t) return;
    _lastSigTicker = t;
    navTo('signals');
    _loadSignals(t);
  }

  async function _loadSignals(ticker) {
    // Show loading
    document.getElementById('sig-empty').innerHTML =
      `<div class="loading-wrap"><div class="spinner"></div><div class="loading-text">Analyzing ${ticker}…</div></div>`;
    document.getElementById('sig-empty').style.display    = 'block';
    document.getElementById('sig-verdict').style.display  = 'none';
    document.getElementById('sig-chart-section').style.display = 'none';
    document.getElementById('sig-cards').style.display    = 'none';

    try {
      const data = await fetch('/api/signals?t=' + ticker).then(r => r.json());
      if (data.error) {
        document.getElementById('sig-empty').innerHTML =
          `<div style="color:var(--red);padding:40px;text-align:center">⚠️ ${data.error}</div>`;
        return;
      }
      document.getElementById('sig-empty').style.display = 'none';
      _renderVerdict(data);
      _renderChart(data);
      _renderSignalCards(data.signals);
    } catch (e) {
      document.getElementById('sig-empty').innerHTML =
        `<div style="color:var(--red);padding:40px;text-align:center">⚠️ ${e.message}</div>`;
    }
  }

  // ── Verdict card renderer ──────────────────────────────────────────────────
  function _renderVerdict(d) {
    const el     = document.getElementById('sig-verdict');
    const pct    = (d.score / 10) * 100;
    const colour = d.overall === 'bull' ? '#10B981' : d.overall === 'bear' ? '#EF4444' : '#F59E0B';
    const label  = d.overall === 'bull' ? '🟢 BULLISH' : d.overall === 'bear' ? '🔴 BEARISH' : '🟡 MIXED';
    const chgCls = d.price_change_pct >= 0 ? 'chg-up' : 'chg-down';
    const chgStr = (d.price_change_pct >= 0 ? '+' : '') + d.price_change_pct.toFixed(2) + '% today';
    const barGrad = d.overall === 'bull'
      ? 'linear-gradient(90deg,#10B981,#06B6D4)'
      : d.overall === 'bear'
      ? 'linear-gradient(90deg,#EF4444,#F59E0B)'
      : 'linear-gradient(90deg,#F59E0B,#94A3B8)';

    el.className      = `verdict-card ${d.overall}`;
    el.style.display  = 'block';
    el.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:flex-start">
        <div>
          <div class="verdict-ticker">${d.ticker}</div>
          <div class="verdict-price">$${d.price.toFixed(2)} &nbsp;<span class="market-chip-chg ${chgCls}">${chgStr}</span></div>
        </div>
        <div style="text-align:right">
          <div style="color:var(--faint);font-size:10px;font-weight:700;text-transform:uppercase">Confidence</div>
          <div style="font-weight:800;font-size:13px;color:${colour}">${d.confidence}</div>
        </div>
      </div>
      <div class="verdict-badge ${d.overall}">${label}</div>
      <div class="verdict-headline">${d.headline}</div>
      <div class="verdict-score-row">
        <span style="color:var(--faint);font-size:11px;width:44px">Score</span>
        <div class="verdict-score-bar">
          <div class="verdict-score-fill" style="width:${pct}%;background:${barGrad}"></div>
        </div>
        <span class="verdict-score-val" style="color:${colour}">${d.score}/10</span>
      </div>
      <div style="display:flex;align-items:center;gap:10px;margin-top:6px">
        <div class="verdict-action ${d.overall}">${d.action}</div>
        <div class="verdict-conf">Not financial advice</div>
      </div>`;
  }

  // ── Chart renderer (TradingView Lightweight Charts) ───────────────────────
  function _renderChart(d) {
    const section = document.getElementById('sig-chart-section');
    section.style.display = 'block';

    const chartOpts = (container) => ({
      layout:   { background: { color: '#141D2E' }, textColor: '#94A3B8' },
      grid:     { vertLines: { color: '#1E2D42' }, horzLines: { color: '#1E2D42' } },
      crosshair: { mode: 0 },
      rightPriceScale: { borderColor: '#1E2D42' },
      timeScale: { borderColor: '#1E2D42', timeVisible: true },
      handleScroll: { mouseWheel: false, pressedMouseMove: true },
      handleScale:  { mouseWheel: false, pinch: true },
      width:  container.clientWidth  || 340,
      height: parseInt(container.style.height) || 260,
    });

    // ── Main chart (candlestick + SMA20 + SMA50 + volume) ─────────────────
    const mainEl = document.getElementById('main-chart');
    if (_mainChart) { try { _mainChart.remove(); } catch(_){} }
    _mainChart = LightweightCharts.createChart(mainEl, chartOpts(mainEl));

    const candleSeries = _mainChart.addCandlestickSeries({
      upColor: '#10B981', downColor: '#EF4444',
      borderUpColor: '#10B981', borderDownColor: '#EF4444',
      wickUpColor: '#10B981', wickDownColor: '#EF4444',
    });
    const sma20Series = _mainChart.addLineSeries({
      color: '#3B82F6', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false,
    });
    const sma50Series = _mainChart.addLineSeries({
      color: '#8B5CF6', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false,
    });

    if (d.candles?.length)   candleSeries.setData(d.candles);
    if (d.sma20_line?.length) sma20Series.setData(d.sma20_line);
    if (d.sma50_line?.length) sma50Series.setData(d.sma50_line);
    if (d.markers?.length)   candleSeries.setMarkers(d.markers);
    _mainChart.timeScale().fitContent();

    // ── MACD chart ────────────────────────────────────────────────────────
    const macdEl = document.getElementById('macd-chart');
    if (_macdChart) { try { _macdChart.remove(); } catch(_){} }
    _macdChart = LightweightCharts.createChart(macdEl, { ...chartOpts(macdEl), height: 110 });

    const histSeries = _macdChart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
    const macdLine   = _macdChart.addLineSeries({ color: '#3B82F6', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false });
    const sigLine    = _macdChart.addLineSeries({ color: '#F59E0B', lineWidth: 1,   priceLineVisible: false, lastValueVisible: false });

    if (d.macd_data?.length) {
      histSeries.setData(d.macd_data.map(p => ({
        time:  p.time, value: p.hist,
        color: p.hist >= 0 ? '#10B98177' : '#EF444477',
      })));
      macdLine.setData(d.macd_data.map(p => ({ time: p.time, value: p.macd   })));
      sigLine.setData(d.macd_data.map( p => ({ time: p.time, value: p.signal })));
    }
    _macdChart.timeScale().fitContent();

    // Sync scroll/zoom between charts
    _mainChart.timeScale().subscribeVisibleLogicalRangeChange(r => {
      if (r) _macdChart.timeScale().setVisibleLogicalRange(r);
    });
    _macdChart.timeScale().subscribeVisibleLogicalRangeChange(r => {
      if (r) _mainChart.timeScale().setVisibleLogicalRange(r);
    });
  }

  // ── Signal cards renderer ──────────────────────────────────────────────────
  function _renderSignalCards(signals) {
    const wrap = document.getElementById('sig-cards');
    const grid = document.getElementById('sig-grid');
    wrap.style.display = 'block';

    grid.innerHTML = signals.map((s, i) => {
      const isLast    = i === signals.length - 1 && signals.length % 2 !== 0;
      const badges    = { bull: '▲ BULLISH', bear: '▼ BEARISH', neutral: '◆ NEUTRAL' };
      const dots      = [1,2,3].map(n =>
        `<div class="sig-dot ${n <= s.strength ? 'lit ' + s.verdict : ''}"></div>`
      ).join('');
      return `
        <div class="signal-card ${isLast ? 'full-width' : ''}">
          <div class="sig-header">
            <span class="sig-name">${s.emoji} ${s.name}</span>
            <span class="sig-badge ${s.verdict}">${badges[s.verdict] || s.verdict}</span>
          </div>
          <div class="sig-headline">${s.headline}</div>
          <div class="sig-kid">💬 "${s.kid_explain}"</div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div class="sig-value">${s.value_label}</div>
            <div class="sig-strength" title="Signal strength">${dots}</div>
          </div>
        </div>`;
    }).join('');
  }

  // ── Navigation ─────────────────────────────────────────────────────────────
  function navTo(tab) {
    _activeNav = tab;
    document.getElementById('home-view').style.display    = tab === 'home'    ? 'block' : 'none';
    document.getElementById('signals-view').style.display = tab === 'signals' ? 'block' : 'none';
    document.getElementById('output-view').style.display  = (tab !== 'home' && tab !== 'signals') ? 'block' : 'none';
    ['home','pulse','scan','monitor','signals','alerts'].forEach(t => {
      document.getElementById('nav-' + t)?.classList.toggle('active', t === tab);
    });
    document.getElementById('main-scroll').scrollTop = 0;
  }

  function goHome() { navTo('home'); }

  // ── Run a command ──────────────────────────────────────────────────────────
  async function runCmd(cmd, title, subtitle, ticker) {
    navTo(cmd === 'pulse' ? 'pulse' : cmd === 'scan' ? 'scan' : cmd === 'monitor' ? 'monitor' : 'home');

    // Force output view for non-nav commands (flow, chart)
    document.getElementById('home-view').style.display   = 'none';
    document.getElementById('output-view').style.display = 'block';

    document.getElementById('output-title').textContent    = title;
    document.getElementById('output-subtitle').textContent = subtitle || '';
    document.getElementById('output-content').innerHTML    =
      `<div class="loading-wrap"><div class="spinner"></div><div class="loading-text">Running ${title.toLowerCase()}…</div></div>`;

    // Scan uses the structured JSON endpoint for visual cards
    const isScan = cmd === 'scan';
    let url = '/api/' + (isScan ? 'scan-json' : cmd);
    if (ticker) url += '?t=' + encodeURIComponent(ticker);

    try {
      const res = await fetch(url);
      if (isScan) {
        const data = await res.json();
        renderScanCards(data);
        loadRecs();  // refresh sidebar recommendations with latest results
      } else {
        const text = await res.text();
        renderOutput(text, cmd);
      }
    } catch (e) {
      document.getElementById('output-content').innerHTML =
        `<pre class="output-pre error">Network error: ${e.message}</pre>`;
    }
  }

  // ── Output renderer ────────────────────────────────────────────────────────
  function renderOutput(text, cmd) {
    const esc = text
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    const hi = esc
      .replace(/\b(BULLISH|BUY CALL|CALL)\b/g,        '<span class="hi-green">$1</span>')
      .replace(/\b(BEARISH|BUY PUT|PUT)\b/g,           '<span class="hi-red">$1</span>')
      .replace(/\b(HOLD|NEUTRAL|MONITOR)\b/g,          '<span class="hi-yellow">$1</span>')
      .replace(/\b(RISK-ON)\b/g,                       '<span class="hi-green">$1</span>')
      .replace(/\b(RISK-OFF|FEAR)\b/g,                 '<span class="hi-red">$1</span>')
      .replace(/\b(ELEVATED|CHOPPY)\b/g,               '<span class="hi-yellow">$1</span>')
      .replace(/\b(LOW|NEUTRAL REGIME)\b/g,            '<span class="hi-cyan">$1</span>')
      .replace(/(\d+\s*\/\s*25)/g,                     '<span class="hi-blue">$1</span>')
      .replace(/([+]\d+\.?\d*%)/g,                     '<span class="hi-green">$1</span>')
      .replace(/([-]\d+\.?\d*%)/g,                     '<span class="hi-red">$1</span>')
      .replace(/(⚠[^\n]*)/g,                           '<span class="hi-yellow">$1</span>')
      .replace(/\b(TAKE PROFIT|TAKE-PROFIT)\b/g,       '<span class="hi-green">$1</span>')
      .replace(/\b(STOP OUT|STOP-OUT)\b/g,             '<span class="hi-red">$1</span>')
      .replace(/(NO TRADE[^\n]*)/g,                    '<span class="hi-yellow">$1</span>');

    const isErr = text.startsWith('ERROR');
    document.getElementById('output-content').innerHTML =
      `<div style="animation:fadeUp 0.2s ease"><pre class="output-pre${isErr ? ' error' : ''}">${hi}</pre></div>`;
  }

  // ── Scan visual cards renderer ─────────────────────────────────────────────
  function renderScanCards(data) {
    const el = document.getElementById('output-content');

    if (data.error) {
      el.innerHTML = `<pre class="output-pre error">Error: ${data.error}</pre>`;
      return;
    }

    const trades = data.trades || [];
    if (trades.length === 0) {
      el.innerHTML = `
        <div class="scan-empty">
          <div style="font-size:40px;margin-bottom:12px">🔍</div>
          <div style="font-size:15px;font-weight:700;color:var(--text);margin-bottom:8px">No confirmed setups found</div>
          <div>The scanner found no tickers where options flow<br>agrees with technical signals right now.</div>
          <div style="margin-top:12px;color:var(--blue);font-size:12px">VIX: ${data.vix || '—'}</div>
        </div>`;
      return;
    }

    // ── SVG donut gauge ──────────────────────────────────────────────────────
    function scoreGaugeSvg(score, max, r, cx, cy, strokeW, color) {
      const circ = 2 * Math.PI * r;
      const fill = (score / max) * circ;
      const sz = cx * 2;
      return `<svg width="${sz}" height="${sz}" viewBox="0 0 ${sz} ${sz}">
        <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#1E2D42" stroke-width="${strokeW}"/>
        <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${color}" stroke-width="${strokeW}"
          stroke-dasharray="${fill.toFixed(1)} ${circ.toFixed(1)}"
          stroke-linecap="round" transform="rotate(-90 ${cx} ${cy})"/>
        <text x="${cx}" y="${cy - 4}" text-anchor="middle" fill="${color}"
          font-size="${r * 0.42}" font-weight="900" font-family="system-ui">${score}</text>
        <text x="${cx}" y="${cy + r * 0.32}" text-anchor="middle" fill="#475569"
          font-size="${r * 0.26}" font-family="system-ui">/${max}</text>
      </svg>`;
    }

    function popGaugeSvg(pop, r, cx, cy, strokeW) {
      const color = pop >= 50 ? '#10B981' : pop >= 35 ? '#3B82F6' : '#F59E0B';
      const circ  = 2 * Math.PI * r;
      const fill  = (pop / 100) * circ;
      const sz = cx * 2;
      return `<svg width="${sz}" height="${sz}" viewBox="0 0 ${sz} ${sz}">
        <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#1E2D42" stroke-width="${strokeW}"/>
        <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${color}" stroke-width="${strokeW}"
          stroke-dasharray="${fill.toFixed(1)} ${circ.toFixed(1)}"
          stroke-linecap="round" transform="rotate(-90 ${cx} ${cy})"/>
        <text x="${cx}" y="${cy + r * 0.34}" text-anchor="middle" fill="${color}"
          font-size="${r * 0.44}" font-weight="900" font-family="system-ui">${pop.toFixed(0)}%</text>
      </svg>`;
    }

    const factorColors = {
      'Flow Conviction':    '#06B6D4',
      'Technical Alignment':'#10B981',
      'Risk/Reward':        '#3B82F6',
      'IV Environment':     '#8B5CF6',
      'Catalyst':           '#F59E0B',
    };

    const cards = trades.map((t, i) => {
      const isBull  = t.direction === 'BULLISH';
      const noTrade = !!t.no_trade_reason;
      const cls     = noTrade ? 'notrade' : (isBull ? 'bull' : 'bear');
      const scoreColor = t.score >= 20 ? '#10B981' : t.score >= 15 ? '#3B82F6' : '#F59E0B';

      const gauge = scoreGaugeSvg(t.score, 25, 30, 38, 38, 7, scoreColor);
      const pop   = popGaugeSvg(t.pop_estimate, 18, 22, 22, 5);

      // Factor breakdown bars
      const factorRows = Object.entries(t.score_breakdown).map(([name, pts]) => {
        const pct  = (pts / 5) * 100;
        const col  = factorColors[name] || '#3B82F6';
        const abbr = { 'Technical Alignment': 'Tech Align',
                       'Flow Conviction':      'Flow Conv.',
                       'IV Environment':       'IV Environ.' }[name] || name;
        return `<div class="scan-factor">
          <div class="scan-factor-name">${abbr}</div>
          <div class="scan-factor-bar"><div class="scan-factor-fill" style="width:${pct}%;background:${col}"></div></div>
          <div class="scan-factor-val">${pts}/5</div>
        </div>`;
      }).join('');

      // Why & risk bullets
      const whyHtml = (t.why || []).filter(w => w).map(w =>
        `<div class="scan-why-item"><span class="scan-why-bullet">▸</span><span>${w}</span></div>`
      ).join('');
      const riskHtml = (t.risk_flags || []).map(r =>
        `<div class="scan-risk-item"><span>⚠</span><span>${r}</span></div>`
      ).join('');

      // Tech chips
      const techCol = t.tech.direction === 'BULLISH' ? 'var(--green)'
                    : t.tech.direction === 'BEARISH'  ? 'var(--red)' : 'var(--yellow)';
      const rsiCol  = t.tech.rsi < 35 ? 'var(--green)' : t.tech.rsi > 65 ? 'var(--red)' : 'var(--muted)';
      const evColor = t.expected_value >= 0 ? 'var(--green)' : 'var(--red)';
      const evSign  = t.expected_value >= 0 ? '+' : '';

      return `
      <div class="scan-card ${cls}" style="animation-delay:${i * 0.07}s">
        <div class="scan-card-header">
          <div>
            <div class="scan-ticker">${t.ticker}</div>
            <div class="scan-trade-line">$${t.tech.price.toFixed(2)} · IV ${t.iv}% · Δ ${t.delta} · Vol/OI ${t.vol_oi_ratio}x</div>
          </div>
          <div class="scan-dir-chip ${isBull ? 'bull' : 'bear'}">${isBull ? '▲ BULLISH' : '▼ BEARISH'}</div>
          <div class="scan-score-chip">${t.score}/25</div>
        </div>

        <div class="scan-card-body">
          <div class="scan-gauges">
            ${gauge}
            <div class="scan-gauge-label">Score</div>
            ${pop}
            <div class="scan-gauge-label">PoP est.</div>
          </div>
          <div>
            <div class="scan-metrics">
              <div>
                <div class="scan-metric-label">Trade</div>
                <div class="scan-metric-val" style="font-size:11px">BUY ${t.contracts}x&nbsp;$${t.strike.toFixed(0)}&nbsp;${t.contract_type.toUpperCase()}</div>
              </div>
              <div>
                <div class="scan-metric-label">Expiry / DTE</div>
                <div class="scan-metric-val" style="font-size:11px">${t.expiry} <span style="color:var(--faint)">${t.dte}d</span></div>
              </div>
              <div>
                <div class="scan-metric-label">Ask · Size</div>
                <div class="scan-metric-val" style="color:var(--blue)">$${t.ask.toFixed(2)} <span style="color:var(--faint);font-size:10px">· $${t.position_size_usd.toFixed(0)}</span></div>
              </div>
              <div>
                <div class="scan-metric-label">Max Loss</div>
                <div class="scan-metric-val" style="color:var(--red)">$${t.max_loss.toFixed(0)}</div>
              </div>
              <div>
                <div class="scan-metric-label">Target Exit</div>
                <div class="scan-metric-val" style="color:var(--green);font-size:11px">$${t.target_low.toFixed(2)}–$${t.target_high.toFixed(2)}</div>
              </div>
              <div>
                <div class="scan-metric-label">Exp. Value</div>
                <div class="scan-metric-val" style="color:${evColor}">${evSign}$${t.expected_value.toFixed(0)}</div>
              </div>
            </div>
            <div class="scan-tech-row">
              <div class="scan-tech-chip">Trend&nbsp;<span class="scan-tech-chip-val" style="color:${techCol}">${t.tech.trend}</span></div>
              <div class="scan-tech-chip">RSI&nbsp;<span class="scan-tech-chip-val" style="color:${rsiCol}">${t.tech.rsi}</span></div>
              <div class="scan-tech-chip">Vol&nbsp;<span class="scan-tech-chip-val">${t.tech.volume_ratio}x</span></div>
              <div class="scan-tech-chip">BB&nbsp;<span class="scan-tech-chip-val">${t.tech.bb_width_pct}%</span></div>
              ${t.tech.squeeze ? '<div class="scan-tech-chip" style="color:var(--yellow)">⚡ Squeeze</div>' : ''}
            </div>
          </div>
        </div>

        <div class="scan-breakdown">
          <div class="scan-breakdown-title">Score Breakdown — 5 Factors</div>
          ${factorRows}
        </div>

        ${noTrade ? `<div class="scan-notrade-banner">⚠ NO TRADE — ${t.no_trade_reason}</div>` : ''}

        ${(whyHtml || riskHtml) ? `
        <div class="scan-footer">
          ${whyHtml ? `<div style="margin-bottom:${riskHtml ? '6px' : '0'}">${whyHtml}</div>` : ''}
          ${riskHtml}
        </div>` : ''}
      </div>`;
    }).join('');

    const tradeable = trades.filter(t => !t.no_trade_reason).length;
    el.innerHTML = `<div style="animation:fadeUp 0.2s ease">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap">
        <div style="font-size:13px;font-weight:700">${trades.length} setup${trades.length !== 1 ? 's' : ''} found</div>
        <div style="font-size:11px;color:var(--green);font-weight:700">${tradeable} tradeable</div>
        <div style="font-size:11px;color:var(--faint);margin-left:auto">VIX ${data.vix}</div>
      </div>
      ${cards}
    </div>`;
  }

  // ── Ticker input ───────────────────────────────────────────────────────────
  function showTickerInput(mode) {
    _tickerMode = mode;
    const sec = document.getElementById('ticker-section');
    sec.style.display = 'block';
    const inp = document.getElementById('ticker-input');
    inp.placeholder = mode === 'flow' ? 'Flow scan ticker (e.g. NVDA)' : 'Chart ticker (e.g. AAPL)';
    inp.value = '';
    inp.focus();
    sec.scrollIntoView({ behavior: 'smooth' });
  }

  function submitTicker() {
    const t = document.getElementById('ticker-input').value.trim().toUpperCase();
    if (!t) return;
    document.getElementById('ticker-section').style.display = 'none';
    if (_tickerMode === 'flow') {
      runCmd('flow', 'Flow: ' + t, 'Unusual options activity · ' + t, t);
    } else {
      runCmd('chart', 'Chart: ' + t, 'Technical analysis · ' + t, t);
    }
  }
  document.getElementById('ticker-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') submitTicker();
  });

  // ── Load recommendations ───────────────────────────────────────────────────
  async function loadRecs() {
    try {
      const res  = await fetch('/api/recs');
      const recs = await res.json();
      renderRecs(recs);
    } catch (_) { /* silent */ }
  }

  function renderRecs(recs) {
    const el = document.getElementById('recs-panel');
    if (!recs || recs.length === 0) {
      el.innerHTML = `<div class="empty-recs">
        Run a <strong style="color:var(--text)">Full Scan</strong> to populate trade recommendations.<br>
        Results are cached and shown here between sessions.
      </div>`;
      return;
    }
    el.innerHTML = recs.map(r => {
      const isBull = r.direction === 'BULLISH';
      const pct    = Math.round((r.score / 25) * 100);
      const barCol = r.score >= 20 ? 'linear-gradient(90deg,#10B981,#06B6D4)'
                   : r.score >= 15 ? 'linear-gradient(90deg,#3B82F6,#8B5CF6)'
                   : 'linear-gradient(90deg,#F59E0B,#EF4444)';
      return `
        <div class="rec-card">
          <div class="rec-header">
            <span class="rec-ticker">${r.ticker}</span>
            <span class="rec-dir ${isBull ? 'bullish' : 'bearish'}">${isBull ? '▲ CALL' : '▼ PUT'}</span>
          </div>
          <div class="rec-score-row">
            <span class="rec-score-label">Score</span>
            <div class="rec-score-bar"><div class="rec-score-fill" style="width:${pct}%;background:${barCol}"></div></div>
            <span class="rec-score-val" style="color:${r.score>=20?'#10B981':r.score>=15?'#3B82F6':'#F59E0B'}">${r.score}/25</span>
          </div>
          ${r.trade ? `<div class="rec-trade">${r.trade}</div>` : ''}
          <div class="rec-stats">
            <div class="rec-stat">
              <div class="rec-stat-label">Ask</div>
              <div class="rec-stat-val">$${r.ask}</div>
            </div>
            <div class="rec-stat" style="border-left:1px solid var(--border);padding-left:14px;margin-left:14px;">
              <div class="rec-stat-label">Max Loss</div>
              <div class="rec-stat-val" style="color:var(--red)">$${r.max_loss}</div>
            </div>
            <div class="rec-stat" style="border-left:1px solid var(--border);padding-left:14px;margin-left:14px;">
              <div class="rec-stat-label">PoP</div>
              <div class="rec-stat-val" style="color:var(--green)">${r.pop !== '—' ? r.pop + '%' : '—'}</div>
            </div>
          </div>
        </div>`;
    }).join('');
  }

  // ── Real-time alerts (SSE) ─────────────────────────────────────────────────
  function startSSE() {
    const es = new EventSource('/events');
    es.onmessage = e => {
      try {
        const alert = JSON.parse(e.data);
        if (alert.id <= _lastAlertId) return;
        _lastAlertId = alert.id;
        _alertCount++;
        updateAlertBadge();
        addAlertToDrawer(alert);
        showToast(alert);
      } catch (_) {}
    };
    es.onerror = () => {
      // Reconnect silently — browser handles this for SSE
    };
  }

  function updateAlertBadge() {
    const badge = document.getElementById('alert-badge');
    if (_alertCount > 0) {
      badge.textContent = _alertCount > 9 ? '9+' : _alertCount;
      badge.classList.add('visible');
    }
  }

  function addAlertToDrawer(alert) {
    const noMsg = document.getElementById('no-alerts-msg');
    if (noMsg) noMsg.style.display = 'none';

    const icons = { info: 'ℹ️', warn: '⚠️', danger: '🚨' };
    const item  = document.createElement('div');
    item.className = 'alert-item';
    item.innerHTML = `
      <div class="alert-dot ${alert.level}"></div>
      <div class="alert-body">
        <div class="alert-msg">${alert.message}</div>
        <div class="alert-meta">${alert.ts}${alert.ticker ? ' · ' + alert.ticker : ''}</div>
      </div>`;
    document.getElementById('alerts-list').prepend(item);
  }

  function showToast(alert) {
    const icons  = { info: 'ℹ️', warn: '⚠️', danger: '🚨' };
    const toast  = document.createElement('div');
    toast.className = `toast ${alert.level}`;
    toast.innerHTML = `
      <span class="toast-icon">${icons[alert.level] || 'ℹ️'}</span>
      <span class="toast-msg">${alert.message}</span>
      <button class="toast-close" onclick="this.parentElement.remove()">×</button>`;
    document.getElementById('toast-container').prepend(toast);
    setTimeout(() => toast.remove(), 8000);
  }

  // ── Positions: load + render ───────────────────────────────────────────────
  async function loadPositions() {
    try {
      const res       = await fetch('/api/positions');
      const positions = await res.json();
      renderPositions(positions);
    } catch (_) { /* silent */ }
  }

  function renderPositions(positions) {
    const el = document.getElementById('positions-panel');
    if (!positions || positions.length === 0) {
      el.innerHTML = `<div class="empty-state">
        No open positions.<br>
        <span style="color:var(--faint)">Tap <strong style="color:var(--blue)">+ Log Trade</strong> to record a trade from Fidelity.</span>
      </div>`;
      return;
    }
    el.innerHTML = positions.map((p, i) => {
      const ctype = (p.contract_type || 'call').toLowerCase();
      // Total cost = fill price × contracts × 100 (1 contract = 100 shares)
      const cost  = ((p.entry_price || 0) * (p.contracts || 1) * 100).toFixed(0);
      const pct   = ((parseFloat(cost) / 5000) * 100).toFixed(1);
      return `
        <div class="pos-card" style="animation:fadeUp 0.2s ease">
          <div class="pos-header">
            <div style="display:flex;align-items:center;gap:10px">
              <span class="pos-ticker">${p.ticker}</span>
              <span class="pos-type ${ctype}">${ctype.toUpperCase()}</span>
            </div>
            <button class="pos-delete" onclick="deletePosition(${i})" title="Remove position">×</button>
          </div>
          <div class="pos-meta">
            <div class="pos-stat">
              <div class="pos-stat-label">Strike</div>
              <div class="pos-stat-val">$${p.strike}</div>
            </div>
            <div class="pos-stat">
              <div class="pos-stat-label">Expiry</div>
              <div class="pos-stat-val">${p.expiry}</div>
            </div>
            <div class="pos-stat">
              <div class="pos-stat-label">Qty</div>
              <div class="pos-stat-val">${p.contracts}</div>
            </div>
            <div class="pos-stat">
              <div class="pos-stat-label">Entry</div>
              <div class="pos-stat-val">$${p.entry_price}</div>
            </div>
          </div>
          <div class="pos-cost">
            Total cost: <strong style="color:var(--text)">$${cost}</strong>
            &nbsp;·&nbsp; ${pct}% of capital
            ${p.notes ? `&nbsp;·&nbsp; ${p.notes}` : ''}
          </div>
        </div>`;
    }).join('');
  }

  async function deletePosition(index) {
    if (!confirm('Remove this position from tracking?')) return;
    await fetch('/api/positions?i=' + index, { method: 'DELETE' });
    await loadPositions();
  }

  // ── Trade log modal ────────────────────────────────────────────────────────
  function openTradeModal() {
    document.getElementById('modal-overlay').style.display = 'block';
    document.getElementById('trade-modal').classList.add('open');
    // Default expiry to ~2 weeks out
    const d = new Date();
    d.setDate(d.getDate() + 14);
    document.getElementById('f-expiry').value =
      d.toISOString().slice(0, 10);
    setTimeout(() => document.getElementById('f-ticker').focus(), 300);
  }

  function closeModal() {
    document.getElementById('modal-overlay').style.display = 'none';
    document.getElementById('trade-modal').classList.remove('open');
  }

  // Live cost calculator — updates as user types fill price or contracts
  ['f-entry', 'f-contracts'].forEach(id => {
    document.getElementById(id).addEventListener('input', updateCostSummary);
  });

  function updateCostSummary() {
    const entry     = parseFloat(document.getElementById('f-entry').value)     || 0;
    const contracts = parseInt(document.getElementById('f-contracts').value)   || 0;
    const summary   = document.getElementById('cost-summary');
    if (!entry || !contracts) { summary.classList.remove('visible'); return; }

    const CAPITAL = 5000;
    const total   = entry * contracts * 100;   // 1 contract = 100 shares
    const pct     = (total / CAPITAL * 100).toFixed(1);
    const isOver  = total > CAPITAL * 0.20;    // guardrail: max 20%

    document.getElementById('cost-total').textContent  = '$' + total.toFixed(2);
    document.getElementById('cost-maxloss').textContent = '$' + total.toFixed(2);
    const pctEl = document.getElementById('cost-pct');
    pctEl.textContent = pct + '%';
    pctEl.className   = 'cost-val' + (isOver ? ' cost-over' : pct > 15 ? ' cost-warn' : '');
    summary.classList.add('visible');
  }

  async function submitTrade(e) {
    e.preventDefault();
    const btn    = document.getElementById('submit-btn');
    btn.textContent = 'Saving…';
    btn.disabled    = true;

    const notes = document.getElementById('f-notes').value.trim();
    const trade = {
      ticker:        document.getElementById('f-ticker').value.toUpperCase().trim(),
      contract_type: document.getElementById('f-type').value,
      strike:        parseFloat(document.getElementById('f-strike').value),
      expiry:        document.getElementById('f-expiry').value,
      contracts:     parseInt(document.getElementById('f-contracts').value),
      entry_price:   parseFloat(document.getElementById('f-entry').value),
      logged_at:     new Date().toISOString(),
      ...(notes && { notes }),
    };

    try {
      const res = await fetch('/api/positions', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(trade),
      });
      if (!res.ok) throw new Error(await res.text());
      closeModal();
      document.getElementById('trade-form').reset();
      document.getElementById('cost-summary').classList.remove('visible');
      await loadPositions();
    } catch (err) {
      alert('Failed to save trade: ' + err.message);
    } finally {
      btn.textContent = 'Log Trade';
      btn.disabled    = false;
    }
  }

  // ── Alerts drawer toggle ───────────────────────────────────────────────────
  function toggleAlerts() {
    const drawer  = document.getElementById('alerts-drawer');
    const overlay = document.getElementById('alerts-overlay');
    const isOpen  = drawer.classList.contains('open');
    drawer.classList.toggle('open', !isOpen);
    overlay.classList.toggle('open', !isOpen);
    if (!isOpen) {
      // Clear badge when drawer is opened
      _alertCount = 0;
      document.getElementById('alert-badge').classList.remove('visible');
    }
  }
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# HTTP handler
# ─────────────────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        path   = parsed.path

        # ── SPA shell ──
        if path == "/":
            self._send(200, "text/html; charset=utf-8", _HTML.encode())

        # ── API: agent commands ──
        elif path == "/api/pulse":
            self._send_text(_run(["pulse"]))

        elif path == "/api/scan":
            output = _run(["scan"])
            # Parse and cache recommendations from scan output
            recs = _parse_recs(output)
            if recs:
                _save_recs(recs)
            self._send_text(output)

        elif path == "/api/monitor":
            self._send_text(_run(["monitor"]))

        elif path == "/api/flow":
            ticker = qs.get("t", ["SPY"])[0].upper()[:6]
            self._send_text(_run(["flow", ticker]))

        elif path == "/api/chart":
            ticker = qs.get("t", ["SPY"])[0].upper()[:6]
            self._send_text(_run(["chart", ticker]))

        # ── API: market news (Polygon /v2/reference/news) ──
        elif path == "/api/news":
            raw_tickers = qs.get("t", [])
            ticker_list = [t.upper()[:6] for t in raw_tickers if t] or None
            data = _fetch_news(ticker_list)
            self._send(200, "application/json", json.dumps(data).encode())

        # ── API: scan-json (structured scan results for visual cards) ──
        elif path == "/api/scan-json":
            data = _fetch_scan_json()
            self._send(200, "application/json", json.dumps(data).encode())

        # ── API: signal analysis (signals engine + chart data) ──
        elif path == "/api/signals":
            ticker = qs.get("t", ["SPY"])[0].upper()[:6]
            data   = _fetch_signals(ticker)
            self._send(200, "application/json", json.dumps(data).encode())

        # ── API: market bar snapshot (SPY/QQQ/IWM/VIX quick chips) ──
        elif path == "/api/market-bar":
            self._send(200, "application/json",
                       json.dumps(_fetch_market_bar()).encode())

        # ── API: positions (read) ──
        elif path == "/api/positions":
            positions = _load_positions()
            self._send(200, "application/json", json.dumps(positions).encode())

        # ── API: cached recommendations ──
        elif path == "/api/recs":
            recs = _load_recs()
            self._send(200, "application/json", json.dumps(recs).encode())

        # ── SSE: real-time alert stream ──
        elif path == "/events":
            self._sse_stream()

        else:
            self.send_error(404)

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str) -> None:
        body = text.encode("utf-8")
        self._send(200, "text/plain; charset=utf-8", body)

    def _sse_stream(self) -> None:
        """Long-lived SSE connection; pushes new alerts as they arrive."""
        self.send_response(200)
        self.send_header("Content-Type",  "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection",    "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        last_seen = 0
        try:
            while True:
                with _alerts_lock:
                    new = [a for a in _alerts if a["id"] > last_seen]
                if new:
                    for alert in reversed(new):
                        payload = f"data: {json.dumps(alert)}\n\n"
                        self.wfile.write(payload.encode())
                        last_seen = max(last_seen, alert["id"])
                    self.wfile.flush()
                time.sleep(1.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # Client disconnected — clean exit

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/positions":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                trade = json.loads(body)
            except json.JSONDecodeError as exc:
                self._send(400, "text/plain; charset=utf-8", str(exc).encode())
                return

            # Validate required fields
            required = ["ticker", "contract_type", "strike", "expiry", "contracts", "entry_price"]
            missing  = [f for f in required if f not in trade]
            if missing:
                self._send(400, "text/plain; charset=utf-8",
                           f"Missing fields: {', '.join(missing)}".encode())
                return

            positions = _load_positions()
            positions.append(trade)
            _save_positions(positions)
            self._send(200, "application/json", b'{"ok":true}')
        else:
            self.send_error(404)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        if parsed.path == "/api/positions":
            try:
                idx       = int(qs.get("i", ["-1"])[0])
                positions = _load_positions()
                if 0 <= idx < len(positions):
                    positions.pop(idx)
                    _save_positions(positions)
                    self._send(200, "application/json", b'{"ok":true}')
                else:
                    self._send(400, "text/plain; charset=utf-8", b"Invalid index")
            except (ValueError, IndexError) as exc:
                self._send(400, "text/plain; charset=utf-8", str(exc).encode())
        else:
            self.send_error(404)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802
        pass  # Suppress per-request logs


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start alert watchdog in background
    t = threading.Thread(target=_watchdog, daemon=True)
    t.start()

    # Ensure positions.json exists so the UI never 500s on first boot
    positions_file = BASE_DIR / "positions.json"
    if not positions_file.exists():
        positions_file.write_text("[]", encoding="utf-8")

    # Bind to 0.0.0.0 so Railway (and any reverse proxy) can reach us
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    print(f"Options Agent dashboard -> http://0.0.0.0:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
