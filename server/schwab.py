"""Minimal async Schwab Trader API client: OAuth token management + market data."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .config import SETTINGS

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
MARKETDATA_BASE = "https://api.schwabapi.com/marketdata/v1"

# Schwab access tokens live 30 minutes; refresh a little early.
ACCESS_TOKEN_SKEW = 120
# Schwab refresh tokens hard-expire after 7 days -- no way around re-login.
REFRESH_TOKEN_LIFETIME = 7 * 24 * 3600


class SchwabAuthError(RuntimeError):
    """Raised when we have no usable credentials and the user must log in again."""


class SchwabAPIError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Schwab API {status}: {body[:500]}")
        self.status = status
        self.body = body


def _basic_auth_header(key: str, secret: str) -> str:
    raw = f"{key}:{secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def authorization_url() -> str:
    """The URL the user opens in a browser to grant access."""
    if not SETTINGS.configured:
        raise SchwabAuthError("SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set (see .env).")
    return (
        f"{AUTH_URL}?response_type=code"
        f"&client_id={SETTINGS.app_key}"
        f"&redirect_uri={SETTINGS.redirect_uri}"
    )


def extract_code(redirected_url: str) -> str:
    """Pull the `code` param out of the URL Schwab redirects the browser to.

    Schwab appends a `@` suffix to the code and does not URL-decode it, so we take
    the parsed value verbatim.
    """
    query = parse_qs(urlparse(redirected_url.strip()).query)
    codes = query.get("code")
    if not codes:
        raise SchwabAuthError(
            "No `code=` parameter found in that URL. Paste the full address bar "
            "contents after Schwab redirects you."
        )
    return codes[0]


class TokenStore:
    """Reads/writes the token JSON and knows when each token goes stale."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                self._data = {}

    def save(self, payload: dict[str, Any]) -> None:
        now = time.time()
        # A refresh does not extend the 7-day refresh-token window, so carry the
        # original expiry forward; a fresh login starts the clock over.
        refresh_expires_at = now + REFRESH_TOKEN_LIFETIME
        if payload.get("_keep_refresh_clock"):
            refresh_expires_at = self._data.get("refresh_expires_at") or refresh_expires_at

        data = {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "access_expires_at": now + float(payload.get("expires_in", 1800)),
            "refresh_expires_at": refresh_expires_at,
            "updated_at": now,
        }
        self._data = data
        self.path.write_text(json.dumps(data, indent=2))
        self.path.chmod(0o600)

    @property
    def access_token(self) -> str | None:
        return self._data.get("access_token")

    @property
    def refresh_token(self) -> str | None:
        return self._data.get("refresh_token")

    @property
    def access_valid(self) -> bool:
        exp = self._data.get("access_expires_at", 0)
        return bool(self.access_token) and time.time() < exp - ACCESS_TOKEN_SKEW

    @property
    def refresh_valid(self) -> bool:
        exp = self._data.get("refresh_expires_at", 0)
        return bool(self.refresh_token) and time.time() < exp

    def status(self) -> dict[str, Any]:
        return {
            "has_token": bool(self.access_token),
            "access_valid": self.access_valid,
            "refresh_valid": self.refresh_valid,
            "refresh_expires_at": self._data.get("refresh_expires_at"),
            "updated_at": self._data.get("updated_at"),
        }


class SchwabClient:
    def __init__(self, token_path: Path | None = None):
        self.tokens = TokenStore(token_path or SETTINGS.token_path)
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- auth

    async def exchange_code(self, code: str) -> None:
        """Trade an authorization code for the first access/refresh token pair."""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": SETTINGS.redirect_uri,
        }
        payload = await self._token_request(data)
        self.tokens.save(payload)

    async def _refresh(self) -> None:
        if not SETTINGS.configured:
            raise SchwabAuthError(
                "SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set. Copy .env.example to .env "
                "and fill in your Schwab app credentials."
            )
        if not self.tokens.refresh_valid:
            raise SchwabAuthError(
                "Refresh token missing or older than 7 days. Re-run `python -m server.login`."
            )
        payload = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": self.tokens.refresh_token}
        )
        # A refresh does NOT extend the 7-day refresh-token window.
        payload["_keep_refresh_clock"] = True
        self.tokens.save(payload)

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        if not SETTINGS.configured:
            raise SchwabAuthError("SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set (see .env).")
        resp = await self._client.post(
            TOKEN_URL,
            data=data,
            headers={
                "Authorization": _basic_auth_header(SETTINGS.app_key, SETTINGS.app_secret),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if resp.status_code != 200:
            raise SchwabAuthError(f"Token request failed ({resp.status_code}): {resp.text[:400]}")
        return resp.json()

    async def _access_token(self) -> str:
        async with self._lock:
            if not self.tokens.access_valid:
                await self._refresh()
            token = self.tokens.access_token
            if not token:
                raise SchwabAuthError("No access token. Run `python -m server.login`.")
            return token

    # ------------------------------------------------------------ requests

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        token = await self._access_token()
        url = f"{MARKETDATA_BASE}{path}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        resp = await self._client.get(url, params=params, headers=headers)

        if resp.status_code == 401:
            # Token rejected mid-flight; force one refresh and retry once.
            async with self._lock:
                await self._refresh()
            headers["Authorization"] = f"Bearer {self.tokens.access_token}"
            resp = await self._client.get(url, params=params, headers=headers)
            if resp.status_code == 401:
                # Still unauthorized after a fresh token: this is an auth problem,
                # not an upstream data problem -- let the UI show the login guidance.
                raise SchwabAuthError(
                    "Schwab rejected a freshly refreshed token. Re-run `python -m server.login`."
                )

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
