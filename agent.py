#!/usr/bin/env python3
"""
Options Research & Signal Agent — CLI entry point.

Usage:
    python agent.py scan                  # Full pipeline
    python agent.py pulse                 # Market pulse only
    python agent.py flow NVDA             # Flow scan for one ticker
    python agent.py chart AAPL            # Technical summary
    python agent.py size TSLA 18          # Position size for a score
    python agent.py monitor               # Check open positions
    python agent.py explain MSFT          # Plain English breakdown
    python agent.py exit AAPL             # Exit recommendation
"""

import argparse
import io
import os
import sys
from pathlib import Path

# Force UTF-8 output on Windows so Rich box-drawing characters render correctly.
# Must happen before any Rich imports.
if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv

# Load .env.local first, fall back to .env
load_dotenv(Path(".env.local"))
load_dotenv(Path(".env"))

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")


def _require_key() -> str:
    """Exit with a helpful message if the API key is missing."""
    if not POLYGON_API_KEY:
        print("ERROR: POLYGON_API_KEY not set. Add it to .env.local")
        sys.exit(1)
    return POLYGON_API_KEY


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_scan(_args: argparse.Namespace) -> None:
    """Full pipeline: pulse → flow scan → technicals → scored trade cards."""
    from src.market_pulse import get_market_pulse
    from src.scanner import run_full_scan
    from src.scorer import score_trade
    from src.output import print_header, display_market_pulse, display_trade_card

    key = _require_key()
    print_header("MARKET PULSE")
    pulse = get_market_pulse(key)
    display_market_pulse(pulse)

    print_header("SCANNING FOR SETUPS")
    setups = run_full_scan(key)

    if not setups:
        from src.output import console
        console.print("[yellow]No confirmed setups found this session.[/yellow]")
        return

    print_header(f"TRADE SHORTLIST  ({len(setups)} setup{'s' if len(setups) != 1 else ''})")
    for i, setup in enumerate(setups, 1):
        scored = score_trade(setup, vix=pulse.vix)
        display_trade_card(scored, trade_num=i)


def cmd_pulse(_args: argparse.Namespace) -> None:
    """Market pulse only."""
    from src.market_pulse import get_market_pulse
    from src.output import print_header, display_market_pulse

    key = _require_key()
    print_header("MARKET PULSE")
    display_market_pulse(get_market_pulse(key))


def cmd_flow(args: argparse.Namespace) -> None:
    """Unusual options flow for a specific ticker."""
    from src.scanner import get_ticker_flow
    from src.output import display_flow_signals, console

    if not args.ticker:
        console.print("[red]Usage: python agent.py flow TICKER[/red]")
        return
    ticker = args.ticker.upper()
    key = _require_key()
    try:
        signals = get_ticker_flow(key, ticker)
        display_flow_signals(ticker, signals)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")


def cmd_chart(args: argparse.Namespace) -> None:
    """Technical summary for a specific ticker."""
    from src.scanner import get_ticker_tech
    from src.output import display_tech_summary, console

    if not args.ticker:
        console.print("[red]Usage: python agent.py chart TICKER[/red]")
        return
    ticker = args.ticker.upper()
    key = _require_key()
    try:
        tech = get_ticker_tech(key, ticker)
        display_tech_summary(tech)
    except (RuntimeError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")


def cmd_size(args: argparse.Namespace) -> None:
    """Position size for a given score."""
    from src.scorer import calculate_size
    from src.output import display_size_result, console

    if not args.ticker or not args.score:
        console.print("[red]Usage: python agent.py size TICKER SCORE[/red]")
        return
    try:
        score = int(args.score)
    except ValueError:
        console.print("[red]SCORE must be an integer 1–25[/red]")
        return
    result = calculate_size(args.ticker.upper(), score)
    display_size_result(result)


def cmd_monitor(_args: argparse.Namespace) -> None:
    """Check all open positions from positions.json."""
    from src.monitor import check_positions, display_positions

    key = _require_key()
    statuses = check_positions(key)
    display_positions(statuses)


def cmd_explain(args: argparse.Namespace) -> None:
    """Plain English breakdown of whether a ticker is a trade or not."""
    from src.scanner import get_ticker_flow, get_ticker_tech, TradeSetup, _build_trade_setup
    from src.scorer import score_trade
    from src.output import display_trade_card, display_tech_summary, display_flow_signals, console

    if not args.ticker:
        console.print("[red]Usage: python agent.py explain TICKER[/red]")
        return
    ticker = args.ticker.upper()
    key = _require_key()

    console.rule(f"[bold cyan]ANALYSIS — {ticker}[/bold cyan]")

    # Technicals
    try:
        tech = get_ticker_tech(key, ticker)
        display_tech_summary(tech)
    except (RuntimeError, ValueError) as exc:
        console.print(f"[red]Could not fetch technicals: {exc}[/red]")
        return

    # Flow
    try:
        signals = get_ticker_flow(key, ticker)
        display_flow_signals(ticker, signals)
    except RuntimeError as exc:
        console.print(f"[yellow]Flow data unavailable: {exc}[/yellow]")
        return

    if not signals:
        console.print(
            f"[yellow]NO TRADE — No unusual flow signals found for {ticker}. "
            "Insufficient conviction to recommend a position.[/yellow]"
        )
        return

    # Check if flow aligns with technicals
    best = signals[0]
    if tech.direction == "NEUTRAL":
        console.print(
            f"[yellow]NO TRADE — Technical direction is NEUTRAL for {ticker}. "
            "Need a clear trend before entering.[/yellow]"
        )
        return
    if best.direction != tech.direction:
        console.print(
            f"[yellow]NO TRADE — Flow is {best.direction} but technicals are {tech.direction}. "
            "Conflicting signals — standing down.[/yellow]"
        )
        return

    setup = _build_trade_setup(best, tech)
    scored = score_trade(setup)
    display_trade_card(scored, trade_num=1)


def cmd_exit(args: argparse.Namespace) -> None:
    """Exit recommendation for a specific open position."""
    from src.monitor import check_single_exit
    from src.output import console

    if not args.ticker:
        console.print("[red]Usage: python agent.py exit TICKER[/red]")
        return
    check_single_exit(_require_key(), args.ticker.upper())


# ── CLI setup ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Options Research & Signal Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser("scan", help="Full pipeline scan")
    sub.add_parser("pulse", help="Market pulse only")

    p_flow = sub.add_parser("flow", help="Options flow for a ticker")
    p_flow.add_argument("ticker", nargs="?")

    p_chart = sub.add_parser("chart", help="Technical summary for a ticker")
    p_chart.add_argument("ticker", nargs="?")

    p_size = sub.add_parser("size", help="Position size calculator")
    p_size.add_argument("ticker", nargs="?")
    p_size.add_argument("score", nargs="?")

    sub.add_parser("monitor", help="Check open positions")

    p_explain = sub.add_parser("explain", help="Plain English trade analysis")
    p_explain.add_argument("ticker", nargs="?")

    p_exit = sub.add_parser("exit", help="Should I exit this position?")
    p_exit.add_argument("ticker", nargs="?")

    args = parser.parse_args()

    dispatch = {
        "scan": cmd_scan,
        "pulse": cmd_pulse,
        "flow": cmd_flow,
        "chart": cmd_chart,
        "size": cmd_size,
        "monitor": cmd_monitor,
        "explain": cmd_explain,
        "exit": cmd_exit,
    }

    if args.command in dispatch:
        dispatch[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
