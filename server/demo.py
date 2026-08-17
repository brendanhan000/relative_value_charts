"""Synthetic data source for running the UI without Schwab credentials.

Enabled with RVC_DEMO=1. Bars follow a deterministic per-symbol random walk on a
regular-session calendar, so the app is fully exercisable offline -- but nothing
here is market data, and the banner in the UI says so.
"""

from __future__ import annotations

import hashlib
import math
import struct
from datetime import datetime, timedelta, timezone
from typing import Any

from .timeframes import MARKET_TZ

# symbol -> (price level, amplitude of the synthetic swing)
SEEDS = {
    "GLD": (315.0, 0.10),
    "SLV": (34.0, 0.16),
    "USO": (72.0, 0.20),
    "$SPX": (6350.0, 0.07),
    "SPY": (633.0, 0.07),
}


def _noise(symbol: str, bucket: int) -> float:
    """Deterministic value in [-1, 1] from a symbol and an integer bucket."""
    digest = hashlib.blake2b(f"{symbol}:{bucket}".encode(), digest_size=8).digest()
    return (struct.unpack("<Q", digest)[0] / 2**63) - 1.0


DRIFT_EPOCH = 1_750_000_000.0  # arbitrary fixed origin for the drift term

# Anchor for the synthetic "recent dislocation", fixed for the life of the process so
# every timeframe agrees. A steady drift cannot by itself push a z-score past 3 -- the
# mean and sigma travel with it (a pure ramp caps around 1.7 sigma). Real 3-sigma
# readings come from a sharp recent move against an otherwise stable ratio, so the demo
# gives a minority of symbols exactly that.
SHOCK_ANCHOR = datetime.now(timezone.utc).replace(
    hour=0, minute=0, second=0, microsecond=0
).timestamp()
SHOCK_WINDOW_DAYS = 12.0
SHOCK_CUTOFF = 0.88  # |noise| above this gets a shock -> about 12% of symbols
SHOCK_SIZE = 0.28


def _shock(symbol: str, epoch: float) -> float:
    """Multiplier for a recent, symbol-specific dislocation (1.0 for most symbols)."""
    amplitude = _noise(symbol, 5)
    if abs(amplitude) <= SHOCK_CUTOFF:
        return 1.0
    age_days = (SHOCK_ANCHOR - epoch) / 86400
    progress = max(0.0, min(1.0, (SHOCK_WINDOW_DAYS - age_days) / SHOCK_WINDOW_DAYS))
    return 1 + SHOCK_SIZE * amplitude * progress


def price_at(symbol: str, epoch: float) -> float:
    """Price as a pure function of time.

    Being time-based rather than a stepwise walk means every timeframe -- and the
    quote endpoint -- agree on the level, so ratios stay consistent everywhere.

    The per-symbol drift matters for the screener: without it every synthetic name
    oscillates inside the same band and no ratio can reach 3 sigma, so the tails the
    screener exists to find would never appear offline.
    """
    base, amp = SEEDS.get(symbol, (100.0, 0.12))
    t = epoch
    swing = (
        0.55 * math.sin(t / (86400 * 41.0) + _noise(symbol, 1) * math.pi)
        + 0.30 * math.sin(t / (86400 * 9.3) + _noise(symbol, 2) * math.pi)
        + 0.15 * math.sin(t / (3600 * 19.0) + _noise(symbol, 3) * math.pi)
    )
    # Smoothed hourly jitter so intraday bars are not perfectly sinusoidal.
    hour = int(t // 3600)
    frac = (t % 3600) / 3600
    jitter = _noise(symbol, hour) * (1 - frac) + _noise(symbol, hour + 1) * frac

    # Compounding per-symbol trend, +/-18%/yr, so single names disperse from the index.
    years = (t - DRIFT_EPOCH) / (365.25 * 86400)
    drift = (1 + 0.18 * _noise(symbol, 4)) ** years

    return base * drift * _shock(symbol, t) * (1 + amp * swing + 0.006 * jitter)


def _session_stamps(start: datetime, end: datetime, minutes: int | None) -> list[datetime]:
    """Regular-session bar timestamps between two instants (weekdays only)."""
    stamps: list[datetime] = []
    day = start.astimezone(MARKET_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    last = end.astimezone(MARKET_TZ)

    while day <= last:
        if day.weekday() < 5:
            if minutes is None:
                # Match Schwab's convention: daily bars carry 00:00 UTC of the
                # session date (the UI labels daily points from the UTC date).
                stamps.append(day.astimezone(timezone.utc).replace(hour=0, minute=0))
            else:
                t = day.replace(hour=9, minute=30)
                close = day.replace(hour=16, minute=0)
                while t < close:
                    stamps.append(t)
                    t += timedelta(minutes=minutes)
        day += timedelta(days=1)

    return [s for s in stamps if start.astimezone(MARKET_TZ) <= s <= last]


class DemoClient:
    """Duck-typed stand-in for SchwabClient."""

    is_demo = True

    def __init__(self) -> None:
        self.tokens = _DemoTokens()

    async def aclose(self) -> None:  # pragma: no cover - nothing to close
        return None

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
        start = datetime.fromtimestamp((start_ms or 0) / 1000, timezone.utc)
        end = datetime.fromtimestamp((end_ms or 0) / 1000, timezone.utc)
        minutes = frequency if frequency_type == "minute" else None

        stamps = _session_stamps(start, end, minutes)
        if not stamps:
            return []

        span = (minutes or 390) * 60
        candles = []
        for ts in stamps:
            t0 = ts.timestamp()
            open_ = price_at(symbol, t0)
            close = price_at(symbol, t0 + span)
            # Sample the interior so the wick reflects the path, not a random draw.
            mids = [price_at(symbol, t0 + span * f) for f in (0.25, 0.5, 0.75)]
            values = [open_, close, *mids]
            candles.append(
                {
                    "datetime": int(t0 * 1000),
                    "open": round(open_, 4),
                    "high": round(max(values), 4),
                    "low": round(min(values), 4),
                    "close": round(close, 4),
                    "volume": int(200_000 + abs(_noise(symbol, int(t0))) * 700_000),
                }
            )
        return candles

    async def quotes(self, symbols: list[str]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).timestamp()
        out: dict[str, Any] = {}
        for symbol in symbols:
            price = round(price_at(symbol, now), 4)
            out[symbol] = {
                "symbol": symbol,
                "quote": {"lastPrice": price, "bidPrice": price - 0.01, "askPrice": price + 0.01},
            }
        return out


class _DemoTokens:
    def status(self) -> dict[str, Any]:
        return {
            "has_token": True,
            "access_valid": True,
            "refresh_valid": True,
            "refresh_expires_at": None,
            "updated_at": None,
        }
