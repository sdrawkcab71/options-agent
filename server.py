#!/usr/bin/env python3
"""
Options Agent web dashboard — SharpEdge design language.
Single-page app with real-time alerts, ticker search, and recommendations panel.

Usage:
    python server.py   →   http://localhost:7823
"""

import html as _esc
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT            = 7823
BASE_DIR        = Path(__file__).parent
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

<!-- ── Main ─────────────────────────────────────────────────────────────────── -->
<main id="main-scroll">

  <!-- HOME VIEW -->
  <div id="home-view">

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
    <p class="section-title">Open Positions</p>
    <div id="positions-panel">POSITIONS_PLACEHOLDER</div>

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
    <div class="alerts-drawer-title">🔔 Alerts</div>
    <button class="alerts-close" onclick="toggleAlerts()">×</button>
  </div>
  <div class="alerts-list" id="alerts-list">
    <div class="no-alerts" id="no-alerts-msg">No alerts yet.<br>Monitoring VIX and SPY/QQQ for threshold events.</div>
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

  // ── Boot: load recs + positions, start SSE ────────────────────────────────
  window.addEventListener('DOMContentLoaded', () => {
    loadRecs();
    startSSE();
  });

  // ── Navigation ─────────────────────────────────────────────────────────────
  function navTo(tab) {
    _activeNav = tab;
    document.getElementById('home-view').style.display   = tab === 'home' ? 'block' : 'none';
    document.getElementById('output-view').style.display = tab !== 'home' ? 'block' : 'none';
    ['home','pulse','scan','monitor','alerts'].forEach(t => {
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

    let url = '/api/' + cmd;
    if (ticker) url += '?t=' + encodeURIComponent(ticker);

    try {
      const res  = await fetch(url);
      const text = await res.text();
      renderOutput(text, cmd);
      // After a scan, refresh the recommendations panel
      if (cmd === 'scan') loadRecs();
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
# Position HTML renderer (server-side)
# ─────────────────────────────────────────────────────────────────────────────

def _render_positions_html() -> str:
    """Render open positions as HTML cards for injection into the template."""
    positions = _load_positions()
    if not positions:
        return '<div class="empty-state">No open positions.<br>Edit <code>positions.json</code> to track trades.</div>'

    cards: list[str] = []
    for p in positions:
        ticker    = _esc.escape(str(p.get("ticker", "?")))
        ctype     = str(p.get("contract_type", "call")).lower()
        strike    = p.get("strike", "—")
        expiry    = _esc.escape(str(p.get("expiry", "—")))
        contracts = p.get("contracts", 1)
        entry     = p.get("entry_price", "—")
        cards.append(f"""
        <div class="pos-card">
          <div class="pos-header">
            <span class="pos-ticker">{ticker}</span>
            <span class="pos-type {ctype}">{ctype.upper()}</span>
          </div>
          <div class="pos-meta">
            <div class="pos-stat">
              <div class="pos-stat-label">Strike</div>
              <div class="pos-stat-val">${strike}</div>
            </div>
            <div class="pos-stat">
              <div class="pos-stat-label">Expiry</div>
              <div class="pos-stat-val">{expiry}</div>
            </div>
            <div class="pos-stat">
              <div class="pos-stat-label">Contracts</div>
              <div class="pos-stat-val">{contracts}</div>
            </div>
            <div class="pos-stat">
              <div class="pos-stat-label">Entry</div>
              <div class="pos-stat-val">${entry}</div>
            </div>
          </div>
        </div>""")
    return "\n".join(cards)


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
            page = _HTML.replace("POSITIONS_PLACEHOLDER", _render_positions_html())
            self._send(200, "text/html; charset=utf-8", page.encode())

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

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802
        pass  # Suppress per-request logs


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start alert watchdog in background
    t = threading.Thread(target=_watchdog, daemon=True)
    t.start()

    server = ThreadingHTTPServer(("localhost", PORT), _Handler)
    print(f"Options Agent dashboard -> http://localhost:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
