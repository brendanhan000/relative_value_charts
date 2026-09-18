"""FastAPI app: JSON endpoints for ratio series + live quotes, plus the static UI."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import SETTINGS
from .earnings import annotate, fetch_calendar, get_calendar, save_calendar
from .earnings import summary as earnings_summary
from .ratio import (
    DEFAULT_PAIR,
    MAX_SYMBOL_LEN,
    PAIRS,
    STAT_WINDOWS,
    Pair,
    build_series,
    live_ratio,
    resolve_pair,
)
from .schwab import SchwabAPIError, SchwabAuthError, get_client
from .screen import (
    ScreenParams,
    cancel_screen,
    estimate_seconds,
    get_job,
    index_options,
    start_screen,
)
from .timeframes import DEFAULT_TIMEFRAME, TIMEFRAMES
from .universe import summary

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
        "auth": await client.auth_status(),
    }


@app.get("/api/auth/status")
async def auth_status() -> dict:
    return await get_client().auth_status()


def _pair_or_400(pair: str, num: Optional[str], den: Optional[str]) -> Pair:
    """Built-in pair by key, or a custom one when num/den are supplied."""
    try:
        return resolve_pair(pair, num, den)
    except KeyError:
        raise HTTPException(404, f"Unknown pair {pair!r}") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/series")
async def series(
    pair: str = Query(DEFAULT_PAIR),
    # Supplying either symbol switches to a custom pair and ignores `pair`.
    num: Optional[str] = Query(None, max_length=MAX_SYMBOL_LEN),
    den: Optional[str] = Query(None, max_length=MAX_SYMBOL_LEN),
    timeframe: str = Query(DEFAULT_TIMEFRAME),
    # FastAPI evaluates route annotations at import, so `int | None` would break
    # on Python 3.9 even with `from __future__ import annotations`.
    lookback_days: Optional[int] = Query(None, ge=1, le=7300),
    stat_window: str = Query("full"),
    extended: bool = Query(False),
) -> dict:
    target = _pair_or_400(pair, num, den)
    if timeframe not in TIMEFRAMES:
        raise HTTPException(404, f"Unknown timeframe {timeframe!r}")
    if stat_window not in STAT_WINDOWS:
        raise HTTPException(400, f"Unknown stat window {stat_window!r}")

    try:
        return await build_series(
            get_client(),
            target,
            timeframe,
            lookback_days=lookback_days,
            stat_window=stat_window,
            extended_hours=extended,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/universe")
async def universe_info() -> dict:
    try:
        info = summary()
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc
    info["estimated_seconds"] = round(estimate_seconds(info["total"]))
    info["requests_per_minute"] = SETTINGS.screen_requests_per_minute
    return info


@app.get("/api/earnings")
async def earnings(
    symbols: str = Query(..., description="Comma-separated tickers"),
    refresh: bool = Query(False),
) -> dict:
    wanted = [s.strip() for s in symbols.split(",") if s.strip()]
    if not wanted:
        raise HTTPException(400, "Pass at least one symbol.")
    if len(wanted) > 500:
        raise HTTPException(400, "Too many symbols; 500 max per request.")

    if refresh:
        try:
            payload = await fetch_calendar()
            save_calendar(payload)
        except Exception as exc:  # noqa: BLE001 - report, keep serving the cache
            raise HTTPException(502, f"Earnings refresh failed: {exc}") from exc
    else:
        payload = await get_calendar()

    return {
        "calendar": earnings_summary(payload),
        "symbols": annotate(wanted, payload),
    }


@app.post("/api/screen")
async def screen_start(
    timeframe: str = Query("1D"),
    lookback_days: Optional[int] = Query(None, ge=1, le=7300),
    stat_window: str = Query("full"),
    prefer_qqq: bool = Query(True),
    indices: str = Query("sp500,nasdaq100"),
    extended: bool = Query(False),
    limit: Optional[int] = Query(None, ge=1, le=1000),
) -> dict:
    if timeframe not in TIMEFRAMES:
        raise HTTPException(404, f"Unknown timeframe {timeframe!r}")
    if stat_window not in STAT_WINDOWS:
        raise HTTPException(400, f"Unknown stat window {stat_window!r}")

    wanted = tuple(i.strip() for i in indices.split(",") if i.strip())
    unknown = [i for i in wanted if i not in set(index_options())]
    if unknown or not wanted:
        raise HTTPException(400, f"Unknown index {unknown or ['(none)']}; use sp500 and/or nasdaq100")

    job = start_screen(
        get_client(),
        ScreenParams(
            timeframe=timeframe,
            lookback_days=lookback_days,
            stat_window=stat_window,
            prefer_qqq=prefer_qqq,
            indices=wanted,
            extended=extended,
            limit=limit,
        ),
    )
    return job.payload(include_rows=False)


@app.get("/api/screen/{job_id}")
async def screen_status(job_id: str, rows: bool = Query(True)) -> dict:
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, f"No screen job {job_id!r}")
    return job.payload(include_rows=rows)


@app.delete("/api/screen/{job_id}")
async def screen_cancel(job_id: str) -> dict:
    if not get_job(job_id):
        raise HTTPException(404, f"No screen job {job_id!r}")
    return {"cancelled": cancel_screen(job_id)}


@app.get("/api/quote")
async def quote(
    pair: str = Query(DEFAULT_PAIR),
    num: Optional[str] = Query(None, max_length=MAX_SYMBOL_LEN),
    den: Optional[str] = Query(None, max_length=MAX_SYMBOL_LEN),
) -> dict:
    return await live_ratio(get_client(), _pair_or_400(pair, num, den))
