"""
Step 5 — Rich terminal output formatters.

All display functions write to a shared Rich Console.
Nothing in this module makes API calls.
"""

from rich.console import Console, OverflowMethod
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from src.market_pulse import MarketPulse
from src.scorer import ScoredTrade
from src.scanner import FlowSignal, TradeSetup
from src.technicals import TechSummary

# legacy_windows=False forces Rich to use ANSI sequences instead of the
# Win32 console API, which avoids cp1252 encoding errors on Windows terminals.
console = Console(legacy_windows=False)

# Score bar — ASCII-safe for Windows console compatibility
_BAR_FULL = "#"
_BAR_EMPTY = "-"


def _score_bar(score: int, max_score: int = 25) -> str:
    """Render a text progress bar for a score."""
    filled = round(score / max_score * 20)
    return _BAR_FULL * filled + _BAR_EMPTY * (20 - filled)


def _pct_color(pct: float) -> str:
    """Color tag for a percentage change."""
    if pct >= 0:
        return "green"
    return "red"


def print_header(title: str) -> None:
    """Print a bold section header."""
    console.rule(f"[bold cyan]{title}[/bold cyan]")


def display_market_pulse(pulse: MarketPulse) -> None:
    """Print the Step 1 market pulse panel."""
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    vix_color = {
        "LOW": "green", "NEUTRAL": "yellow",
        "ELEVATED": "orange1", "FEAR": "red", "UNKNOWN": "white",
    }.get(pulse.vix_label, "white")

    regime_color = {
        "RISK-ON": "green", "RISK-OFF": "red", "CHOPPY": "yellow", "UNKNOWN": "white",
    }.get(pulse.regime, "white")

    lines: list[str] = [
        f"[bold]MARKET PULSE — {ts}[/bold]",
        "─" * 44,
        f"SPY:  ${pulse.spy:>7.2f}  [{_pct_color(pulse.spy_chg)}]{pulse.spy_chg:+.2f}%[/]",
        f"QQQ:  ${pulse.qqq:>7.2f}  [{_pct_color(pulse.qqq_chg)}]{pulse.qqq_chg:+.2f}%[/]",
        f"IWM:  ${pulse.iwm:>7.2f}  [{_pct_color(pulse.iwm_chg)}]{pulse.iwm_chg:+.2f}%[/]",
        f"VIX:  {pulse.vix:>7.1f}  [{vix_color}]{pulse.vix_label}[/]",
        f"Regime: [{regime_color}]{pulse.regime}[/]",
        "",
    ]

    if pulse.events:
        lines.append("[bold yellow][!] Key Events This Week:[/bold yellow]")
        for e in pulse.events:
            lines.append(f"  - {e}")
        lines.append("")

    if pulse.earnings:
        lines.append("[bold red][EARN] Earnings Landmines (avoid or play carefully):[/bold red]")
        for e in pulse.earnings:
            lines.append(f"  - {e}")
    else:
        lines.append("[dim][EARN] No major earnings in the next 7 days[/dim]")

    console.print(Panel("\n".join(lines), border_style="cyan", padding=(0, 1)))


def display_trade_card(scored: ScoredTrade, trade_num: int = 1) -> None:
    """Print a full trade card for a ScoredTrade."""
    flow = scored.setup.flow
    tech = scored.setup.tech

    if scored.no_trade_reason:
        console.print(
            Panel(
                f"[bold red]NO TRADE — {scored.no_trade_reason}[/bold red]\n"
                f"Ticker: {flow.ticker}  |  Score: {scored.score}/25",
                border_style="red",
            )
        )
        return

    direction_color = "green" if flow.direction == "BULLISH" else "red"
    bar = _score_bar(scored.score)

    lines: list[str] = [
        f"[bold]TRADE #{trade_num} — {flow.ticker}  [{direction_color}]{flow.direction}[/][/bold]",
        "━" * 44,
        f"Signal Score:    [bold]{scored.score} / 25[/bold]  {bar}",
        f"Stock Price:     ${flow.stock_price:.2f}",
        f"Trade:           BUY {scored.position_size_contracts}x "
        f"{flow.ticker} ${flow.strike:.0f} {flow.contract_type.upper()} exp {flow.expiry}",
        f"Ask Price:       ${flow.ask:.2f} / contract  "
        f"(${flow.ask * 100 * scored.position_size_contracts:,.0f} total)",
        f"Max Loss:        [red]${flow.ask * 100 * scored.position_size_contracts:,.0f}[/red]  "
        f"(if expires worthless)",
        f"Target Exit:     ${scored.setup.target_low:.2f}–${scored.setup.target_high:.2f}  "
        f"(+75% to +100%)",
        f"Stop Loss:       [red]${scored.setup.stop_loss:.2f}[/red] / contract  (–50%)",
        "",
        "[bold]Score Breakdown:[/bold]",
    ]
    for factor, pts in scored.score_breakdown.items():
        bar_mini = _BAR_FULL * pts + _BAR_EMPTY * (5 - pts)
        lines.append(f"  {factor:<22} {pts}/5  {bar_mini}")

    lines += [
        "",
        "[bold]Why This Trade:[/bold]",
    ]
    for w in scored.why:
        lines.append(f"  • {w}")

    lines += [
        "",
        "[bold]Technical:[/bold]",
        f"  {tech.summary}",
        f"  Squeeze: {'YES ⚡' if tech.squeeze else 'No'}  |  "
        f"BB Width: {tech.bb_width_pct*100:.1f}%",
    ]

    if scored.risk_flags:
        lines += ["", "[bold yellow]Risk Flags:[/bold yellow]"]
        for rf in scored.risk_flags:
            lines.append(f"  [!] {rf}")

    lines += [
        "",
        f"Probability of Profit (est.): [bold]{scored.pop_estimate:.0f}%[/bold]",
        f"Expected Value (rough):       "
        f"[{'green' if scored.expected_value >= 0 else 'red'}]"
        f"${scored.expected_value:+,.0f}[/]",
    ]

    border = "green" if flow.direction == "BULLISH" else "red"
    console.print(Panel("\n".join(lines), border_style=border, padding=(0, 1)))


