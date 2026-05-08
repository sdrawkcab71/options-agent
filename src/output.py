"""
Rich terminal output formatters for spreads, condors, theta, and classifier verdicts.
"""
from typing import Union, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

console = Console()

_SIGNAL_COLORS = {"GREEN": "green", "YELLOW": "yellow", "RED": "red bold"}
_ACTION_COLORS = {
    "HOLD": "green",
    "TAKE PROFIT": "cyan bold",
    "STOP OUT": "red bold",
    "ADJUST": "yellow bold",
    "ROLL": "magenta",
    "EXPIRED": "dim",
    "UNKNOWN": "dim",
}


def print_header(title: str) -> None:
    console.rule(f"[bold cyan]{title}[/bold cyan]")


def display_market_classifier(verdict) -> None:
    from src.market_classifier import MarketVerdict
    color = _SIGNAL_COLORS.get(verdict.signal, "white")
    title = f"[{color}]◉ MARKET SIGNAL: {verdict.signal}[/{color}]"

    lines = [
        f"VIX {verdict.vix:.1f}  Regime: [bold]{verdict.vix_regime}[/bold]",
        f"IV-RV Edge: {verdict.iv_edge*100:+.1f} vol pts  {'✓ Seller edge' if verdict.has_seller_edge else '✗ No seller edge'}",
        f"Gap Risk: {verdict.gap_risk}  ({verdict.overnight_gap_pct:+.2f}% overnight)",
    ]
    if verdict.events_today:
        lines.append(f"Events Today: {', '.join(verdict.events_today)}")

    lines.append("")
    lines.append(f"[italic]{verdict.strategy_recommendation}[/italic]")

    for r in verdict.reasons:
        lines.append(f"  • {r}")

    console.print(Panel("\n".join(lines), title=title, border_style=color))


def display_expected_move(em) -> None:
    t = Table(title="Expected Move", box=box.SIMPLE, show_header=True)
    t.add_column("Metric", style="cyan")
    t.add_column("Value", justify="right")
    t.add_row("Underlying Price", f"${em.underlying_price:,.2f}")
    t.add_row("IV (30d)", f"{em.iv*100:.1f}%")
    t.add_row("DTE", str(em.dte))
    t.add_row("1σ Expected Move", f"±${em.expected_move:,.2f} ({em.one_sigma_pct:.1f}%)")
    t.add_row("1σ Range", f"${em.range_low:,.2f} – ${em.range_high:,.2f}")
    t.add_row("2σ Range", f"${em.two_sigma_low:,.2f} – ${em.two_sigma_high:,.2f}")
    console.print(t)


def display_credit_spread(spread, num: int = 1) -> None:
    label = "PUT CREDIT SPREAD" if "put" in spread.spread_type else "CALL CREDIT SPREAD"
    color = "red" if "put" in spread.spread_type else "blue"

    if not spread.passes_filters:
        fail_str = " | ".join(spread.filter_failures)
        console.print(Panel(f"[red]SKIPPED[/red]: {fail_str}", title=f"{label} #{num}", border_style="dim"))
        return

    lines = [
        f"[bold]{spread.underlying}[/bold]  {spread.expiry} ({spread.dte} DTE)",
        "",
        f"Short {spread.short_leg.contract_type.upper()}: ${spread.short_leg.strike:,.0f}  |  "
        f"Long {spread.long_leg.contract_type.upper()}: ${spread.long_leg.strike:,.0f}",
        f"Width: ${spread.spread_width:.0f}  |  Net Credit: [green]${spread.credit:.2f}[/green]",
        f"Max Loss: [red]${spread.max_loss_usd:.0f}/contract[/red]  |  "
        f"Breakeven: ${spread.breakeven:,.2f}",
        f"Win Probability: [bold]{spread.pop:.0f}%[/bold]  |  Contracts: {spread.contracts}",
        f"Total Credit: [green]${spread.total_credit_usd:.0f}[/green]",
        "",
        f"[dim]Profit Target: Close at ${spread.profit_target:.2f} (50% of credit)[/dim]",
        f"[dim]Stop Loss: Exit if spread reaches ${spread.stop_loss:.2f} (2× credit)[/dim]",
        "",
        f"[bold cyan]Fidelity Order:[/bold cyan] SELL {spread.contracts} "
        f"{spread.short_leg.contract_type.upper()} {spread.expiry} "
        f"${spread.short_leg.strike:.0f} / BUY {spread.contracts} "
        f"{spread.long_leg.contract_type.upper()} {spread.expiry} "
        f"${spread.long_leg.strike:.0f}  ·  Net Credit LIMIT ${spread.credit:.2f}",
    ]
    console.print(Panel("\n".join(lines), title=f"[{color}]{label} #{num}[/{color}]", border_style=color))


