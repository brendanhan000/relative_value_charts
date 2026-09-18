"""Pair definitions, ratio-series construction, and mean/standard-deviation stats."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from .config import SETTINGS
from .schwab import SchwabAPIError, SchwabClient
from .timeframes import Timeframe, candles_to_closes, get_timeframe, resample_last


@dataclass(frozen=True)
class Leg:
    symbol: str
    name: str
    # Used when `symbol` returns no history (e.g. index minute data entitlements).
    fallback_symbol: str | None = None
    fallback_note: str = ""


@dataclass(frozen=True)
class Pair:
    key: str
    numerator: Leg
    denominator: Leg
    description: str
    unit_hint: str = ""
    decimals: int | None = None  # None => derive from the ratio's magnitude

    @property
    def label(self) -> str:
        return f"{self.numerator.symbol}/{self.denominator.symbol}"


GLD = Leg("GLD", "SPDR Gold Shares")
SLV = Leg("SLV", "iShares Silver Trust")
USO = Leg("USO", "United States Oil Fund")
SPX = Leg(
    "$SPX",
    "S&P 500 Index",
    fallback_symbol="SPY",
    fallback_note="$SPX history unavailable on this account; using SPY (≈1/10 of SPX).",
)

PAIRS: dict[str, Pair] = {
    p.key: p
    for p in (
        Pair(
            "GLD_USO",
            GLD,
            USO,
            "Gold vs. crude oil — how many USO units buy one GLD unit.",
            unit_hint="USO units per GLD unit",
            decimals=3,
        ),
        Pair(
            "SPX_USO",
            SPX,
            USO,
            "Equities vs. crude oil — S&P 500 index points per USO unit.",
            unit_hint="USO units per index unit",
            decimals=2,
        ),
        Pair(
            "GLD_SLV",
            GLD,
            SLV,
            "The gold/silver ratio, expressed through the ETFs.",
            unit_hint="SLV units per GLD unit",
            decimals=4,
        ),
    )
}

DEFAULT_PAIR = "GLD_USO"

# ------------------------------------------------------------ custom pairs

# Schwab symbols are upper case and may carry a `$` index prefix, a `/` futures
# prefix, or dots (BRK.B). Anything outside this set is a typo, not a symbol.
SYMBOL_RE = re.compile(r"^[A-Z0-9$/][A-Z0-9./$_-]{0,23}$")
MAX_SYMBOL_LEN = 24

# Symbols that are commonly unavailable and have a workable stand-in, applied to
# hand-typed legs the same way the built-in pairs get it.
KNOWN_FALLBACKS: dict[str, tuple[str, str]] = {
    "$SPX": ("SPY", "$SPX history unavailable on this account; using SPY (≈1/10 of SPX)."),
    "$NDX": ("QQQ", "$NDX history unavailable on this account; using QQQ."),
    "$DJI": ("DIA", "$DJI history unavailable on this account; using DIA."),
}


def normalize_symbol(raw: str) -> str:
    """Upper-case and validate a hand-entered ticker."""
    symbol = (raw or "").strip().upper()
    if not symbol:
        raise ValueError("Enter a symbol for both legs.")
    if len(symbol) > MAX_SYMBOL_LEN:
        raise ValueError(f"'{symbol[:MAX_SYMBOL_LEN]}…' is too long to be a symbol.")
    if not SYMBOL_RE.match(symbol):
        raise ValueError(
            f"'{symbol}' is not a valid symbol — use letters, digits and . / $ _ - only."
        )
    return symbol


def _custom_leg(symbol: str) -> Leg:
    fallback = KNOWN_FALLBACKS.get(symbol)
    return Leg(
        symbol,
        symbol,
        fallback_symbol=fallback[0] if fallback else None,
        fallback_note=fallback[1] if fallback else "",
    )


def custom_pair(num: str, den: str) -> Pair:
    """Build an ad-hoc pair from two hand-entered symbols."""
    numerator = normalize_symbol(num)
    denominator = normalize_symbol(den)
    if numerator == denominator:
        raise ValueError(
            f"Both legs are {numerator}; the ratio would be a flat 1.0. Use two symbols."
        )
    return Pair(
        key=f"custom:{numerator}/{denominator}",
        numerator=_custom_leg(numerator),
        denominator=_custom_leg(denominator),
        description=f"Custom ratio — {numerator} priced in {denominator}.",
        unit_hint=f"{denominator} units per {numerator} unit",
        decimals=None,
    )


def resolve_pair(pair_key: str, num: str | None, den: str | None) -> Pair:
    """A built-in pair, or a custom one when either symbol field is supplied."""
    if num or den:
        return custom_pair(num or "", den or "")
    if pair_key not in PAIRS:
        raise KeyError(pair_key)
    return PAIRS[pair_key]


def decimals_for(value: float) -> int:
    """Enough precision to see the ratio move, without noise digits."""
    magnitude = abs(value)
    if magnitude >= 100:
        return 2
    if magnitude >= 10:
        return 3
    if magnitude >= 1:
        return 4
    if magnitude >= 0.01:
        return 5
    return 6

STAT_WINDOWS: dict[str, int | None] = {
    "full": None,
    "500": 500,
    "200": 200,
    "100": 100,
}


# --------------------------------------------------------------------- fetch


@dataclass
class _CacheEntry:
    fetched_at: float
    closes: pd.Series
    symbol_used: str
    note: str


_cache: dict[tuple[str, str, int, int], _CacheEntry] = {}
_cache_lock = asyncio.Lock()


def window_ms(days: int) -> tuple[int, int]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


async def fetch_symbol_closes(
    client: SchwabClient,
    symbol: str,
    tf: Timeframe,
    days: int,
    *,
    extended: bool = False,
) -> pd.Series:
    """One symbol's base-resolution closes. No caching, no fallback."""
    start_ms, end_ms = window_ms(days)
    candles = await client.price_history(
        symbol,
        period_type=tf.period_type,
        frequency_type=tf.frequency_type,
        frequency=tf.frequency,
        start_ms=start_ms,
        end_ms=end_ms,
        extended_hours=extended,
    )
    return candles_to_closes(candles)


