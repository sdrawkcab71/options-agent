#!/usr/bin/env python3
"""
Options Agent V2 — CLI entry point.
Focused on SPX credit spreads and iron condors (premium selling / theta strategy).

Commands:
  scan              Full pipeline: classify market → find best credit spread
  pulse             Market pulse + GREEN/YELLOW/RED verdict
  condor [WEEKLY|MONTHLY]  Build iron condor on best index
  strikes TICKER    Probability-based strike selection
  theta             Theta decay analysis for open positions
  monitor           Check open spread positions
  size SCORE CREDIT Position sizing calculator
  goal TARGET DAYS  Goal wizard (e.g. goal 500 30)
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(".env.local"))
load_dotenv(Path(".env"))

from src.output import console, print_header


def _require_key() -> str:
    key = os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY", "")
    if not key:
        console.print(
            "[red]Error: MASSIVE_API_KEY not set.[/red]\n"
            "Add it to .env.local:\n  MASSIVE_API_KEY=your_key_here"
        )
        sys.exit(1)
    return key


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_scan(args) -> None:
    from src.spread_builder import find_best_credit_spread
    from src.scorer import score_trade
    from src.output import display_daily_spread_result, display_scored_trade

    key = _require_key()
    account = getattr(args, "account", 5000.0)

    print_header("FULL SCAN — SPX Credit Spreads")
    console.print("[dim]Running: Market Pulse → Classifier → Expected Move → Strike Select → Spread Build...[/dim]\n")

    result = find_best_credit_spread(key, account_size=account)
    display_daily_spread_result(result)

    if result.put_spread and result.put_spread.passes_filters:
        scored = score_trade(result.put_spread, result.classifier)
        display_scored_trade(scored)


def cmd_pulse(args) -> None:
    from src.market_pulse import get_market_pulse
    from src.market_classifier import classify_market
    from src.output import display_market_classifier

    key = _require_key()
    console.print("[dim]Fetching market pulse...[/dim]")
    pulse = get_market_pulse(key)
    verdict = classify_market(pulse)

    print_header("MARKET PULSE")
    console.print(
        f"SPX: [bold]${pulse.spx:,.2f}[/bold] ({pulse.spx_chg:+.2f}%)  "
        f"SPY: ${pulse.spy:.2f}  QQQ: ${pulse.qqq:.2f}  IWM: ${pulse.iwm:.2f}\n"
        f"VIX: [bold]{pulse.vix:.2f}[/bold] [{pulse.vix_label}]  "
        f"IV 30d: {pulse.iv_30d*100:.1f}%  RV 20d: {pulse.rv_20d*100:.1f}%  "
        f"Edge: {pulse.iv_rv_edge*100:+.1f} vol pts\n"
        f"Overnight Gap: {pulse.overnight_gap_pct:+.2f}% [{pulse.gap_risk_label}]  "
        f"Regime: [bold]{pulse.regime}[/bold]"
    )
    if pulse.events_today:
        console.print(f"Events Today: [yellow]{', '.join(pulse.events_today)}[/yellow]")

    display_market_classifier(verdict)


def cmd_condor(args) -> None:
    from src.market_pulse import get_market_pulse
    from src.market_classifier import classify_market
    from src.condor_builder import find_best_condor
    from src.scorer import score_trade
    from src.output import display_iron_condor, display_scored_trade, display_market_classifier

    key = _require_key()
    account = getattr(args, "account", 5000.0)
    dte_pref = getattr(args, "dte", "WEEKLY").upper()

    console.print("[dim]Fetching market data and building iron condor...[/dim]")
    pulse = get_market_pulse(key)
    verdict = classify_market(pulse)

    print_header(f"IRON CONDOR — {dte_pref}")
    display_market_classifier(verdict)

    if verdict.signal == "RED":
        console.print("[red bold]RED signal — no condor recommended today.[/red bold]")
        return

    condor = find_best_condor(key, account_size=account, dte_preference=dte_pref, pulse=pulse)
    display_iron_condor(condor)
    scored = score_trade(condor, verdict)
    display_scored_trade(scored)


def cmd_strikes(args) -> None:
    from src.market_pulse import get_market_pulse
    from src.strike_selector import select_strikes
    from src.spread_builder import _find_next_expiry
    from src.output import display_strike_recommendation
    from datetime import date

    key = _require_key()
    ticker = args.ticker.upper()
    win_rate = getattr(args, "win_rate", 0.84)

    console.print(f"[dim]Fetching data and computing strikes for {ticker}...[/dim]")
    pulse = get_market_pulse(key)
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
    display_strike_recommendation(rec)


def cmd_theta(args) -> None:
    from src.market_pulse import get_market_pulse
    from src.theta_calculator import build_theta_report
    from src.output import display_theta_report

    key = _require_key()
    account = getattr(args, "account", 5000.0)
    pulse = get_market_pulse(key)
    spx = pulse.spx or 5000.0
    iv = pulse.iv_30d or 0.18

    report = build_theta_report(underlying_price=spx, iv=iv, account_size=account)
    display_theta_report(report)


def cmd_monitor(args) -> None:
    from src.monitor import check_spread_positions
    from src.output import display_spread_positions

    key = _require_key()
    statuses = check_spread_positions(key)
    display_spread_positions(statuses)


def cmd_size(args) -> None:
    from src.config import CAPITAL, MAX_RISK_PCT

    account = getattr(args, "account", CAPITAL)
    score = int(args.score)
    credit = float(args.credit)
    spread_width = getattr(args, "width", 5.0)

    max_risk = account * MAX_RISK_PCT
    max_loss_per_contract = (spread_width - credit) * 100
    contracts = max(1, int(max_risk / max_loss_per_contract)) if max_loss_per_contract > 0 else 0

    if score < 15:
        verdict = "[red]NO TRADE — score below 15[/red]"
        contracts = 0
    elif score < 20:
        max_risk = account * 0.03
        contracts = max(1, int(max_risk / max_loss_per_contract)) if max_loss_per_contract > 0 else 0
        verdict = "[yellow]SMALL TRADE (score 15–19)[/yellow]"
    else:
        verdict = "[green]STANDARD TRADE (score 20–25)[/green]"

    print_header("POSITION SIZE")
    console.print(
        f"Score: {score}/25  Credit: ${credit:.2f}  Width: ${spread_width:.0f}  Account: ${account:,.0f}\n"
        f"Max Loss/Contract: ${max_loss_per_contract:.0f}  "
        f"Max Risk Budget: ${max_risk:.0f}\n"
        f"Contracts: [bold]{contracts}[/bold]  Total Credit: ${credit * contracts * 100:.0f}\n"
        f"Verdict: {verdict}"
    )


def cmd_goal(args) -> None:
    from src.goal_engine import calculate_goal_profile
    from src.output import display_goal_profile

    target = float(args.target)
    days = int(args.days)
    account = getattr(args, "account", 5000.0)

    profile = calculate_goal_profile(target, days, account)
    display_goal_profile(profile)


# ── CLI wiring ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Options Agent V2 — SPX Credit Spreads & Iron Condors",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--account", type=float, default=5000.0, help="Account size (default 5000)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("scan", help="Full pipeline: classify → credit spread")
    sub.add_parser("pulse", help="Market pulse + GREEN/YELLOW/RED verdict")

    p_condor = sub.add_parser("condor", help="Build iron condor on best index")
    p_condor.add_argument("dte", nargs="?", default="WEEKLY", choices=["WEEKLY", "MONTHLY"])

    p_strikes = sub.add_parser("strikes", help="Probability-based strike selection")
    p_strikes.add_argument("ticker", help="Ticker symbol (e.g. SPX)")
    p_strikes.add_argument("--win-rate", type=float, default=0.84, dest="win_rate")

    sub.add_parser("theta", help="Theta decay for open positions")
    sub.add_parser("monitor", help="Check open spread positions")

    p_size = sub.add_parser("size", help="Position sizing calculator")
    p_size.add_argument("score", help="Trade score (0–25)")
    p_size.add_argument("credit", help="Net credit received per share")
    p_size.add_argument("--width", type=float, default=5.0)

    p_goal = sub.add_parser("goal", help="Goal wizard (e.g. goal 500 30)")
    p_goal.add_argument("target", help="Target profit in dollars")
    p_goal.add_argument("days", help="Days to achieve goal")

    args = parser.parse_args()

    dispatch = {
        "scan": cmd_scan,
        "pulse": cmd_pulse,
        "condor": cmd_condor,
        "strikes": cmd_strikes,
        "theta": cmd_theta,
        "monitor": cmd_monitor,
        "size": cmd_size,
        "goal": cmd_goal,
    }

    if not args.command:
        parser.print_help()
        return

    handler = dispatch.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