def display_flow_signals(ticker: str, signals: list[FlowSignal]) -> None:
    """Print unusual flow signals for a ticker as a table."""
    if not signals:
        console.print(f"[yellow]No unusual flow signals found for {ticker}[/yellow]")
        return

    table = Table(title=f"Options Flow — {ticker}", box=box.SIMPLE_HEAVY)
    table.add_column("Direction", style="bold")
    table.add_column("Strike")
    table.add_column("Expiry")
    table.add_column("DTE", justify="right")
    table.add_column("Ask")
    table.add_column("Vol/OI", justify="right")
    table.add_column("Est. Premium", justify="right")
    table.add_column("IV", justify="right")
    table.add_column("Delta", justify="right")

    for s in signals:
        dir_color = "green" if s.direction == "BULLISH" else "red"
        table.add_row(
            f"[{dir_color}]{s.direction}[/]",
            f"${s.strike:.0f} {s.contract_type.upper()}",
            s.expiry,
            str(s.dte),
            f"${s.ask:.2f}",
            f"{s.vol_oi_ratio:.1f}x",
            f"${s.estimated_premium:,.0f}",
            f"{s.iv*100:.0f}%",
            f"{s.delta:.2f}",
        )
    console.print(table)


def display_tech_summary(tech: TechSummary) -> None:
    """Print a technical analysis summary panel."""
    trend_color = {"BULLISH": "green", "BEARISH": "red", "NEUTRAL": "yellow"}
    dir_color = trend_color.get(tech.direction, "white")

    lines = [
        f"[bold]Technical Analysis — {tech.ticker}[/bold]",
        "─" * 40,
        f"Price:       ${tech.price:.2f}",
        f"SMA20:       ${tech.sma20:.2f}  |  SMA50: ${tech.sma50:.2f}",
        f"RSI(14):     {tech.rsi:.1f}  [{trend_color.get(tech.momentum,'white')}]{tech.momentum}[/]",
        f"Volume:      {tech.volume_ratio:.1f}x avg  [{trend_color.get(tech.volume_signal,'white') if tech.volume_signal=='HIGH' else 'white'}]{tech.volume_signal}[/]",
        f"BB Width:    {tech.bb_width_pct*100:.1f}%  {'** SQUEEZE **' if tech.squeeze else ''}",
        f"Trend:       [{trend_color.get(tech.trend,'white')}]{tech.trend}[/]",
        f"Momentum:    [{trend_color.get(tech.momentum,'white')}]{tech.momentum}[/]",
        f"[bold]Overall:     [{dir_color}]{tech.direction}[/][/bold]",
    ]
    console.print(Panel("\n".join(lines), border_style=dir_color or "white", padding=(0, 1)))


def display_size_result(result: dict) -> None:
    """Print a position sizing result."""
    verdict = result["verdict"]
    color = "green" if result["size_usd"] > 0 else "red"
    console.print(
        Panel(
            f"[bold]{result['ticker']}[/bold]  Score: {result['score']}/25\n"
            f"[{color}]{verdict}[/]\n"
            f"Size: ${result['size_usd']:,.0f}  ({result['size_pct']}% of capital)",
            border_style=color,
        )
    )