async def _fetch_leg(
    client: SchwabClient, leg: Leg, tf: Timeframe, days: int, extended: bool
) -> tuple[pd.Series, str, str]:
    """Fetch one leg's base closes. Returns (closes, symbol_used, note)."""
    key = (leg.symbol, tf.key, days, int(extended))
    now = time.time()

    async with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit.fetched_at < SETTINGS.history_cache_seconds:
            # The note rides along so a fallback banner does not flicker off on refresh.
            return hit.closes, hit.symbol_used, hit.note

    note = ""
    symbol_used = leg.symbol

    async def pull(symbol: str) -> pd.Series:
        return await fetch_symbol_closes(client, symbol, tf, days, extended=extended)

    try:
        closes = await pull(leg.symbol)
    except SchwabAPIError:
        if not leg.fallback_symbol:
            raise
        closes = pd.Series(dtype="float64")

    if closes.empty and leg.fallback_symbol:
        closes = await pull(leg.fallback_symbol)
        if not closes.empty:
            symbol_used = leg.fallback_symbol
            note = leg.fallback_note

    async with _cache_lock:
        _cache[key] = _CacheEntry(now, closes, symbol_used, note)

    return closes, symbol_used, note


# --------------------------------------------------------------------- ratio


def build_ratio_series(num: pd.Series, den: pd.Series, tf: Timeframe) -> pd.Series:
    """Ratio at base resolution, then aggregated to the timeframe.

    Only timestamps where both legs printed are used -- a missing print on one leg
    drops that point rather than pairing a fresh price against a stale one.
    """
    idx = num.index.intersection(den.index)
    if len(idx) == 0:
        return pd.Series(dtype="float64")

    with np.errstate(divide="ignore", invalid="ignore"):
        raw = num.loc[idx] / den.loc[idx]

    raw = raw.replace([np.inf, -np.inf], np.nan).dropna()
    if raw.empty:
        return raw

    return resample_last(raw, tf.bucket_minutes).dropna()


def compute_stats(closes: pd.Series, window: int | None) -> dict[str, Any]:
    """Mean and 1/2/3-sigma levels over the stats window (a flat horizontal band).

    The mean/σ lines describe "where the ratio usually sits" and are computed over
    the full window INCLUDING the latest point. The z-score answers a different
    question -- "how unusual is THIS print" -- so it is OUT-OF-SAMPLE: the last
    point is scored against the mean/σ of the window EXCLUDING itself. Including
    the scored point lets a shock pull the mean toward itself and inflate σ, which
    damps z (measured: a true 7.1σ move reads 5.7σ in-sample at a 100-bar window).
    """
    sample = closes if window is None else closes.tail(window)
    sample = sample.dropna()
    n = int(sample.size)

    if n < 2:
        return {"n": n, "mean": None, "std": None, "bands": [], "last": None, "z": None}

    mean = float(sample.mean())
    std = float(sample.std(ddof=1))
    last = float(closes.iloc[-1])

    bands = []
    for k in (1, 2, 3):
        bands.append({"k": k, "upper": mean + k * std, "lower": mean - k * std})

    # Out-of-sample z: reference stats over the window minus the last point.
    ref = sample.iloc[:-1]
    z = None
    ref_mean = ref_std = None
    if ref.size >= 2:
        ref_mean = float(ref.mean())
        ref_std = float(ref.std(ddof=1))
        if ref_std > 0:
            z = (last - ref_mean) / ref_std

    return {
        "n": n,
        "mean": mean,
        "std": std,
        "bands": bands,
        "last": last,
        "z": z,
        # The reference the z was computed against (window minus the last point).
        "z_ref_mean": ref_mean,
        "z_ref_std": ref_std,
        "min": float(sample.min()),
        "max": float(sample.max()),
        "start": sample.index[0].isoformat(),
        "end": sample.index[-1].isoformat(),
    }


