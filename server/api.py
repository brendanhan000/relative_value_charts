"""FastAPI app: JSON endpoints for ratio series + live quotes, plus the static UI."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import SETTINGS
from .ratio import DEFAULT_PAIR, PAIRS, STAT_WINDOWS, build_series, live_ratio
from .schwab import SchwabAPIError, SchwabAuthError, authorization_url, get_client
from .timeframes import DEFAULT_TIMEFRAME, TIMEFRAMES

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await get_client().aclose()


app = FastAPI(title="Relative Value Charts", lifespan=lifespan)
app.mount("/vendor", StaticFiles(directory=WEB_DIR / "vendor"), name="vendor")


@app.exception_handler(SchwabAuthError)
async def _auth_error(_request, exc: SchwabAuthError):
    return JSONResponse(status_code=401, content={"error": str(exc), "kind": "auth"})


@app.exception_handler(SchwabAPIError)
async def _api_error(_request, exc: SchwabAPIError):
    return JSONResponse(
        status_code=502, content={"error": str(exc), "kind": "upstream"}
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/app.js")
async def app_js() -> FileResponse:
    return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")


@app.get("/style.css")
async def style_css() -> FileResponse:
    return FileResponse(WEB_DIR / "style.css", media_type="text/css")


@app.get("/api/config")
async def config() -> dict:
    client = get_client()
    return {
        "pairs": [
            {
                "key": p.key,
                "label": p.label,
                "description": p.description,
                "unit_hint": p.unit_hint,
                "decimals": p.decimals,
            }
            for p in PAIRS.values()
        ],
        "timeframes": [
            {
                "key": tf.key,
                "label": tf.label,
                "default_lookback_days": tf.default_lookback_days,
                "default_view_days": tf.default_view_days,
                "max_lookback_days": tf.max_lookback_days,
            }
            for tf in TIMEFRAMES.values()
        ],
        "stat_windows": list(STAT_WINDOWS),
        "defaults": {"pair": DEFAULT_PAIR, "timeframe": DEFAULT_TIMEFRAME},
        "quote_poll_seconds": SETTINGS.quote_poll_seconds,
        "demo": SETTINGS.demo,
        "auth": auth_status_payload(client),
    }


def auth_status_payload(client) -> dict:
    status = client.tokens.status()
    status["credentials_configured"] = SETTINGS.configured or SETTINGS.demo
    status["ready"] = SETTINGS.demo or (SETTINGS.configured and status["refresh_valid"])
    status["demo"] = SETTINGS.demo
    return status


@app.get("/api/auth/status")
async def auth_status() -> dict:
    payload = auth_status_payload(get_client())
    if SETTINGS.configured and not payload["ready"]:
        payload["authorization_url"] = authorization_url()
    return payload


@app.get("/api/series")
async def series(
    pair: str = Query(DEFAULT_PAIR),
    timeframe: str = Query(DEFAULT_TIMEFRAME),
    # FastAPI evaluates route annotations at import, so `int | None` would break
    # on Python 3.9 even with `from __future__ import annotations`.
    lookback_days: Optional[int] = Query(None, ge=1, le=7300),
    stat_window: str = Query("full"),
    extended: bool = Query(False),
) -> dict:
    if pair not in PAIRS:
        raise HTTPException(404, f"Unknown pair {pair!r}")
    if timeframe not in TIMEFRAMES:
        raise HTTPException(404, f"Unknown timeframe {timeframe!r}")
    if stat_window not in STAT_WINDOWS:
        raise HTTPException(400, f"Unknown stat window {stat_window!r}")

    try:
        return await build_series(
            get_client(),
            PAIRS[pair],
            timeframe,
            lookback_days=lookback_days,
            stat_window=stat_window,
            extended_hours=extended,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/quote")
async def quote(pair: str = Query(DEFAULT_PAIR)) -> dict:
    if pair not in PAIRS:
        raise HTTPException(404, f"Unknown pair {pair!r}")
    return await live_ratio(get_client(), PAIRS[pair])
