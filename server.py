#!/usr/bin/env python3
"""
Options Agent V2 — Flask web server.
Gamified homepage + real-time SPX credit spread / iron condor dashboard.
"""
import json
import os
import queue
import threading
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template_string, request

load_dotenv(Path(".env.local"))
load_dotenv(Path(".env"))

PORT = int(os.environ.get("PORT", 7823))
API_KEY = os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY", "")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# ── Alert queue (SSE watchdog) ────────────────────────────────────────────────
_alert_queue: queue.Queue = queue.Queue(maxsize=50)
_market_cache: dict = {}
_market_cache_ts: float = 0.0
_CACHE_TTL = 60.0


def _safe_asdict(obj) -> dict:
    try:
        return asdict(obj)
    except Exception:
        return {}


# ── Market data cache ─────────────────────────────────────────────────────────

def _get_market_bar() -> dict:
    global _market_cache, _market_cache_ts
    now = time.time()
    if now - _market_cache_ts < _CACHE_TTL and _market_cache:
        return _market_cache

    if not API_KEY:
        return {}

    try:
        from src.market_pulse import get_market_pulse
        pulse = get_market_pulse(API_KEY)
        _market_cache = {
            "spx": pulse.spx,
            "spx_chg": pulse.spx_chg,
            "spy": pulse.spy,
            "spy_chg": pulse.spy_chg,
            "qqq": pulse.qqq,
            "qqq_chg": pulse.qqq_chg,
            "iwm": pulse.iwm,
            "iwm_chg": pulse.iwm_chg,
            "vix": pulse.vix,
            "vix_label": pulse.vix_label,
            "iv_30d": pulse.iv_30d,
            "rv_20d": pulse.rv_20d,
            "iv_rv_edge": pulse.iv_rv_edge,
            "has_seller_edge": pulse.has_seller_edge,
            "overnight_gap_pct": pulse.overnight_gap_pct,
            "gap_risk_label": pulse.gap_risk_label,
            "regime": pulse.regime,
            "events_today": pulse.events_today,
            "events_this_week": pulse.events_this_week,
            "event_density": pulse.event_density,
        }
        _market_cache_ts = now
    except Exception as e:
        _market_cache = {"error": str(e)}

    return _market_cache


# ── Watchdog thread ────────────────────────────────────────────────────────────

def _watchdog():
    time.sleep(20)
    while True:
        try:
            bar = _get_market_bar()
            vix = bar.get("vix", 0)
            spx_chg = abs(bar.get("spx_chg", 0))

            if vix >= 30:
                _alert_queue.put_nowait({"type": "FEAR", "msg": f"VIX {vix:.1f} — CRISIS regime. No new trades.", "ts": datetime.now().isoformat()})
            elif vix >= 25:
                _alert_queue.put_nowait({"type": "WARN", "msg": f"VIX {vix:.1f} — Elevated. Use wider strikes.", "ts": datetime.now().isoformat()})
            if spx_chg >= 1.5:
                _alert_queue.put_nowait({"type": "MOVE", "msg": f"SPX moved {bar.get('spx_chg',0):+.2f}% — monitor spreads.", "ts": datetime.now().isoformat()})
        except Exception:
            pass
        time.sleep(60)