async def build_series(
    client: SchwabClient,
    pair: Pair,
    timeframe_key: str,
    *,
    lookback_days: int | None = None,
    stat_window: str = "full",
    extended_hours: bool = False,
) -> dict[str, Any]:
    tf = get_timeframe(timeframe_key)
    days = min(lookback_days or tf.default_lookback_days, tf.max_lookback_days)

    (num_px, num_sym, num_note), (den_px, den_sym, den_note) = await asyncio.gather(
        _fetch_leg(client, pair.numerator, tf, days, extended_hours),
        _fetch_leg(client, pair.denominator, tf, days, extended_hours),
    )

    notes = [n for n in (num_note, den_note) if n]
    if num_px.empty or den_px.empty:
        missing = []
        for leg, closes in ((pair.numerator, num_px), (pair.denominator, den_px)):
            if not closes.empty:
                continue
            # Name the fallback too, so it is clear that it was tried and also failed.
            missing.append(
                f"{leg.symbol} (or {leg.fallback_symbol})"
                if leg.fallback_symbol
                else leg.symbol
            )
        raise ValueError(
            f"No {tf.label} history returned for {', '.join(missing)} "
            f"over the last {days} days."
        )

    ratio = build_ratio_series(num_px, den_px, tf)
    if ratio.empty:
        raise ValueError(
            f"{num_sym} and {den_sym} have no overlapping {tf.label} bars in this window."
        )

    window = STAT_WINDOWS.get(stat_window, None)
    stats = compute_stats(ratio, window)
    decimals = pair.decimals if pair.decimals is not None else decimals_for(
        float(ratio.median())
    )

    points = [
        {"time": int(ts.timestamp()), "value": round(float(v), 6)}
        for ts, v in ratio.items()
    ]

    return {
        "pair": {
            "key": pair.key,
            "label": f"{num_sym}/{den_sym}",
            "description": pair.description,
            "unit_hint": pair.unit_hint,
            "decimals": decimals,
            "custom": pair.key.startswith("custom:"),
            "numerator": num_sym,
            "denominator": den_sym,
        },
        "timeframe": {
            "key": tf.key,
            "label": tf.label,
            "lookback_days": days,
            # The x-axis window applied on arrival; the rest is there to pan into.
            "view_days": min(tf.default_view_days, days),
            "base_frequency": (
                f"{tf.frequency}m" if tf.is_intraday else f"{tf.frequency}d"
            ),
            "aggregated": tf.bucket_minutes is not None,
        },
        "stats": stats,
        "stat_window": stat_window,
        "points": points,
        "notes": notes,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------------------ realtime


def _quote_price(entry: dict[str, Any]) -> float | None:
    """Best available live price from a Schwab quote payload."""
    quote = entry.get("quote", {}) or {}
    for field_name in ("lastPrice", "mark", "closePrice"):
        value = quote.get(field_name)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    # Fall back to the midpoint if there is no last trade (common for indices pre-open).
    bid, ask = quote.get("bidPrice"), quote.get("askPrice")
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 < ask:
        return (bid + ask) / 2
    return None


async def live_ratio(client: SchwabClient, pair: Pair) -> dict[str, Any]:
    """Current ratio from the quotes endpoint, for updating the in-progress bar."""
    wanted = {"numerator": pair.numerator, "denominator": pair.denominator}
    symbols: list[str] = []
    resolved: dict[str, str] = {}
    for role, leg in wanted.items():
        resolved[role] = leg.symbol
        symbols.append(leg.symbol)

    data = await client.quotes(symbols)

    prices: dict[str, float] = {}
    for role, leg in wanted.items():
        entry = data.get(leg.symbol)
        price = _quote_price(entry) if entry else None
        if price is None and leg.fallback_symbol:
            fb = await client.quotes([leg.fallback_symbol])
            entry = fb.get(leg.fallback_symbol)
            price = _quote_price(entry) if entry else None
            if price is not None:
                resolved[role] = leg.fallback_symbol
        if price is None:
            return {"ok": False, "reason": f"No live quote for {leg.symbol}."}
        prices[role] = price

    if prices["denominator"] == 0:
        return {"ok": False, "reason": "Denominator quote is zero."}

    return {
        "ok": True,
        "ratio": prices["numerator"] / prices["denominator"],
        "numerator": {"symbol": resolved["numerator"], "price": prices["numerator"]},
        "denominator": {"symbol": resolved["denominator"], "price": prices["denominator"]},
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
