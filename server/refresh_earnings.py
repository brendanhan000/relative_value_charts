"""Rebuild the cached earnings calendar: `python -m server.refresh_earnings`.

The app refreshes this itself when it goes stale; run it manually to pre-warm the
cache so the first screener result is not waiting on Nasdaq.
"""

from __future__ import annotations

import asyncio
import sys

from .earnings import fetch_calendar, save_calendar


async def run() -> int:
    print("Fetching the Nasdaq earnings calendar…")
    try:
        payload = await fetch_calendar()
    except Exception as exc:  # noqa: BLE001 - surface whatever went wrong
        print(f"Refresh failed: {exc.__class__.__name__}: {exc}")
        print("The existing cache (if any) is untouched.")
        return 1

    path = save_calendar(payload)
    print(
        f"Saved {payload['count']} scheduled announcements "
        f"({payload['from']} → {payload['to']}) to {path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
