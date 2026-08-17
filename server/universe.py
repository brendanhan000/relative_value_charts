"""Index membership for the screener.

The Schwab Trader API has no index-constituent endpoint, so the SPY and QQQ member
lists come from a public dataset and are cached to disk. The cached file is the
source of truth at runtime -- the network is only touched when you ask for a
refresh -- so a scan never depends on a third party being up.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import ROOT

UNIVERSE_PATH = ROOT / "data" / "universe.json"

# Monthly-refreshed constituent lists. Symbol + name only.
SOURCES = {
    "sp500": "https://raw.githubusercontent.com/yfiua/index-constituents/main/docs/constituents-sp500.csv",
    "nasdaq100": "https://raw.githubusercontent.com/yfiua/index-constituents/main/docs/constituents-nasdaq100.csv",
}
# Best-effort GICS sector enrichment; the screener works fine without it.
SECTOR_SOURCE = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
)

BENCHMARKS = ("SPY", "QQQ")


@dataclass(frozen=True)
class Member:
    symbol: str  # Schwab-formatted
    name: str
    sector: str = ""
    indices: tuple[str, ...] = field(default_factory=tuple)

    def benchmark(self, prefer_qqq: bool = True) -> str:
        """Which ETF this name is measured against.

        Nasdaq-100 members are benchmarked to QQQ and everything else to SPY. For
        the ~85 names in both indices, QQQ is the tighter peer group -- flip
        `prefer_qqq` to score those against SPY instead.
        """
        in_ndx = "nasdaq100" in self.indices
        if in_ndx and (prefer_qqq or "sp500" not in self.indices):
            return "QQQ"
        return "SPY"


def to_schwab_symbol(symbol: str) -> str:
    """Dataset tickers use `BRK.B`; Schwab wants `BRK/B` for share classes."""
    return symbol.strip().upper().replace(".", "/")


# --------------------------------------------------------------------- fetch


# Telltales of UTF-8 bytes that were decoded as latin-1 somewhere upstream.
_MOJIBAKE_MARKS = ("Ã", "Â", "â€")


def repair_text(text: str) -> str:
    """Undo a latin-1/UTF-8 double encoding, e.g. 'EstÃ©e' -> 'Estée'.

    The upstream constituent CSV ships some names already mangled, so this runs on
    ingest. It only touches strings that carry the telltale marks and whose repair
    round-trips cleanly, leaving genuine accented names alone.
    """
    if not any(mark in text for mark in _MOJIBAKE_MARKS):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


async def _get(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url, headers={"User-Agent": "relative-value-charts/1.0"})
    resp.raise_for_status()
    # Decode explicitly; these endpoints do not always declare a charset.
    return resp.content.decode("utf-8", errors="replace")


def _parse_symbol_name(text: str) -> list[tuple[str, str]]:
    rows = list(csv.DictReader(io.StringIO(text)))
    out = []
    for row in rows:
        symbol = (row.get("Symbol") or row.get("symbol") or "").strip()
        name = repair_text(
            (row.get("Name") or row.get("Security") or row.get("name") or "").strip()
        )
        if symbol:
            out.append((symbol, name))
    return out


def _parse_sectors(text: str) -> dict[str, str]:
    sectors: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(text)):
        symbol = (row.get("Symbol") or "").strip()
        sector = repair_text((row.get("GICS Sector") or "").strip())
        if symbol and sector:
            sectors[symbol.upper()] = sector
    return sectors


async def fetch_universe() -> dict[str, Any]:
    """Pull fresh constituent lists. Raises on network failure."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        sp500_text, ndx_text = await asyncio.gather(
            _get(client, SOURCES["sp500"]),
            _get(client, SOURCES["nasdaq100"]),
        )
        try:
            sectors = _parse_sectors(await _get(client, SECTOR_SOURCE))
        except Exception:  # enrichment only -- never fail the refresh over it
            sectors = {}

    members: dict[str, dict[str, Any]] = {}
    for index, text in (("sp500", sp500_text), ("nasdaq100", ndx_text)):
        parsed = _parse_symbol_name(text)
        if not parsed:
            raise ValueError(f"{index} source returned no rows; refusing to cache it.")
        for symbol, name in parsed:
            key = symbol.upper()
            entry = members.setdefault(
                key, {"symbol": key, "name": name, "sector": "", "indices": []}
            )
            if name and not entry["name"]:
                entry["name"] = name
            if index not in entry["indices"]:
                entry["indices"].append(index)

    for key, entry in members.items():
        entry["sector"] = sectors.get(key, "")

    counts = {
        index: sum(1 for e in members.values() if index in e["indices"])
        for index in SOURCES
    }
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sources": SOURCES,
        "counts": counts,
        "members": sorted(members.values(), key=lambda e: e["symbol"]),
    }


def save_universe(payload: dict[str, Any], path: Path | None = None) -> Path:
    target = path or UNIVERSE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=1))
    return target


# ---------------------------------------------------------------------- load

_cached: dict[str, Any] | None = None
_cached_mtime: float | None = None


def load_universe(path: Path | None = None, *, force: bool = False) -> dict[str, Any]:
    """Read the cached constituent lists, re-reading when the file changes.

    The mtime check means `python -m server.refresh_universe` takes effect on the
    next scan even while the server keeps running -- no restart, no stale lists.
    """
    global _cached, _cached_mtime
    target = path or UNIVERSE_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"No constituent cache at {target}. Run `python -m server.refresh_universe`."
        )
    mtime = target.stat().st_mtime
    if _cached is not None and not force and mtime == _cached_mtime:
        return _cached
    _cached = json.loads(target.read_text())
    _cached_mtime = mtime
    return _cached


def members(
    universe: dict[str, Any] | None = None, *, indices: tuple[str, ...] = ("sp500", "nasdaq100")
) -> list[Member]:
    """Members of the requested indices, with Schwab-formatted symbols."""
    data = universe if universe is not None else load_universe()
    out = []
    for entry in data.get("members", []):
        entry_indices = tuple(entry.get("indices", []))
        if not any(i in entry_indices for i in indices):
            continue
        out.append(
            Member(
                symbol=to_schwab_symbol(entry["symbol"]),
                name=entry.get("name", ""),
                sector=entry.get("sector", ""),
                indices=entry_indices,
            )
        )
    return out


def summary(universe: dict[str, Any] | None = None) -> dict[str, Any]:
    data = universe if universe is not None else load_universe()
    return {
        "fetched_at": data.get("fetched_at"),
        "counts": data.get("counts", {}),
        "total": len(data.get("members", [])),
    }
