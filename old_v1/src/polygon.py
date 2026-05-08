"""
Polygon.io REST API client.

Handles auth, rate limiting, and response parsing for the free tier
(15-min delayed, ~5 req/min). Automatically backs off on 429s.
"""

import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests

BASE_URL = "https://api.polygon.io"
MIN_CALL_INTERVAL = 0.25  # seconds; conservative for free tier


class PolygonClient:
    """Thin, typed wrapper around Polygon REST API."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._session = requests.Session()
        self._session.params = {"apiKey": api_key}  # type: ignore[assignment]
        self._last_call_ts: float = 0.0

    # ── Core request ─────────────────────────────────────────────────────────

    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Rate-limited GET request to Polygon.

        Args:
            endpoint: URL path after BASE_URL (e.g. '/v2/snapshot/...').
            params: Additional query params (apiKey injected automatically).

        Returns:
            Parsed JSON response dict.

        Raises:
            RuntimeError: On HTTP error or network failure.
        """
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < MIN_CALL_INTERVAL:
            time.sleep(MIN_CALL_INTERVAL - elapsed)
        self._last_call_ts = time.monotonic()

        url = f"{BASE_URL}{endpoint}"
        try:
            resp = self._session.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                # Rate limited — back off and retry once
                time.sleep(61)
                resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()  # type: ignore[return-value]
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"Polygon HTTP {resp.status_code}: {resp.text[:300]}"
            ) from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"Polygon network error: {exc}") from exc

    # ── Market data ──────────────────────────────────────────────────────────

    def stock_snapshots(self, tickers: List[str]) -> Dict[str, Any]:
        """Snapshot (price + daily change) for a list of stock tickers."""
        return self._get(
            "/v2/snapshot/locale/us/markets/stocks/tickers",
            params={"tickers": ",".join(tickers)},
        )

    def daily_bars(self, ticker: str, lookback_days: int = 75) -> Dict[str, Any]:
        """
        Daily OHLCV bars going back lookback_days calendar days.

        Args:
            ticker: Stock symbol (e.g. 'AAPL').
            lookback_days: Calendar days of history; adds weekend buffer automatically.

        Returns:
            Polygon /v2/aggs response dict with 'results' list of bars.
        """
        end = date.today()
        start = end - timedelta(days=lookback_days + 15)  # buffer for holidays
        return self._get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            params={"adjusted": "true", "sort": "asc", "limit": 150},
        )

    def options_chain(
        self,
        ticker: str,
        min_expiry: Optional[str] = None,
        max_expiry: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Options chain snapshot with greeks, IV, and volume.

        Args:
            ticker: Underlying symbol (e.g. 'NVDA').
            min_expiry: ISO date 'YYYY-MM-DD' for earliest expiry filter.
            max_expiry: ISO date 'YYYY-MM-DD' for latest expiry filter.

        Returns:
            Polygon /v3/snapshot/options response with 'results' list.
        """
        params: Dict[str, Any] = {"limit": 250}
        if min_expiry:
            params["expiration_date.gte"] = min_expiry
        if max_expiry:
            params["expiration_date.lte"] = max_expiry
        return self._get(f"/v3/snapshot/options/{ticker}", params=params)

    def option_contract_snapshot(self, option_ticker: str) -> Optional[Dict[str, Any]]:
        """
        Snapshot for a single option contract.

        Args:
            option_ticker: Full option ticker (e.g. 'O:AAPL250321C00175000').

        Returns:
            Contract snapshot dict or None on failure.
        """
        try:
            data = self._get(f"/v3/snapshot/options/{option_ticker.split(':')[1][:4]}")
            # Fallback: fetch the underlying snapshot instead
            result = self._get(f"/v3/snapshot/options/{option_ticker}")
            results = result.get("results", [])
            return results[0] if results else None
        except RuntimeError:
            return None

    def index_snapshot(self, index_ticker: str) -> Optional[Dict[str, Any]]:
        """
        Snapshot for a market index (e.g. 'I:VIX').

        Returns:
            Single result dict, or None if unavailable on this plan.
        """
        try:
            data = self._get("/v3/snapshot/indices", params={"tickers": index_ticker})
            results = data.get("results", [])
            return results[0] if results else None
        except RuntimeError:
            return None