def display_iron_condor(condor) -> None:
    lines = [
        f"[bold]{condor.underlying}[/bold]  {condor.expiry} ({condor.dte} DTE)",
        f"Selected: {condor.selection_rationale}" if condor.selection_rationale else "",
        "",
        f"PUT  SPREAD: Short ${condor.put_spread.short_leg.strike:,.0f} / "
        f"Long ${condor.put_spread.long_leg.strike:,.0f}  "
        f"Credit [green]${condor.put_spread.credit:.2f}[/green]  "
        f"PoP {condor.put_spread.pop:.0f}%",
        f"CALL SPREAD: Short ${condor.call_spread.short_leg.strike:,.0f} / "
        f"Long ${condor.call_spread.long_leg.strike:,.0f}  "
        f"Credit [green]${condor.call_spread.credit:.2f}[/green]  "
        f"PoP {condor.call_spread.pop:.0f}%",
        "",
        f"Total Credit: [green bold]${condor.total_credit:.2f}[/green bold]  "
        f"Max Loss/Contract: [red]${condor.max_loss_usd:.0f}[/red]",
        f"Breakeven Zone: ${condor.breakeven_low:,.2f} – ${condor.breakeven_high:,.2f}  "
        f"(${condor.profit_zone_width:,.0f} wide)",
        f"Win Probability: [bold]{condor.win_probability:.0f}%[/bold]  "
        f"Contracts: {condor.contracts}  Risk: {condor.account_risk_pct:.1f}% of account",
        "",
        f"Profit Target: Close when spread value ≤ [cyan]${condor.profit_target:.2f}[/cyan]  (50% of credit)",
        f"Stop Loss:     Exit if spread value ≥ [red]${condor.stop_loss:.2f}[/red]  (2× credit)",
        f"Adjust:  Put ≤ ${condor.adjustment_trigger_low:,.2f} or Call ≥ ${condor.adjustment_trigger_high:,.2f}",
    ]
    if condor.notes:
        for n in condor.notes:
            lines.append(f"[yellow]⚠ {n}[/yellow]")
    console.print(Panel("\n".join(lines), title="[magenta]IRON CONDOR[/magenta]", border_style="magenta"))


def display_scored_trade(scored) -> None:
    color = {"STRONG TRADE": "green", "ACCEPTABLE": "cyan", "WEAK": "yellow", "NO TRADE": "red"}.get(scored.verdict, "white")
    bar = "█" * scored.score + "░" * (25 - scored.score)

    lines = [f"Score: [bold]{scored.score}/25[/bold]  [{color}]{bar}[/{color}]"]
    for factor, pts in scored.score_breakdown.items():
        lines.append(f"  {factor:<25} {'★'*pts}{'·'*(5-pts)}  {pts}/5")
    lines.append("")
    for w in scored.why:
        lines.append(f"  • {w}")
    if scored.risk_flags:
        lines.append("")
        for f in scored.risk_flags:
            lines.append(f"  [yellow]⚠ {f}[/yellow]")
    if scored.no_trade_reason:
        lines.append(f"\n[red bold]NO TRADE: {scored.no_trade_reason}[/red bold]")
    elif scored.size_recommendation:
        lines.append(f"\n[cyan]{scored.size_recommendation}[/cyan]")

    console.print(Panel("\n".join(lines), title=f"[{color}]{scored.verdict}[/{color}]", border_style=color))


