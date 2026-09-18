"""Async client for Schwab market data, served by the central schwab_hub.

The hub owns the credentials and the 7-day login (../schwab_hub/run.sh login);
this project holds no Schwab secrets.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import SETTINGS

MARKETDATA = "/marketdata/v1"


class SchwabAuthError(RuntimeError):
    """The hub is down, unconfigured, or needs a Schwab re-login."""


class SchwabAPIError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Schwab API {status}: {body[:500]}")
        self.status = status
        self.body = body


class SchwabClient:
    def __init__(self, hub_url: str | None = None):
        self._client = httpx.AsyncClient(base_url=hub_url or SETTINGS.hub_url, timeout=90.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def auth_status(self) -> dict[str, Any]:
        """Shape the UI expects: `ready` gates the app, `credentials_configured` picks the message."""
        try:
            health = (await self._client.get("/health")).json()
        except httpx.HTTPError:
            return {"ready": False, "credentials_configured": False, "detail": "schwab_hub is not running."}
        ok = bool(health.get("ok")) and health.get("relogin_in_days", 0) > 0
        return {
            "ready": ok,
            "credentials_configured": bool(health.get("ok")),
            "relogin_in_days": health.get("relogin_in_days"),
            "detail": health.get("detail"),
        }

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            resp = await self._client.get(f"{MARKETDATA}{path}", params=params)
        except httpx.ConnectError as exc:
            raise SchwabAuthError("schwab_hub is not running. Start it with ../schwab_hub/run.sh.") from exc
        if resp.status_code in (401, 503):
            try:
                body = resp.json()
                msg = f"{body.get('detail', '')} {body.get('hint', '')}".strip()
            except ValueError:
                msg = resp.text[:200]
            raise SchwabAuthError(msg or "Schwab rejected the hub's token. Run ../schwab_hub/run.sh login.")
        if resp.status_code != 200:
            raise SchwabAPIError(resp.status_code, resp.text)
        return resp.json()

    async def price_history(
        self,
        symbol: str,
        *,
        period_type: str,
        frequency_type: str,
        frequency: int,
        start_ms: int | None = None,
        end_ms: int | None = None,
        period: int | None = None,
        extended_hours: bool = False,
    ) -> list[dict[str, Any]]:
        """Return raw candles: [{open, high, low, close, volume, datetime(ms)}, ...]."""
        params: dict[str, Any] = {
            "symbol": symbol,
            "periodType": period_type,
            "frequencyType": frequency_type,
            "frequency": frequency,
            "needExtendedHoursData": str(extended_hours).lower(),
            "needPreviousClose": "false",
        }
        if start_ms is not None:
            params["startDate"] = start_ms
        if end_ms is not None:
            params["endDate"] = end_ms
        if period is not None:
            params["period"] = period

        data = await self._get("/pricehistory", params)
        if data.get("empty"):
            return []
        return data.get("candles", []) or []

    async def quotes(self, symbols: list[str]) -> dict[str, Any]:
        """Latest quote per symbol, keyed by symbol."""
        if not symbols:
            return {}
        return await self._get("/quotes", {"symbols": ",".join(symbols), "fields": "quote"})


_client: Any = None


def get_client() -> Any:
    """The live Schwab client, or the synthetic stand-in when RVC_DEMO is set."""
    global _client
    if _client is None:
        if SETTINGS.demo:
            from .demo import DemoClient

            _client = DemoClient()
        else:
            _client = SchwabClient()
    return _client
