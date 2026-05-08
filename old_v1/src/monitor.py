"""
Step 6 — Active Position Monitor.

Loads open positions from positions.json, fetches current option prices,
and applies exit rules: +75% take profit, -50% stop loss, <3 DTE time stop.
"""

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from rich.panel import Panel

from src.output import console
from src.polygon import PolygonClient

POSITIONS_FILE = Path("positions.json")


@dataclass
class Position:
    """A single open options position."""
    ticker: str
    option_ticker: str
    strike: float
    expiry: str
    contract_type: str
    contracts: int
    entry_price: float


@dataclass
class PositionStatus:
    """Current status of an open position with exit recommendation."""
    position: Position
    current_price: Optional[float]
    pnl_pct: Optional[float]
    pnl_usd: Optional[float]
    dte: int
    action: str       # HOLD / TAKE PROFIT / STOP OUT / ROLL / EXIT (time stop)
    reason: str


def _load_positions() -> list[Position]:
    """
    Load positions from positions.json.

    Returns:
        List of Position objects. Returns empty list if file not found.
    """
    if not POSITIONS_FILE.exists():
        return []
    try:
        raw = json.loads(POSITIONS_FILE.read_text())
        positions: list[Position] = []
        for item in raw:
            if "_comment" in item:
                continue
            positions.append(Position(
                ticker=item["ticker"],
                option_ticker=item.get("option_ticker", ""),
                strike=float(item["strike"]),
                expiry=item["expiry"],
                contract_type=item["contract_type"],
                contracts=int(item.get("contracts", 1)),
                entry_price=float(item["entry_price"]),
            ))
        return positions
    except (json.JSONDecodeError, KeyError) as exc:
        console.print(f"[red]Error reading positions.json: {exc}[/red]")
        return []


def _fetch_current_price(client: PolygonClient, pos: Position) -> Optional[float]:
    """
    Attempt to fetch the current mid-price for an open option contract.

    Tries the underlying options chain and matches by strike/expiry.
    Returns None if data is unavailable.
    """
    try:
        from datetime import date as d
        today = d.today()
        chain = client.options_chain(
            pos.ticker,
            min_expiry=pos.expiry,
            max_expiry=pos.expiry,
        )
        for contract in chain.get("results", []):
            details = contract.get("details", {})
            if (
                abs(float(details.get("strike_price", 0)) - pos.strike) < 0.01
                and details.get("contract_type", "") == pos.contract_type
            ):
                quote = contract.get("last_quote", {})
                bid = float(quote.get("bid") or 0)
                ask = float(quote.get("ask") or 0)
                if bid > 0 and ask > 0:
                    return round((bid + ask) / 2, 2)
    except RuntimeError:
        pass
    return None


def _determine_action(pos: Position, current_price: Optional[float], dte: int) -> tuple[str, str]:
    """
    Apply exit rules and return (action, reason).

    Rules (in priority order):
    1. Time stop: < 3 DTE and not profitable → EXIT
    2. Take profit: current ≥ entry × 1.75
    3. Stop loss: current ≤ entry × 0.50
    4. Otherwise: HOLD
    """
    if dte < 0:
        return "EXPIRED", "Position has expired"

    if current_price is None:
        return "HOLD", "Could not fetch current price — check manually"

    ratio = current_price / pos.entry_price if pos.entry_price > 0 else 1.0

    if dte < 3:
        if ratio <= 1.0:
            return "EXIT", f"Time stop: {dte} DTE remaining and position not profitable"
        else:
            return "ROLL", f"{dte} DTE remaining — consider rolling to later expiry"

    if ratio >= 1.75:
        return "TAKE PROFIT", f"Up {(ratio-1)*100:.0f}% — at or beyond +75% target"

    if ratio <= 0.50:
        return "STOP OUT", f"Down {(1-ratio)*100:.0f}% — hit –50% stop loss"

    return "HOLD", f"P&L {(ratio-1)*100:+.0f}% — within normal range, hold"


def check_positions(api_key: str) -> list[PositionStatus]:
    """
    Load all positions and evaluate their current status.

    Args:
        api_key: Polygon.io API key.

    Returns:
        List of PositionStatus objects.
    """
    positions = _load_positions()
    if not positions:
        return []

    client = PolygonClient(api_key)
    statuses: list[PositionStatus] = []
    today = date.today()

    for pos in positions:
        try:
            expiry_date = date.fromisoformat(pos.expiry)
            dte = (expiry_date - today).days
        except ValueError:
            dte = -1

        current = _fetch_current_price(client, pos)
        pnl_pct: Optional[float] = None
        pnl_usd: Optional[float] = None
        if current is not None and pos.entry_price > 0:
            pnl_pct = round((current / pos.entry_price - 1) * 100, 1)
            pnl_usd = round((current - pos.entry_price) * 100 * pos.contracts, 0)

        action, reason = _determine_action(pos, current, dte)
        statuses.append(PositionStatus(
            position=pos,
            current_price=current,
            pnl_pct=pnl_pct,
            pnl_usd=pnl_usd,
            dte=dte,
            action=action,
            reason=reason,
        ))

    return statuses


def display_positions(statuses: list[PositionStatus]) -> None:
    """Print position monitor output to the terminal."""
    if not statuses:
        console.print(
            Panel(
                "[yellow]No open positions found.[/yellow]\n\n"
                "Edit [bold]positions.json[/bold] to add your open trades.\n"
                "Format: ticker, option_ticker, strike, expiry, contract_type, contracts, entry_price",
                title="Position Monitor",
                border_style="yellow",
            )
        )
        return

    console.rule("[bold cyan]OPEN POSITIONS[/bold cyan]")
    for ps in statuses:
        pos = ps.position
        action_colors = {
            "HOLD": "green", "TAKE PROFIT": "bright_green",
            "STOP OUT": "red", "EXIT": "red", "ROLL": "yellow",
            "EXPIRED": "dim", "TAKE PROFIT": "bright_green",
        }
        color = action_colors.get(ps.action, "white")

        price_str = f"${ps.current_price:.2f}" if ps.current_price else "N/A"
        pnl_str = (
            f"[{'green' if (ps.pnl_pct or 0) >= 0 else 'red'}]"
            f"{ps.pnl_pct:+.1f}%  (${ps.pnl_usd:+,.0f})[/]"
            if ps.pnl_pct is not None else "N/A"
        )

        lines = [
            f"[bold]{pos.ticker}  ${pos.strike:.0f} {pos.contract_type.upper()}  exp {pos.expiry}[/bold]",
            f"Entry: ${pos.entry_price:.2f}  |  Current: {price_str}  |  P&L: {pnl_str}",
            f"DTE: {ps.dte}",
            f"[bold {color}]>> {ps.action}[/bold {color}]  {ps.reason}",
        ]
        console.print(Panel("\n".join(lines), border_style=color, padding=(0, 1)))


def check_single_exit(api_key: str, ticker: str) -> None:
    """
    Print exit recommendation for a specific ticker's open position.

    Args:
        api_key: Polygon.io API key.
        ticker: Ticker symbol to check.
    """
    statuses = check_positions(api_key)
    matches = [s for s in statuses if s.position.ticker.upper() == ticker.upper()]
    if not matches:
        console.print(f"[yellow]No open position found for {ticker} in positions.json[/yellow]")
        return
    display_positions(matches)
