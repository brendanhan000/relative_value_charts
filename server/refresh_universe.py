"""Refresh the cached SPY/QQQ constituent lists: `python -m server.refresh_universe`.

Run it whenever you want current membership; the screener uses the cache otherwise.
"""

from __future__ import annotations

import asyncio
import sys

from .universe import fetch_universe, save_universe, summary


async def run() -> int:
    print("Fetching constituent lists…")
    try:
        payload = await fetch_universe()
    except Exception as exc:  # noqa: BLE001 - surface whatever went wrong verbatim
        print(f"Refresh failed: {exc.__class__.__name__}: {exc}")
        print("The existing cache (if any) is untouched.")
        return 1

    path = save_universe(payload)
    info = summary(payload)
    counts = ", ".join(f"{k}={v}" for k, v in info["counts"].items())
    print(f"Saved {info['total']} unique symbols ({counts}) to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