def display_daily_spread_result(result) -> None:
    print_header("DAILY CREDIT SPREAD SCAN")
    display_market_classifier(result.classifier)
    if result.expected_move:
        display_expected_move(result.expected_move)
    if result.recommended_action == "SKIP":
        console.print(Panel(f"[red bold]SKIP — {result.skip_reason}[/red bold]", title="Today's Action", border_style="red"))
        return
    console.print(f"\n[bold]Recommended Action:[/bold] [cyan]{result.recommended_action}[/cyan]\n")
    if result.put_spread:
        display_credit_spread(result.put_spread, num=1)
    if result.call_spread:
        display_credit_spread(result.call_spread, num=2)


def display_theta_report(report) -> None:
    print_header("THETA DECAY REPORT")
    if not report.positions:
        console.print("[dim]No open positions tracked in positions.json[/dim]")
        return

    t = Table(title="Position Theta Summary", box=box.SIMPLE)
    t.add_column("ID", style="dim")
    t.add_column("Strategy")
    t.add_column("Expiry")
    t.add_column("DTE", justify="right")
    t.add_column("θ/Day", justify="right", style="green")
    t.add_column("Weekly", justify="right")
    t.add_column("Gamma Risk", style="yellow")

    for p in report.positions:
        gamma_str = "CRITICAL" if p.gamma_critical else ("IN ZONE" if p.in_gamma_zone else "OK")
        t.add_row(
            p.position_id[:12],
            p.strategy,
            p.expiry,
            str(p.dte),
            f"${p.net_theta_per_day:.2f}",
            f"${p.weekly_target_usd:.2f}",
            gamma_str,
        )

    console.print(t)
    console.print(f"\nPortfolio Daily Theta: [green bold]${report.total_net_theta:.2f}[/green bold]")

    if report.positions:
        p0 = report.positions[0]
        console.print(f"\n30d projection (first position): [cyan]${p0.projection_30d:,.0f}[/cyan]")
        console.print(f"60d projection: [cyan]${p0.projection_60d:,.0f}[/cyan]")
        console.print(f"90d projection: [cyan]${p0.projection_90d:,.0f}[/cyan]")

        # Hourly curve
        h_table = Table(title="Today's Hourly Theta (approx)", box=box.SIMPLE)
        h_table.add_column("Hour")
        h_table.add_column("$ Earned", justify="right", style="green")
        h_table.add_column("% of Day", justify="right")
        for period, usd in p0.hourly_curve.items():
            day_pct = (usd / p0.net_theta_per_day * 100) if p0.net_theta_per_day else 0
            h_table.add_row(period, f"${usd:.3f}", f"{day_pct:.0f}%")
        console.print(h_table)


