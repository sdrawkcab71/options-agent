"""
Position Monitor — multi-leg spread and condor monitoring with exit rules.
"""
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from src.config import STOP_LOSS_MULTIPLIER, PROFIT_TARGET_PCT, GAMMA_RISK_DTE
from src.massive_client import MassiveClient

_POSITIONS_FILE = Path("positions.json")


@dataclass
class SpreadStatus:
    position_id: str
    strategy: str
    underlying: str
    expiry: str
    dte: int
    credit_received: float
    current_value: Optional[float]  # current cost to close spread (buy it back)
    pnl_pct: Optional[float]        # % of credit captured
    pnl_usd: Optional[float]
    action: str                     # "HOLD" | "TAKE PROFIT" | "STOP OUT" | "ADJUST" | "EXPIRED" | "UNKNOWN"
    reason: str
    in_gamma_zone: bool
    adjustment_needed: bool
    contracts: int = 1
    underlying_price: Optional[float] = None
    notes: list[str] = field(default_factory=list)


def _dte(expiry: str) -> int:
    try:
        return max(0, (date.fromisoformat(expiry) - date.today()).days)
    except Exception:
        return -1


def _fetch_spread_current_value(
    position: dict,
    client: MassiveClient,
    underlying_price: Optional[float] = None,
) -> Optional[float]:
    """Fetch current mid-price net value of all legs (cost to close)."""
    legs = position.get("legs", [])
    if not legs:
        return None

    total = 0.0
    for leg in legs:
        ticker = leg.get("option_ticker", "")
        if not ticker:
            continue
        try:
            snap = client.option_contract_snapshot(ticker)
            quote = snap.get("last_quote", {})
            bid = float(quote.get("bid", 0))
            ask = float(quote.get("ask", 0))
            if bid and ask:
                mid = (bid + ask) / 2.0
            elif snap.get("day", {}).get("close"):
                mid = float(snap["day"]["close"])
            else:
                continue
            # Short legs add value when you buy to close (costs money)
            # Long legs credit value when you sell to close
            role = leg.get("role", "short")
            sign = 1.0 if role == "short" else -1.0
            total += sign * mid
        except Exception:
            continue

    return round(total, 2) if total != 0.0 else None


def _determine_action(
    position: dict,
    current_value: Optional[float],
    dte: int,
    underlying_price: Optional[float] = None,
) -> tuple[str, str, bool, bool]:
    """Returns (action, reason, adjustment_needed, in_gamma_zone)."""
    credit = float(position.get("credit_received", 0))
    in_gamma = dte <= GAMMA_RISK_DTE

    if dte < 0:
        return "EXPIRED", "Position expired", False, False

    if current_value is None:
        return "UNKNOWN", "Cannot fetch current price", False, in_gamma

    stop_loss_threshold = credit * STOP_LOSS_MULTIPLIER
    profit_target_threshold = credit * PROFIT_TARGET_PCT

    # DTE < 3 time stop
    if dte <= 2:
        if current_value <= profit_target_threshold:
            return "TAKE PROFIT", f"DTE {dte} — close profitably before expiry", False, in_gamma
        return "ROLL", f"DTE {dte} — roll to avoid assignment / gamma risk", False, in_gamma

    if current_value >= stop_loss_threshold:
        return (
            "STOP OUT",
            f"Spread value ${current_value:.2f} hit 2× credit (${stop_loss_threshold:.2f})",
            False,
            in_gamma,
        )

    if current_value <= profit_target_threshold:
        return (
            "TAKE PROFIT",
            f"50% profit target hit (current ${current_value:.2f} ≤ ${profit_target_threshold:.2f})",
            False,
            in_gamma,
        )

    # Adjustment check
    adjustment_needed = False
    adj_reason = ""
    if underlying_price:
        put_short = position.get("put_short_strike") or position.get("short_strike")
        call_short = position.get("call_short_strike")
        if put_short:
            distance = underlying_price - float(put_short)
            spread_width = abs(float(position.get("put_short_strike", 0)) - float(position.get("put_long_strike", 0) or 0)) or 5.0
            if distance < spread_width * 1.5:
                adjustment_needed = True
                adj_reason = f"Price ${underlying_price:.0f} approaching put spread (short ${put_short})"
        if call_short and not adjustment_needed:
            distance = float(call_short) - underlying_price
            spread_width = abs(float(position.get("call_short_strike", 0)) - float(position.get("call_long_strike", 0) or 0)) or 5.0
            if distance < spread_width * 1.5:
                adjustment_needed = True
                adj_reason = f"Price ${underlying_price:.0f} approaching call spread (short ${call_short})"

    if adjustment_needed:
        return "ADJUST", adj_reason, True, in_gamma

    return "HOLD", f"Within profit zone — DTE {dte}, value ${current_value:.2f}", False, in_gamma


def check_spread_positions(api_key: str) -> list[SpreadStatus]:
    if not _POSITIONS_FILE.exists():
        return []

    try:
        raw = json.loads(_POSITIONS_FILE.read_text())
    except Exception:
        return []

    client = MassiveClient(api_key)
    statuses: list[SpreadStatus] = []

    for pos in raw:
        if not isinstance(pos, dict) or pos.get("_comment"):
            continue
        if pos.get("status") not in ("open", None):
            continue

        pos_id = pos.get("id", "unknown")
        strategy = pos.get("type", pos.get("strategy", "spread"))
        underlying = pos.get("underlying", "SPX")
        expiry = pos.get("expiry", "")
        dte = _dte(expiry)
        credit = float(pos.get("credit_received", 0))
        contracts = int(pos.get("contracts", 1))

        # Try to get underlying price
        underlying_price: Optional[float] = None
        try:
            snap = client.stock_snapshot(underlying)
            day = snap.get("day", {})
            underlying_price = float(day.get("c") or snap.get("lastTrade", {}).get("p", 0) or 0)
        except Exception:
            pass

        current_val = _fetch_spread_current_value(pos, client, underlying_price)

        # P&L
        pnl_pct: Optional[float] = None
        pnl_usd: Optional[float] = None
        if current_val is not None and credit > 0:
            pnl_pct = round((credit - current_val) / credit * 100, 1)
            pnl_usd = round((credit - current_val) * contracts * 100, 2)

        action, reason, adjustment_needed, in_gamma = _determine_action(
            pos, current_val, dte, underlying_price
        )

        statuses.append(SpreadStatus(
            position_id=pos_id,
            strategy=strategy,
            underlying=underlying,
            expiry=expiry,
            dte=dte,
            credit_received=credit,
            current_value=current_val,
            pnl_pct=pnl_pct,
            pnl_usd=pnl_usd,
            action=action,
            reason=reason,
            in_gamma_zone=in_gamma,
            adjustment_needed=adjustment_needed,
            contracts=contracts,
            underlying_price=underlying_price,
        ))

    return statuses


def save_position(position: dict) -> None:
    existing: list = []
    if _POSITIONS_FILE.exists():
        try:
            existing = json.loads(_POSITIONS_FILE.read_text())
        except Exception:
            pass
    existing.append(position)
    _POSITIONS_FILE.write_text(json.dumps(existing, indent=2))


def load_positions() -> list[dict]:
    if not _POSITIONS_FILE.exists():
        return []
    try:
        return [p for p in json.loads(_POSITIONS_FILE.read_text()) if not p.get("_comment")]
    except Exception:
        return []
