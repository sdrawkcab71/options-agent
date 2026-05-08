"""
Massive.com API client (rebranded Polygon.io).
Identical endpoints; auth is Bearer token instead of ?apiKey= query param.
"""
import time
from datetime import date, timedelta
from typing import Optional

import requests

_MIN_CALL_INTERVAL = 0.25  # 5 req/min free tier ceiling
_BASE = "https://api.polygon.io"  # Massive.com still resolves to Polygon infrastructure


class MassiveClient:
    def __init__(self, api_key: str):
        self._key = api_key
        self._last_call = 0.0
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {api_key}"})

    def _get(self, path: str, params: Optional[dict] = None, retries: int = 1) -> dict:
        now = time.time()
        wait = _MIN_CALL_INTERVAL - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

        resp = self._session.get(f"{_BASE}{path}", params=params or {}, timeout=15)

        if resp.status_code == 429 and retries > 0:
            time.sleep(61)
            return self._get(path, params, retries - 1)

        if not resp.ok:
            raise RuntimeError(f"Massive API {resp.status_code}: {resp.text[:200]}")

        return resp.json()

    def stock_snapshot(self, ticker: str) -> dict:
        data = self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
        return data.get("ticker", {})

    def stock_snapshots(self, tickers: list[str]) -> dict:
        joined = ",".join(tickers)
        data = self._get(
            "/v2/snapshot/locale/us/markets/stocks/tickers",
            params={"tickers": joined},
        )
        result = {}
        for t in data.get("tickers", []):
            result[t["ticker"]] = t
        return result

    def index_snapshot(self, index_ticker: str) -> dict:
        data = self._get(
            "/v3/snapshot/indices",
            params={"ticker": index_ticker},
        )
        results = data.get("results", [])
        return results[0] if results else {}

    def daily_bars(self, ticker: str, lookback_days: int = 75) -> list[dict]:
        # Buffer extra days for weekends/holidays
        end = date.today()
        start = end - timedelta(days=lookback_days + 30)
        data = self._get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            params={"adjusted": "true", "sort": "asc", "limit": 300},
        )
        return data.get("results", [])

    def options_chain(
        self,
        ticker: str,
        min_expiry: Optional[str] = None,
        max_expiry: Optional[str] = None,
    ) -> list[dict]:
        params: dict = {"limit": 250}
        if min_expiry:
            params["expiration_date.gte"] = min_expiry
        if max_expiry:
            params["expiration_date.lte"] = max_expiry

        results: list[dict] = []
        url = f"/v3/snapshot/options/{ticker}"
        while url:
            data = self._get(url, params)
            results.extend(data.get("results", []))
            next_url = data.get("next_url")
            if next_url:
                # next_url is a full URL; strip base and re-fetch
                url = next_url.replace(_BASE, "")
                params = {}
            else:
                break
        return results

    def option_contract_snapshot(self, option_ticker: str) -> dict:
        # option_ticker format: O:SPX260516P05200000
        underlying = option_ticker.split(":")[1][:3] if ":" in option_ticker else "SPX"
        data = self._get(f"/v3/snapshot/options/{underlying}/{option_ticker}")
        return data.get("results", {})

    def news(self, ticker: Optional[str] = None, limit: int = 15) -> list[dict]:
        params: dict = {"limit": limit, "order": "desc"}
        if ticker:
            params["ticker"] = ticker
        data = self._get("/v2/reference/news", params)
        return data.get("results", [])