def display_goal_profile(goal) -> None:
    risk_colors = {
        "CONSERVATIVE": "green",
        "MODERATE": "cyan",
        "AGGRESSIVE": "yellow",
        "VERY_AGGRESSIVE": "red",
    }
    color = risk_colors.get(goal.risk_profile, "white")
    feasibility_colors = {"REALISTIC": "green", "STRETCH": "yellow", "UNLIKELY": "red"}
    fc = feasibility_colors.get(goal.feasibility, "white")

    lines = [
        f"Goal: [bold]${goal.target_profit_usd:,.0f}[/bold] in {goal.horizon_days} days",
        f"Account: ${goal.account_size:,.0f}  →  Required return: {goal.required_return_pct:.1f}% total / {goal.weekly_return_needed:.2f}%/week",
        "",
        f"Risk Profile: [{color}]{goal.risk_profile}[/{color}]  (Score {goal.risk_score}/10)",
        f"Strategy: [bold]{goal.recommended_strategy}[/bold]",
        f"{goal.strategy_description}",
        "",
        f"Target Delta: {goal.target_delta}  |  Win Rate: {goal.win_probability_estimate:.0f}%",
        f"Daily Target: [green]${goal.daily_target_usd:.2f}[/green]  |  Weekly: [green]${goal.weekly_target_usd:.2f}[/green]",
        f"Max Risk/Trade: [red]${goal.max_risk_per_trade_usd:.0f}[/red]  |  Trades/Week: {goal.trades_per_week}",
        "",
        f"Feasibility: [{fc}]{goal.feasibility}[/{fc}]  ({goal.probability_of_goal:.0f}% probability of hitting goal)",
        f"[italic]{goal.feasibility_note}[/italic]",
    ]

    console.print(Panel("\n".join(lines), title=f"[{color}]GOAL PROFILE[/{color}]", border_style=color))

    if goal.milestones:
        console.print("\n[bold]Weekly Milestones:[/bold]")
        for m in goal.milestones[:6]:
            bar_filled = int(m["pct_of_account"] / (goal.required_return_pct or 1) * 20)
            bar = "█" * min(bar_filled, 20) + "░" * max(0, 20 - bar_filled)
            console.print(f"  {m['label']}  [{color}]{bar}[/{color}]")


def display_spread_positions(statuses: list) -> None:
    print_header("POSITION MONITOR")
    if not statuses:
        console.print("[dim]No open positions in positions.json[/dim]")
        return

    t = Table(box=box.ROUNDED)
    t.add_column("ID", style="dim")
    t.add_column("Strategy")
    t.add_column("Underlying")
    t.add_column("Expiry")
    t.add_column("DTE", justify="right")
    t.add_column("Credit", justify="right")
    t.add_column("Current Val", justify="right")
    t.add_column("P&L", justify="right")
    t.add_column("Action", justify="center")

    for s in statuses:
        pnl_str = f"{s.pnl_pct:.0f}%" if s.pnl_pct is not None else "—"
        cur_str = f"${s.current_value:.2f}" if s.current_value is not None else "—"
        action_color = _ACTION_COLORS.get(s.action, "white")
        t.add_row(
            s.position_id[:12],
            s.strategy,
            s.underlying,
            s.expiry,
            str(s.dte),
            f"${s.credit_received:.2f}",
            cur_str,
            pnl_str,
            f"[{action_color}]{s.action}[/{action_color}]",
        )

    console.print(t)
    for s in statuses:
        if s.action not in ("HOLD", "EXPIRED"):
            action_color = _ACTION_COLORS.get(s.action, "white")
            console.print(f"  [{action_color}]{s.position_id[:12]}[/{action_color}]: {s.reason}")


def display_strike_recommendation(rec) -> None:
    print_header(f"STRIKE SELECTOR — {rec.underlying}")
    display_expected_move(rec.expected_move)

    t = Table(title="Probability Map", box=box.SIMPLE)
    t.add_column("Side")
    t.add_column("Short Strike", justify="right")
    t.add_column("Long Strike", justify="right")
    t.add_column("Delta", justify="right")
    t.add_column("Win Rate", justify="right", style="green")

    t.add_row(
        "PUT",
        f"${rec.put_short_strike:,.0f}",
        f"${rec.put_long_strike:,.0f}",
        f"{abs(rec.put_delta):.3f}",
        f"{rec.put_win_probability:.0f}%",
    )
    t.add_row(
        "CALL",
        f"${rec.call_short_strike:,.0f}",
        f"${rec.call_long_strike:,.0f}",
        f"{abs(rec.call_delta):.3f}",
        f"{rec.call_win_probability:.0f}%",
    )
    console.print(t)

    if rec.wider_strikes:
        console.print(f"[yellow]⚠ Wider strikes applied (+{rec.event_widening:.0f}pts) — major event today[/yellow]")
    if rec.put_skew_adjustment > 0:
        console.print(f"[yellow]⚠ Put skew detected — short put moved {rec.put_skew_adjustment:.0f}pts further OTM[/yellow]")
    for n in rec.notes:
        console.print(f"  [dim]{n}[/dim]")