threading.Thread(target=_watchdog, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/market-bar")
def api_market_bar():
    return jsonify(_get_market_bar())


@app.route("/api/pulse")
def api_pulse():
    if not API_KEY:
        return jsonify({"error": "MASSIVE_API_KEY not set"}), 500
    try:
        from src.market_pulse import get_market_pulse
        from src.market_classifier import classify_market
        pulse = get_market_pulse(API_KEY)
        verdict = classify_market(pulse)
        return jsonify({
            "pulse": _safe_asdict(pulse),
            "verdict": _safe_asdict(verdict),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/market")
def api_market():
    if not API_KEY:
        return jsonify({"error": "MASSIVE_API_KEY not set"}), 500
    try:
        from src.market_pulse import get_market_pulse
        from src.market_classifier import classify_market
        pulse = get_market_pulse(API_KEY)
        verdict = classify_market(pulse)
        return jsonify(_safe_asdict(verdict))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scan", methods=["GET", "POST"])
def api_scan():
    if not API_KEY:
        return jsonify({"error": "MASSIVE_API_KEY not set"}), 500
    try:
        body = request.get_json(silent=True) or {}
        account = float(body.get("account", 5000.0))
        from src.spread_builder import find_best_credit_spread
        from src.scorer import score_trade

        result = find_best_credit_spread(API_KEY, account_size=account)
        data = {
            "timestamp": result.timestamp,
            "classifier": _safe_asdict(result.classifier),
            "recommended_action": result.recommended_action,
            "skip_reason": result.skip_reason,
            "expected_move": _safe_asdict(result.expected_move) if result.expected_move else None,
        }

        if result.put_spread:
            ps = _safe_asdict(result.put_spread)
            if result.put_spread.passes_filters:
                scored = score_trade(result.put_spread, result.classifier)
                ps["score"] = scored.score
                ps["verdict"] = scored.verdict
                ps["score_breakdown"] = scored.score_breakdown
                ps["risk_flags"] = scored.risk_flags
            data["put_spread"] = ps

        if result.call_spread:
            cs = _safe_asdict(result.call_spread)
            if result.call_spread.passes_filters:
                scored = score_trade(result.call_spread, result.classifier)
                cs["score"] = scored.score
                cs["verdict"] = scored.verdict
            data["call_spread"] = cs

        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/condor", methods=["GET", "POST"])
def api_condor():
    if not API_KEY:
        return jsonify({"error": "MASSIVE_API_KEY not set"}), 500
    try:
        body = request.get_json(silent=True) or {}
        account = float(body.get("account", 5000.0))
        dte_pref = body.get("dte", "WEEKLY").upper()

        from src.market_pulse import get_market_pulse
        from src.market_classifier import classify_market
        from src.condor_builder import find_best_condor
        from src.scorer import score_trade

        pulse = get_market_pulse(API_KEY)
        verdict = classify_market(pulse)

        if verdict.signal == "RED":
            return jsonify({"signal": "RED", "skip_reason": verdict.reasons[0] if verdict.reasons else "RED conditions"})

        condor = find_best_condor(API_KEY, account_size=account, dte_preference=dte_pref, pulse=pulse)
        scored = score_trade(condor, verdict)
        result = _safe_asdict(condor)
        result["score"] = scored.score
        result["verdict"] = scored.verdict
        result["score_breakdown"] = scored.score_breakdown
        result["risk_flags"] = scored.risk_flags
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/strikes", methods=["GET", "POST"])
def api_strikes():
    if not API_KEY:
        return jsonify({"error": "MASSIVE_API_KEY not set"}), 500
    try:
        body = request.get_json(silent=True) or {}
        ticker = (body.get("ticker") or request.args.get("ticker", "SPX")).upper()
        win_rate = float(body.get("win_rate", request.args.get("win_rate", 0.84)))

        from src.market_pulse import get_market_pulse
        from src.strike_selector import select_strikes
        from src.spread_builder import _find_next_expiry

        pulse = get_market_pulse(API_KEY)
        iv = pulse.iv_30d or 0.18
        price = pulse.spx if ticker in ("SPX", "I:SPX") else pulse.spy * 10
        expiry = _find_next_expiry(14)
        dte = (date.fromisoformat(expiry) - date.today()).days

        rec = select_strikes(
            underlying=ticker,
            current_price=price,
            expiry=expiry,
            dte=dte,
            iv=iv,
            target_win_rate=win_rate,
            events_today=pulse.events_today,
        )
        return jsonify(_safe_asdict(rec))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/theta")
def api_theta():
    if not API_KEY:
        return jsonify({"error": "MASSIVE_API_KEY not set"}), 500
    try:
        account = float(request.args.get("account", 5000.0))
        from src.market_pulse import get_market_pulse
        from src.theta_calculator import build_theta_report

        pulse = get_market_pulse(API_KEY)
        report = build_theta_report(
            underlying_price=pulse.spx or 5000.0,
            iv=pulse.iv_30d or 0.18,
            account_size=account,
        )
        return jsonify(_safe_asdict(report))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/goal", methods=["POST"])
def api_goal():
    try:
        body = request.get_json(silent=True) or {}
        target = float(body.get("target", 500))
        days = int(body.get("days", 30))
        account = float(body.get("account", 5000))

        from src.goal_engine import calculate_goal_profile
        profile = calculate_goal_profile(target, days, account)
        return jsonify(_safe_asdict(profile))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/positions", methods=["GET"])
def api_positions_get():
    try:
        from src.monitor import load_positions
        return jsonify(load_positions())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/positions", methods=["POST"])
def api_positions_post():
    try:
        body = request.get_json(silent=True) or {}
        from src.monitor import save_position
        save_position(body)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/monitor")
def api_monitor():
    if not API_KEY:
        return jsonify({"error": "MASSIVE_API_KEY not set"}), 500
    try:
        from src.monitor import check_spread_positions
        statuses = check_spread_positions(API_KEY)
        return jsonify([_safe_asdict(s) for s in statuses])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/news")
def api_news():
    if not API_KEY:
        return jsonify([])
    try:
        ticker = request.args.get("ticker")
        from src.massive_client import MassiveClient
        client = MassiveClient(API_KEY)
        return jsonify(client.news(ticker=ticker))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/alerts")
def api_alerts():
    alerts = []
    try:
        while not _alert_queue.empty():
            alerts.append(_alert_queue.get_nowait())
    except Exception:
        pass
    return jsonify(alerts)


@app.route("/events")
def events():
    def stream():
        while True:
            try:
                alert = _alert_queue.get(timeout=30)
                yield f"data: {json.dumps(alert)}\n\n"
            except queue.Empty:
                yield "data: {\"type\":\"ping\"}\n\n"
    return Response(stream(), mimetype="text/event-stream")


# ── Homepage (gamified goal wizard SPA) ───────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Options Agent V2</title>
<style>
  :root{--bg:#0a0e1a;--card:#111827;--border:#1e2a3a;--accent:#3b82f6;--green:#22c55e;--red:#ef4444;--yellow:#eab308;--text:#e2e8f0;--dim:#64748b}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;min-height:100vh}
  .topbar{display:flex;align-items:center;gap:16px;padding:10px 24px;background:var(--card);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;flex-wrap:wrap}
  .topbar-ticker{font-size:13px;color:var(--dim)}
  .topbar-ticker span{color:var(--text);font-weight:600}
  .topbar-ticker .up{color:var(--green)}
  .topbar-ticker .dn{color:var(--red)}
  .signal-badge{padding:4px 10px;border-radius:6px;font-weight:700;font-size:12px;letter-spacing:.05em}
  .signal-GREEN{background:#14532d;color:var(--green)}
  .signal-YELLOW{background:#713f12;color:var(--yellow)}
  .signal-RED{background:#450a0a;color:var(--red)}
  .nav{display:flex;gap:4px;padding:0 24px;background:var(--card);border-bottom:1px solid var(--border)}
  .nav button{padding:10px 16px;background:none;border:none;color:var(--dim);cursor:pointer;font-size:14px;border-bottom:2px solid transparent}
  .nav button.active{color:var(--text);border-bottom-color:var(--accent)}
  .page{display:none;padding:24px;max-width:1100px;margin:0 auto}
  .page.active{display:block}
  h2{font-size:20px;margin-bottom:16px;color:var(--text)}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:16px}
  .card h3{font-size:15px;font-weight:600;margin-bottom:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
  @media(max-width:700px){.grid2,.grid3{grid-template-columns:1fr}}
  .metric{display:flex;flex-direction:column;gap:4px}
  .metric .val{font-size:24px;font-weight:700}
  .metric .lbl{font-size:12px;color:var(--dim)}
  .up{color:var(--green)} .dn{color:var(--red)} .neutral{color:var(--yellow)}
  .btn{padding:10px 20px;border-radius:8px;border:none;cursor:pointer;font-size:14px;font-weight:600}
  .btn-primary{background:var(--accent);color:#fff}
  .btn-primary:hover{background:#2563eb}
  .btn-sm{padding:6px 14px;font-size:13px}
  input[type=range]{width:100%;accent-color:var(--accent)}
  input[type=number]{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:15px;width:100%}
  .risk-dial{display:flex;align-items:center;gap:12px;margin:16px 0}
  .dial-bar{flex:1;height:12px;border-radius:6px;background:linear-gradient(to right,var(--green),var(--yellow),var(--red));position:relative}
  .dial-needle{position:absolute;top:-4px;width:4px;height:20px;background:white;border-radius:2px;transform:translateX(-50%);transition:left .3s}
  .strategy-badge{display:inline-block;padding:6px 14px;border-radius:20px;font-size:13px;font-weight:600;margin-top:8px}
  .badge-CONSERVATIVE{background:#14532d;color:var(--green)}
  .badge-MODERATE{background:#1e3a5f;color:#60a5fa}
  .badge-AGGRESSIVE{background:#713f12;color:var(--yellow)}
  .badge-VERY_AGGRESSIVE{background:#450a0a;color:var(--red)}
  .milestone{display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border)}
  .milestone-bar{flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden}
  .milestone-fill{height:100%;background:var(--accent);border-radius:3px;transition:width .4s}
  .spread-card{border-left:4px solid;padding:16px;border-radius:0 8px 8px 0;background:var(--bg);margin-bottom:12px}
  .spread-card.put{border-color:var(--red)}
  .spread-card.call{border-color:var(--accent)}
  .spread-card.condor{border-color:#a855f7}
  .spread-row{display:flex;justify-content:space-between;align-items:center;padding:4px 0}
  .spread-label{font-size:12px;color:var(--dim)}
  .spread-val{font-size:14px;font-weight:600}
  .fidelity-box{background:#0c1929;border:1px solid #1e3a5f;border-radius:8px;padding:12px;font-family:monospace;font-size:12px;color:#93c5fd;margin-top:8px}
  .theta-bar{display:flex;gap:4px;height:80px;align-items:flex-end;margin:12px 0}
  .theta-bar-col{flex:1;background:var(--accent);border-radius:4px 4px 0 0;min-height:4px;transition:height .4s}
  .theta-bar-label{font-size:10px;color:var(--dim);text-align:center;margin-top:4px}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th{text-align:left;padding:8px;color:var(--dim);font-weight:500;border-bottom:1px solid var(--border)}
  td{padding:8px;border-bottom:1px solid var(--border)}
  .action-HOLD{color:var(--green)}
  .action-TAKE.PROFIT{color:#22d3ee}
  .action-STOP{color:var(--red)}
  .action-ADJUST{color:var(--yellow)}
  .loading{opacity:.5;font-size:13px;margin:8px 0}
  .error-msg{color:var(--red);font-size:13px;padding:8px;background:#450a0a;border-radius:6px;margin:8px 0}
</style>
</head>
<body>

<!-- Top bar: live market ticker -->
<div class="topbar" id="topbar">
  <div style="font-weight:700;color:var(--accent);font-size:15px">⚡ OPTIONS AGENT V2</div>
  <div class="topbar-ticker" id="ticker-spx">SPX <span>—</span></div>
  <div class="topbar-ticker" id="ticker-spy">SPY <span>—</span></div>
  <div class="topbar-ticker" id="ticker-qqq">QQQ <span>—</span></div>
  <div class="topbar-ticker" id="ticker-vix">VIX <span>—</span></div>
  <div id="signal-badge" class="signal-badge signal-UNKNOWN">LOADING</div>
  <div style="flex:1"></div>
  <div style="font-size:12px;color:var(--dim)" id="last-update"></div>
</div>

<!-- Nav tabs -->
<div class="nav">
  <button class="active" onclick="showPage('goal')">🎯 Goal Wizard</button>
  <button onclick="showPage('market')">📊 Market</button>
  <button onclick="showPage('trade')">📈 Trade Finder</button>
  <button onclick="showPage('condor')">🦅 Iron Condor</button>
  <button onclick="showPage('theta')">⏱ Theta Lab</button>
  <button onclick="showPage('monitor')">🔍 Monitor</button>
</div>

<!-- ── GOAL WIZARD ─────────────────────────────────── -->
<div class="page active" id="page-goal">
  <h2>🎯 Goal Wizard</h2>
  <p style="color:var(--dim);margin-bottom:20px">Tell me your goals. I'll design the exact options strategy to get you there.</p>
  <div class="grid2">
    <div class="card">
      <h3>Set Your Target</h3>
      <div style="margin-bottom:16px">
        <label style="font-size:13px;color:var(--dim)">I want to make</label>
        <div style="display:flex;align-items:center;gap:10px;margin-top:6px">
          <span style="font-size:20px;color:var(--green)">$</span>
          <input type="number" id="goal-target" value="500" min="50" max="50000" style="font-size:22px;font-weight:700;max-width:180px">
        </div>
      </div>
      <div style="margin-bottom:16px">
        <label style="font-size:13px;color:var(--dim)">Account size</label>
        <div style="display:flex;align-items:center;gap:10px;margin-top:6px">
          <span style="font-size:20px;color:var(--dim)">$</span>
          <input type="number" id="goal-account" value="5000" min="1000" max="1000000" style="font-size:18px;max-width:180px">
        </div>
      </div>
      <div style="margin-bottom:16px">
        <label style="font-size:13px;color:var(--dim)">In <span id="horizon-display" style="color:var(--text);font-weight:700">30</span> days</label>
        <input type="range" id="goal-days" min="5" max="90" value="30" step="5" oninput="document.getElementById('horizon-display').textContent=this.value;calcGoalPreview()">
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--dim)">
          <span>5d</span><span>10d</span><span>15d</span><span>30d</span><span>60d</span><span>90d</span>
        </div>
      </div>
      <button class="btn btn-primary" onclick="runGoal()">Calculate My Strategy →</button>
    </div>
    <div class="card" id="goal-preview">
      <h3>Live Preview</h3>
      <div id="goal-preview-content" style="color:var(--dim);font-size:14px">Adjust sliders to see preview...</div>
    </div>
  </div>
  <div id="goal-result" style="display:none">
    <div class="card" id="goal-result-card"></div>
    <div class="card" id="goal-milestones">
      <h3>Weekly Milestones</h3>
      <div id="milestones-list"></div>
    </div>
  </div>
</div>

<!-- ── MARKET DASHBOARD ────────────────────────────── -->
<div class="page" id="page-market">
  <h2>📊 Market Conditions</h2>
  <button class="btn btn-primary btn-sm" onclick="loadMarket()" style="margin-bottom:16px">🔄 Refresh</button>
  <div id="market-content"><div class="loading">Loading market data...</div></div>
</div>

<!-- ── TRADE FINDER ────────────────────────────────── -->
<div class="page" id="page-trade">
  <h2>📈 Today's Best Credit Spread</h2>
  <button class="btn btn-primary btn-sm" onclick="loadScan()" style="margin-bottom:16px">🔄 Run Full Scan</button>
  <div id="scan-content"><div class="loading">Click "Run Full Scan" to analyze today's market...</div></div>
</div>

<!-- ── IRON CONDOR ─────────────────────────────────── -->
<div class="page" id="page-condor">
  <h2>🦅 Iron Condor Builder</h2>
  <div style="display:flex;gap:12px;align-items:center;margin-bottom:16px">
    <select id="condor-dte" style="background:var(--card);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px">
      <option value="WEEKLY">Weekly (7 DTE)</option>
      <option value="MONTHLY">Monthly (30 DTE)</option>
    </select>
    <button class="btn btn-primary btn-sm" onclick="loadCondor()">Build Condor →</button>
  </div>
  <div id="condor-content"><div class="loading">Click "Build Condor" to generate today's iron condor setup...</div></div>
</div>

<!-- ── THETA LAB ───────────────────────────────────── -->
<div class="page" id="page-theta">
  <h2>⏱ Theta Lab</h2>
  <button class="btn btn-primary btn-sm" onclick="loadTheta()" style="margin-bottom:16px">📊 Analyze Positions</button>
  <div id="theta-content"><div class="loading">Click to analyze theta decay for your open positions...</div></div>
</div>

<!-- ── POSITION MONITOR ────────────────────────────── -->
<div class="page" id="page-monitor">
  <h2>🔍 Position Monitor</h2>
  <button class="btn btn-primary btn-sm" onclick="loadMonitor()" style="margin-bottom:16px">🔄 Check Positions</button>
  <div id="monitor-content"><div class="loading">Click to check your open spread positions...</div></div>
</div>

<script>
// ── Navigation ──────────────────────────────────────────────────────────────
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'market') loadMarket();
  if (name === 'theta') loadTheta();
  if (name === 'monitor') loadMonitor();
}

// ── Top bar updater ──────────────────────────────────────────────────────────
function fmtChg(v) {
  const s = (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
  return `<span class="${v >= 0 ? 'up' : 'dn'}">${s}</span>`;
}

async function updateTopBar() {
  try {
    const d = await fetch('/api/market-bar').then(r => r.json());
    if (d.error) return;
    document.getElementById('ticker-spx').innerHTML = `SPX <span>$${(d.spx||0).toLocaleString('en',{minimumFractionDigits:2})} ${fmtChg(d.spx_chg||0)}</span>`;
    document.getElementById('ticker-spy').innerHTML = `SPY <span>$${(d.spy||0).toFixed(2)} ${fmtChg(d.spy_chg||0)}</span>`;
    document.getElementById('ticker-qqq').innerHTML = `QQQ <span>$${(d.qqq||0).toFixed(2)} ${fmtChg(d.qqq_chg||0)}</span>`;
    document.getElementById('ticker-vix').innerHTML = `VIX <span>${(d.vix||0).toFixed(2)}</span>`;

    // Determine signal from VIX
    let sig = 'GREEN', sigLabel = '● GREEN';
    if (d.vix >= 30) { sig = 'RED'; sigLabel = '● RED — NO TRADE'; }
    else if (d.vix >= 20 || !d.has_seller_edge) { sig = 'YELLOW'; sigLabel = '● YELLOW — CAUTION'; }
    const badge = document.getElementById('signal-badge');
    badge.textContent = sigLabel;
    badge.className = 'signal-badge signal-' + sig;
    document.getElementById('last-update').textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch(e) {}
}
updateTopBar();
setInterval(updateTopBar, 60000);

// ── Goal Wizard ──────────────────────────────────────────────────────────────
function calcGoalPreview() {
  const target = parseFloat(document.getElementById('goal-target').value) || 500;
  const account = parseFloat(document.getElementById('goal-account').value) || 5000;
  const days = parseInt(document.getElementById('goal-days').value) || 30;
  const retPct = (target / account * 100).toFixed(1);
  const weeklyRet = (target / account / (days / 5) * 100).toFixed(2);
  let profile, color;
  if (weeklyRet <= 0.5) { profile = 'CONSERVATIVE'; color = '#22c55e'; }
  else if (weeklyRet <= 1.5) { profile = 'MODERATE'; color = '#60a5fa'; }
  else if (weeklyRet <= 3.0) { profile = 'AGGRESSIVE'; color = '#eab308'; }
  else { profile = 'VERY AGGRESSIVE'; color = '#ef4444'; }

  const riskPct = Math.min(100, (weeklyRet / 3) * 100);
  document.getElementById('goal-preview-content').innerHTML = `
    <div class="metric" style="margin-bottom:12px">
      <div class="val" style="color:${color}">${profile}</div>
      <div class="lbl">Risk Profile</div>
    </div>
    <div class="risk-dial">
      <span style="font-size:11px;color:var(--green)">LOW</span>
      <div class="dial-bar"><div class="dial-needle" style="left:${riskPct}%"></div></div>
      <span style="font-size:11px;color:var(--red)">HIGH</span>
    </div>
    <div style="font-size:13px;color:var(--dim)">
      Required return: <strong style="color:var(--text)">${retPct}%</strong> total /
      <strong style="color:var(--text)">${weeklyRet}%</strong>/week<br>
      Daily target: <strong style="color:var(--green)">$${(target / (days * 5 / 7)).toFixed(2)}</strong>
    </div>`;
}

document.getElementById('goal-target').addEventListener('input', calcGoalPreview);
document.getElementById('goal-account').addEventListener('input', calcGoalPreview);
calcGoalPreview();

async function runGoal() {
  const target = parseFloat(document.getElementById('goal-target').value) || 500;
  const account = parseFloat(document.getElementById('goal-account').value) || 5000;
  const days = parseInt(document.getElementById('goal-days').value) || 30;

  const res = await fetch('/api/goal', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({target, account, days})}).then(r => r.json());
  if (res.error) { alert(res.error); return; }

  const profileColors = {CONSERVATIVE:'#22c55e', MODERATE:'#60a5fa', AGGRESSIVE:'#eab308', VERY_AGGRESSIVE:'#ef4444'};
  const c = profileColors[res.risk_profile] || '#60a5fa';
  const feasColors = {REALISTIC:'var(--green)', STRETCH:'var(--yellow)', UNLIKELY:'var(--red)'};
  const fc = feasColors[res.feasibility] || 'var(--dim)';

  document.getElementById('goal-result-card').innerHTML = `
    <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px">
      <div>
        <div style="font-size:28px;font-weight:800;color:${c}">${res.risk_profile.replace('_',' ')}</div>
        <div style="font-size:13px;color:var(--dim)">Risk Score: ${res.risk_score}/10</div>
      </div>
      <div style="flex:1">
        <div class="risk-dial">
          <span style="font-size:11px;color:var(--green)">LOW</span>
          <div class="dial-bar"><div class="dial-needle" style="left:${res.risk_score*10}%"></div></div>
          <span style="font-size:11px;color:var(--red)">HIGH</span>
        </div>
      </div>
    </div>
    <div class="grid3" style="margin-bottom:16px">
      <div class="metric"><div class="val up">$${res.daily_target_usd.toFixed(2)}</div><div class="lbl">Daily Target</div></div>
      <div class="metric"><div class="val up">$${res.weekly_target_usd.toFixed(2)}</div><div class="lbl">Weekly Target</div></div>
      <div class="metric"><div class="val" style="color:${fc}">${res.feasibility}</div><div class="lbl">Feasibility</div></div>
    </div>
    <div style="font-size:15px;font-weight:600;margin-bottom:6px">${res.recommended_strategy}</div>
    <div style="font-size:13px;color:var(--dim);margin-bottom:12px">${res.strategy_description}</div>
    <div class="grid3">
      <div><div style="font-size:12px;color:var(--dim)">Target Delta</div><div style="font-weight:600">${res.target_delta}</div></div>
      <div><div style="font-size:12px;color:var(--dim)">Win Rate</div><div style="font-weight:600">${res.win_probability_estimate.toFixed(0)}%</div></div>
      <div><div style="font-size:12px;color:var(--dim)">Max Risk/Trade</div><div style="font-weight:600;color:var(--red)">$${res.max_risk_per_trade_usd.toFixed(0)}</div></div>
    </div>
    <div style="margin-top:12px;padding:10px;background:var(--bg);border-radius:8px;font-size:13px;color:var(--dim);font-style:italic">${res.feasibility_note}</div>`;

  const milestoneHtml = (res.milestones || []).slice(0,8).map(m => {
    const fillPct = Math.min(100, m.pct_of_account / res.required_return_pct * 100);
    return `<div class="milestone">
      <div style="font-size:12px;min-width:60px;color:var(--dim)">Wk ${m.week}</div>
      <div class="milestone-bar"><div class="milestone-fill" style="width:${fillPct}%"></div></div>
      <div style="font-size:12px;min-width:80px;text-align:right">$${m.target_usd.toLocaleString()}</div>
    </div>`;
  }).join('');
  document.getElementById('milestones-list').innerHTML = milestoneHtml;
  document.getElementById('goal-result').style.display = 'block';
}

// ── Market Dashboard ─────────────────────────────────────────────────────────
async function loadMarket() {
  document.getElementById('market-content').innerHTML = '<div class="loading">Loading market data...</div>';
  try {
    const d = await fetch('/api/pulse').then(r => r.json());
    if (d.error) { document.getElementById('market-content').innerHTML = `<div class="error-msg">${d.error}</div>`; return; }
    const v = d.verdict; const p = d.pulse;
    const sigColors = {GREEN:'var(--green)', YELLOW:'var(--yellow)', RED:'var(--red)'};
    const sc = sigColors[v.signal] || 'var(--text)';

    document.getElementById('market-content').innerHTML = `
      <div class="card" style="border-left:4px solid ${sc}">
        <div style="font-size:28px;font-weight:800;color:${sc};margin-bottom:8px">◉ ${v.signal}</div>
        <div style="font-size:14px;color:var(--dim);margin-bottom:12px">${v.strategy_recommendation}</div>
        <div class="grid3">
          <div class="metric"><div class="val">VIX ${v.vix.toFixed(1)}</div><div class="lbl">${v.vix_regime}</div></div>
          <div class="metric"><div class="val ${v.has_seller_edge ? 'up' : 'dn'}">${v.has_seller_edge ? '✓ Seller Edge' : '✗ No Edge'}</div><div class="lbl">IV vs RV: ${(v.iv_edge*100).toFixed(1)} pts</div></div>
          <div class="metric"><div class="val">${v.gap_risk}</div><div class="lbl">Gap Risk (${(v.overnight_gap_pct||0).toFixed(2)}%)</div></div>
        </div>
        ${v.events_today && v.events_today.length ? `<div style="margin-top:12px;padding:8px;background:#713f12;border-radius:6px;font-size:13px;color:var(--yellow)">⚠ Events Today: ${v.events_today.join(', ')}</div>` : ''}
        <div style="margin-top:12px">${(v.reasons||[]).map(r=>`<div style="font-size:13px;color:var(--dim);padding:3px 0">• ${r}</div>`).join('')}</div>
      </div>
      <div class="grid3">
        <div class="card"><h3>IV / RV</h3>
          <div class="metric"><div class="val">${((p.iv_30d||0)*100).toFixed(1)}%</div><div class="lbl">Implied Vol (30d)</div></div>
          <div style="margin-top:8px" class="metric"><div class="val">${((p.rv_20d||0)*100).toFixed(1)}%</div><div class="lbl">Realized Vol (20d)</div></div>
        </div>
        <div class="card"><h3>Regime</h3>
          <div class="metric"><div class="val">${p.regime||'—'}</div><div class="lbl">Market Regime</div></div>
        </div>
        <div class="card"><h3>Events This Week</h3>
          ${(p.events_this_week||[]).slice(0,5).map(e=>`<div style="font-size:13px;padding:2px 0">• ${e}</div>`).join('') || '<div style="color:var(--green);font-size:13px">Clear — no major events</div>'}
        </div>
      </div>`;
  } catch(e) {
    document.getElementById('market-content').innerHTML = `<div class="error-msg">${e.message}</div>`;
  }
}

// ── Trade Finder ─────────────────────────────────────────────────────────────
function spreadCard(s, label, cls) {
  if (!s || !s.passes_filters) return `<div class="spread-card ${cls}"><div style="color:var(--dim)">${label}: Not available</div></div>`;
  const side = cls === 'put' ? 'PUT SPREAD' : 'CALL SPREAD';
  const fidelityOrder = `SELL ${s.contracts} ${s.short_leg?.contract_type?.toUpperCase()} ${s.expiry} $${s.short_leg?.strike?.toFixed(0)} / BUY ${s.contracts} ${s.long_leg?.contract_type?.toUpperCase()} ${s.expiry} $${s.long_leg?.strike?.toFixed(0)}  ·  NET CREDIT LIMIT $${s.credit?.toFixed(2)}`;
  return `<div class="spread-card ${cls}">
    <div style="font-weight:700;margin-bottom:10px">${side} — ${s.underlying} ${s.expiry} (${s.dte} DTE)</div>
    <div class="grid2">
      <div>
        <div class="spread-row"><span class="spread-label">Short ${s.short_leg?.contract_type} Strike</span><span class="spread-val">$${(s.short_leg?.strike||0).toLocaleString()}</span></div>
        <div class="spread-row"><span class="spread-label">Long ${s.long_leg?.contract_type} Strike</span><span class="spread-val">$${(s.long_leg?.strike||0).toLocaleString()}</span></div>
        <div class="spread-row"><span class="spread-label">Net Credit</span><span class="spread-val up">$${s.credit?.toFixed(2)}</span></div>
        <div class="spread-row"><span class="spread-label">Max Loss</span><span class="spread-val dn">$${s.max_loss_usd?.toFixed(0)}/contract</span></div>
      </div>
      <div>
        <div class="spread-row"><span class="spread-label">Breakeven</span><span class="spread-val">$${(s.breakeven||0).toLocaleString()}</span></div>
        <div class="spread-row"><span class="spread-label">Win Probability</span><span class="spread-val up">${s.pop?.toFixed(0)}%</span></div>
        <div class="spread-row"><span class="spread-label">Contracts</span><span class="spread-val">${s.contracts}</span></div>
        <div class="spread-row"><span class="spread-label">Score</span><span class="spread-val">${s.score || '—'}/25</span></div>
      </div>
    </div>
    <div style="margin-top:8px;font-size:12px;color:var(--dim)">
      Profit Target: Close at $${s.profit_target?.toFixed(2)} &nbsp;|&nbsp; Stop: Exit if spread reaches $${s.stop_loss?.toFixed(2)}
    </div>
    <div class="fidelity-box">📋 Fidelity Order: ${fidelityOrder}</div>
  </div>`;
}

async function loadScan() {
  const account = parseFloat(document.getElementById('goal-account')?.value) || 5000;
  document.getElementById('scan-content').innerHTML = '<div class="loading">⏳ Running full scan pipeline...</div>';
  try {
    const d = await fetch('/api/scan', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({account})}).then(r => r.json());
    if (d.error) { document.getElementById('scan-content').innerHTML = `<div class="error-msg">${d.error}</div>`; return; }

    const sigColors = {GREEN:'var(--green)', YELLOW:'var(--yellow)', RED:'var(--red)'};
    const sc = sigColors[d.classifier?.signal] || 'var(--text)';
    const em = d.expected_move;

    let html = `<div class="card" style="border-left:4px solid ${sc};margin-bottom:16px">
      <div style="font-size:20px;font-weight:700;color:${sc}">${d.classifier?.signal} — ${d.recommended_action}</div>
      ${d.skip_reason ? `<div style="color:var(--red);margin-top:8px">${d.skip_reason}</div>` : ''}
      ${em ? `<div style="margin-top:10px;font-size:13px;color:var(--dim)">
        SPX: $${(em.underlying_price||0).toLocaleString()} &nbsp;|&nbsp;
        1σ Expected Move: ±$${em.expected_move?.toFixed(0)} (${em.one_sigma_pct?.toFixed(1)}%) &nbsp;|&nbsp;
        Range: $${em.range_low?.toFixed(0)} – $${em.range_high?.toFixed(0)}
      </div>` : ''}
    </div>`;

    html += spreadCard(d.put_spread, 'PUT CREDIT SPREAD', 'put');
    html += spreadCard(d.call_spread, 'CALL CREDIT SPREAD', 'call');
    document.getElementById('scan-content').innerHTML = html;
  } catch(e) {
    document.getElementById('scan-content').innerHTML = `<div class="error-msg">${e.message}</div>`;
  }
}

// ── Iron Condor ───────────────────────────────────────────────────────────────
async function loadCondor() {
  const dte = document.getElementById('condor-dte').value;
  const account = parseFloat(document.getElementById('goal-account')?.value) || 5000;
  document.getElementById('condor-content').innerHTML = '<div class="loading">⏳ Building iron condor...</div>';
  try {
    const d = await fetch('/api/condor', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({account, dte})}).then(r => r.json());
    if (d.error) { document.getElementById('condor-content').innerHTML = `<div class="error-msg">${d.error}</div>`; return; }
    if (d.signal === 'RED') { document.getElementById('condor-content').innerHTML = `<div class="card"><div style="color:var(--red);font-size:18px;font-weight:700">🚫 RED — ${d.skip_reason}</div></div>`; return; }

    const fidelityOrder = `
SELL ${d.contracts} PUT  ${d.expiry} $${d.put_spread?.short_leg?.strike?.toFixed(0)} / BUY PUT ${d.expiry} $${d.put_spread?.long_leg?.strike?.toFixed(0)}
SELL ${d.contracts} CALL ${d.expiry} $${d.call_spread?.short_leg?.strike?.toFixed(0)} / BUY CALL ${d.expiry} $${d.call_spread?.long_leg?.strike?.toFixed(0)}
NET CREDIT LIMIT $${(d.total_credit||0).toFixed(2)}`;

    document.getElementById('condor-content').innerHTML = `
      <div class="spread-card condor">
        <div style="font-weight:700;font-size:16px;margin-bottom:12px">🦅 IRON CONDOR — ${d.underlying} ${d.expiry} (${d.dte} DTE)</div>
        <div style="font-size:12px;color:var(--dim);margin-bottom:12px">${d.selection_rationale || ''}</div>
        <div class="grid2">
          <div>
            <div style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--red)">PUT SIDE</div>
            <div class="spread-row"><span class="spread-label">Short Put</span><span class="spread-val">$${(d.put_spread?.short_leg?.strike||0).toLocaleString()}</span></div>
            <div class="spread-row"><span class="spread-label">Long Put</span><span class="spread-val">$${(d.put_spread?.long_leg?.strike||0).toLocaleString()}</span></div>
            <div class="spread-row"><span class="spread-label">Put Credit</span><span class="spread-val up">$${(d.put_spread?.credit||0).toFixed(2)}</span></div>
            <div class="spread-row"><span class="spread-label">Put PoP</span><span class="spread-val up">${(d.put_spread?.pop||0).toFixed(0)}%</span></div>
          </div>
          <div>
            <div style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--accent)">CALL SIDE</div>
            <div class="spread-row"><span class="spread-label">Short Call</span><span class="spread-val">$${(d.call_spread?.short_leg?.strike||0).toLocaleString()}</span></div>
            <div class="spread-row"><span class="spread-label">Long Call</span><span class="spread-val">$${(d.call_spread?.long_leg?.strike||0).toLocaleString()}</span></div>
            <div class="spread-row"><span class="spread-label">Call Credit</span><span class="spread-val up">$${(d.call_spread?.credit||0).toFixed(2)}</span></div>
            <div class="spread-row"><span class="spread-label">Call PoP</span><span class="spread-val up">${(d.call_spread?.pop||0).toFixed(0)}%</span></div>
          </div>
        </div>
        <div style="margin-top:16px;padding:12px;background:var(--bg);border-radius:8px">
          <div class="grid3">
            <div class="metric"><div class="val up">$${(d.total_credit||0).toFixed(2)}</div><div class="lbl">Total Credit</div></div>
            <div class="metric"><div class="val dn">$${(d.max_loss_usd||0).toFixed(0)}</div><div class="lbl">Max Loss/Contract</div></div>
            <div class="metric"><div class="val up">${(d.win_probability||0).toFixed(0)}%</div><div class="lbl">Win Probability</div></div>
          </div>
          <div style="margin-top:12px;font-size:13px;color:var(--dim)">
            Breakeven Zone: $${(d.breakeven_low||0).toLocaleString()} – $${(d.breakeven_high||0).toLocaleString()} ($${(d.profit_zone_width||0).toFixed(0)} wide)<br>
            Profit Target: Close at $${(d.profit_target||0).toFixed(2)} &nbsp;|&nbsp; Stop: $${(d.stop_loss||0).toFixed(2)}<br>
            Adjust if: Underlying ≤ $${(d.adjustment_trigger_low||0).toFixed(0)} or ≥ $${(d.adjustment_trigger_high||0).toFixed(0)}
          </div>
        </div>
        <div class="fidelity-box" style="white-space:pre">📋 Fidelity Order:${fidelityOrder}</div>
        ${(d.notes||[]).map(n=>`<div style="color:var(--yellow);font-size:13px;margin-top:6px">⚠ ${n}</div>`).join('')}
      </div>
      <div class="card">
        <h3>Score: ${d.score||0}/25 — ${d.verdict||'—'}</h3>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          ${Object.entries(d.score_breakdown||{}).map(([k,v])=>`
            <div style="background:var(--bg);padding:8px 12px;border-radius:6px;text-align:center">
              <div style="font-weight:700">${v}/5</div>
              <div style="font-size:11px;color:var(--dim)">${k}</div>
            </div>`).join('')}
        </div>
        ${(d.risk_flags||[]).map(f=>`<div style="color:var(--yellow);font-size:13px;margin-top:6px">⚠ ${f}</div>`).join('')}
      </div>`;
  } catch(e) {
    document.getElementById('condor-content').innerHTML = `<div class="error-msg">${e.message}</div>`;
  }
}

// ── Theta Lab ────────────────────────────────────────────────────────────────
async function loadTheta() {
  const account = parseFloat(document.getElementById('goal-account')?.value) || 5000;
  document.getElementById('theta-content').innerHTML = '<div class="loading">Calculating theta decay...</div>';
  try {
    const d = await fetch(`/api/theta?account=${account}`).then(r => r.json());
    if (d.error) { document.getElementById('theta-content').innerHTML = `<div class="error-msg">${d.error}</div>`; return; }

    if (!d.positions || d.positions.length === 0) {
      document.getElementById('theta-content').innerHTML = '<div class="card" style="color:var(--dim)">No open positions in positions.json. Add positions to see theta analysis.</div>';
      return;
    }

    let html = `<div class="card">
      <h3>Portfolio Daily Theta: <span style="color:var(--green)">$${(d.total_net_theta||0).toFixed(2)}/day</span></h3>`;

    d.positions.forEach(p => {
      const gammaColor = p.gamma_critical ? 'var(--red)' : (p.in_gamma_zone ? 'var(--yellow)' : 'var(--green)');
      html += `<div style="border:1px solid var(--border);border-radius:8px;padding:12px;margin:12px 0">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div><strong>${p.strategy}</strong> — ${p.underlying} ${p.expiry} (${p.dte} DTE)</div>
          <div style="color:var(--green);font-weight:700">θ $${(p.net_theta_per_day||0).toFixed(2)}/day</div>
        </div>
        ${p.gamma_warning ? `<div style="color:${gammaColor};font-size:13px;margin-top:6px">⚠ ${p.gamma_warning}</div>` : ''}
        <div class="grid3" style="margin-top:12px">
          <div class="metric"><div class="val" style="color:var(--green)">$${(p.weekly_target_usd||0).toFixed(2)}</div><div class="lbl">Weekly Income</div></div>
          <div class="metric"><div class="val" style="color:var(--accent)">$${(p.projection_30d||0).toLocaleString()}</div><div class="lbl">30d Projection</div></div>
          <div class="metric"><div class="val" style="color:var(--accent)">$${(p.projection_90d||0).toLocaleString()}</div><div class="lbl">90d Projection</div></div>
        </div>
        ${p.hourly_curve ? renderHourlyBar(p.hourly_curve, p.net_theta_per_day) : ''}
      </div>`;
    });

    html += '</div>';
    document.getElementById('theta-content').innerHTML = html;
  } catch(e) {
    document.getElementById('theta-content').innerHTML = `<div class="error-msg">${e.message}</div>`;
  }
}

function renderHourlyBar(curve, dailyTheta) {
  const entries = Object.entries(curve);
  const maxVal = Math.max(...entries.map(([,v]) => Math.abs(v)));
  return `<div style="margin-top:12px">
    <div style="font-size:12px;color:var(--dim);margin-bottom:6px">Hourly Theta Distribution</div>
    <div class="theta-bar">
      ${entries.map(([label, val]) => {
        const h = maxVal > 0 ? Math.max(4, (Math.abs(val) / maxVal) * 80) : 4;
        return `<div style="flex:1;text-align:center">
          <div class="theta-bar-col" style="height:${h}px"></div>
          <div class="theta-bar-label">${label.split('-')[0]}</div>
        </div>`;
      }).join('')}
    </div>
  </div>`;
}

// ── Position Monitor ──────────────────────────────────────────────────────────
async function loadMonitor() {
  document.getElementById('monitor-content').innerHTML = '<div class="loading">Fetching position data...</div>';
  try {
    const d = await fetch('/api/monitor').then(r => r.json());
    if (d.error) { document.getElementById('monitor-content').innerHTML = `<div class="error-msg">${d.error}</div>`; return; }

    if (!d.length) {
      document.getElementById('monitor-content').innerHTML = '<div class="card" style="color:var(--dim)">No open positions tracked in positions.json.</div>';
      return;
    }

    const actionColors = {HOLD:'var(--green)', 'TAKE PROFIT':'#22d3ee', 'STOP OUT':'var(--red)', ADJUST:'var(--yellow)', ROLL:'#a855f7', EXPIRED:'var(--dim)'};

    let html = '<div class="card"><table><thead><tr><th>ID</th><th>Strategy</th><th>Underlying</th><th>Expiry</th><th>DTE</th><th>Credit</th><th>Current</th><th>P&L</th><th>Action</th></tr></thead><tbody>';
    d.forEach(s => {
      const ac = actionColors[s.action] || 'var(--text)';
      const pnl = s.pnl_pct != null ? `${s.pnl_pct.toFixed(0)}%` : '—';
      const pnlColor = s.pnl_pct > 0 ? 'var(--green)' : (s.pnl_pct < 0 ? 'var(--red)' : 'var(--text)');
      html += `<tr>
        <td style="font-size:12px;color:var(--dim)">${s.position_id.slice(0,12)}</td>
        <td>${s.strategy}</td>
        <td>${s.underlying}</td>
        <td>${s.expiry}</td>
        <td>${s.dte}</td>
        <td>$${s.credit_received.toFixed(2)}</td>
        <td>${s.current_value != null ? '$'+s.current_value.toFixed(2) : '—'}</td>
        <td style="color:${pnlColor}">${pnl}</td>
        <td style="color:${ac};font-weight:600">${s.action}</td>
      </tr>
      <tr><td colspan="9" style="font-size:12px;color:var(--dim);padding-bottom:8px">${s.reason}</td></tr>`;
    });
    html += '</tbody></table></div>';
    document.getElementById('monitor-content').innerHTML = html;
  } catch(e) {
    document.getElementById('monitor-content').innerHTML = `<div class="error-msg">${e.message}</div>`;
  }
}

// ── SSE live alerts ────────────────────────────────────────────────────────────
const evtSource = new EventSource('/events');
evtSource.onmessage = (e) => {
  try {
    const a = JSON.parse(e.data);
    if (a.type === 'ping') return;
    const alertColors = {FEAR:'#ef4444', WARN:'#eab308', MOVE:'#60a5fa'};
    const c = alertColors[a.type] || '#60a5fa';
    const div = document.createElement('div');
    div.style.cssText = `position:fixed;bottom:${20 + (document.querySelectorAll('.live-alert').length * 60)}px;right:20px;background:var(--card);border:1px solid ${c};border-radius:8px;padding:10px 14px;font-size:13px;color:${c};z-index:9999;max-width:320px`;
    div.className = 'live-alert';
    div.textContent = `⚡ ${a.msg}`;
    document.body.appendChild(div);
    setTimeout(() => div.remove(), 6000);
  } catch(e) {}
};
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(_HTML)


if __name__ == "__main__":
    print(f"Options Agent V2 running on http://localhost:{PORT}")
    if not API_KEY:
        print("WARNING: MASSIVE_API_KEY not set. Add to .env.local")
    app.run(host="0.0.0.0", port=PORT, debug=False)
