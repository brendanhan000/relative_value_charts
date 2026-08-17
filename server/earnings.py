"""Upcoming earnings dates for screener results.

Schwab's fundamental projection carries dividend dates but no earnings date, so this
comes from Nasdaq's public earnings calendar. That calendar is queried one day at a
time, so the whole forward window is pulled once and cached; lookups are then free.

Coverage is inherently partial. Nasdaq publishes confirmed dates roughly eight weeks
out (measured: the furthest date available was 55 days ahead), so a company that has
just reported has no next date yet. Those are reported as "not scheduled" rather than
estimated -- a guessed earnings date is worse than a blank one.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import ROOT

CALENDAR_PATH = ROOT / "data" / "earnings.json"
CALENDAR_URL = "https://api.nasdaq.com/api/calendar/earnings"

# Nasdaq's calendar runs out at about eight weeks; 60 days covers it with margin.
HORIZON_DAYS = 60
# Politeness: 64 weekday requests at this concurrency completed with zero failures.
FETCH_CONCURRENCY = 4
# Earnings dates move, but not by the minute.
CACHE_TTL_SECONDS = 12 * 3600

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126 Safari/537.36"
)

# Nasdaq's own wording -> the convention traders use.
_TIMING = {
    "time-pre-market": "BMO",
    "time-after-hours": "AMC",
    "time-not-supplied": "",
}


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text in {"N/A", "n/a", "--", "0"} else text


async def _fetch_day(client: httpx.AsyncClient, day: date, gate: asyncio.Semaphore) -> list[dict]:
    async with gate:
        try:
            resp = await client.get(
                CALENDAR_URL,
                params={"date": day.isoformat()},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            if resp.status_code != 200:
                return []
            payload = resp.json() or {}
        except Exception:  # noqa: BLE001 - one bad day must not sink the calendar
            return []

    rows = ((payload.get("data") or {}).get("rows")) or []
    out = []
    for row in rows:
        symbol = _clean(row.get("symbol")).upper()
        if symbol:
            out.append({"symbol": symbol, "day": day.isoformat(), "row": row})
    return out


async def fetch_calendar(horizon_days: int = HORIZON_DAYS) -> dict[str, Any]:
    """Pull the forward earnings calendar. Keeps the earliest date per symbol."""
    today = datetime.now(timezone.utc).date()
    days = [today + timedelta(days=i) for i in range(horizon_days)]
    days = [d for d in days if d.weekday() < 5]

    gate = asyncio.Semaphore(FETCH_CONCURRENCY)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        batches = await asyncio.gather(*(_fetch_day(client, d, gate) for d in days))

    entries: dict[str, dict[str, Any]] = {}
    for batch in batches:
        for item in batch:
            symbol = item["symbol"]
            # Days are processed in order, so the first hit is the soonest date.
            if symbol in entries:
                continue
            row = item["row"]
            entries[symbol] = {
                "date": item["day"],
                "when": _TIMING.get(_clean(row.get("time")), ""),
                "fiscal_quarter": _clean(row.get("fiscalQuarterEnding")),
                "eps_forecast": _clean(row.get("epsForecast")),
                "name": _clean(row.get("name")),
            }

    if not entries:
        raise ValueError("Nasdaq returned no earnings rows; refusing to cache an empty calendar.")

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "horizon_days": horizon_days,
        "from": today.isoformat(),
        "to": (today + timedelta(days=horizon_days)).isoformat(),
        "count": len(entries),
        "entries": entries,
    }


def save_calendar(payload: dict[str, Any], path: Path | None = None) -> Path:
    target = path or CALENDAR_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=1))
    return target


def load_calendar(path: Path | None = None) -> dict[str, Any] | None:
    target = path or CALENDAR_PATH
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text())
    except json.JSONDecodeError:
        return None


def is_stale(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(payload["fetched_at"])
    except (KeyError, ValueError):
        return True
    # A calendar built before today is stale regardless of age: "days away" shifts.
    if payload.get("from") != datetime.now(timezone.utc).date().isoformat():
        return True
    return age.total_seconds() > CACHE_TTL_SECONDS


_refresh_lock = asyncio.Lock()


async def get_calendar(*, allow_fetch: bool = True) -> dict[str, Any] | None:
    """Cached calendar, rebuilt when stale. Concurrent callers share one fetch."""
    payload = load_calendar()
    if not is_stale(payload) or not allow_fetch:
        return payload

    async with _refresh_lock:
        # Another caller may have refreshed it while we waited for the lock.
        payload = load_calendar()
        if not is_stale(payload):
            return payload
        try:
            fresh = await fetch_calendar()
        except Exception:  # noqa: BLE001 - fall back to whatever we already had
            return payload
        save_calendar(fresh)
        return fresh


def annotate(symbols: list[str], payload: dict[str, Any] | None) -> dict[str, Any]:
    """Per-symbol earnings info, with days-away computed against the calendar's date."""
    entries = (payload or {}).get("entries") or {}
    today = date.fromisoformat((payload or {}).get("from") or datetime.now(timezone.utc).date().isoformat())

    out: dict[str, Any] = {}
    for raw in symbols:
        symbol = raw.strip().upper()
        # Schwab writes share classes as BRK/B; Nasdaq writes BRK.B.
        entry = entries.get(symbol) or entries.get(symbol.replace("/", "."))
        if not entry:
            out[symbol] = {"scheduled": False}
            continue
        when = date.fromisoformat(entry["date"])
        out[symbol] = {
            "scheduled": True,
            "date": entry["date"],
            "days_away": (when - today).days,
            "when": entry.get("when", ""),
            "fiscal_quarter": entry.get("fiscal_quarter", ""),
            "eps_forecast": entry.get("eps_forecast", ""),
        }
    return out


def summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {"available": False}
    return {
        "available": True,
        "fetched_at": payload.get("fetched_at"),
        "from": payload.get("from"),
        "to": payload.get("to"),
        "count": payload.get("count", 0),
        "horizon_days": payload.get("horizon_days", HORIZON_DAYS),
    }
