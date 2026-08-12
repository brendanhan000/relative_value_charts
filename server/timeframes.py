"""Timeframe definitions and session-anchored resampling.

Schwab only serves minute bars at 1/5/10/15/30 and daily bars, so 1H and 4H are
built by aggregating 30-minute bars. Aggregation is anchored to the 09:30 ET cash
open rather than the wall clock, so hourly buckets run 09:30-10:30, 10:30-11:30, ...
and buckets never span two sessions.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd

MARKET_TZ = ZoneInfo("America/New_York")
SESSION_OPEN_MINUTES = 9 * 60 + 30  # 09:30 ET


@dataclass(frozen=True)
class Timeframe:
    key: str
    label: str
    period_type: str
    frequency_type: str
    frequency: int  # base bar size requested from Schwab
    bucket_minutes: int | None  # None => use base bars as-is
    default_lookback_days: int  # how much history is fetched
    default_view_days: int  # how much of it the x axis shows on arrival
    max_lookback_days: int

    @property
    def is_intraday(self) -> bool:
        return self.frequency_type == "minute"


#                                       freq  bucket  lookback  view   max
TIMEFRAMES: dict[str, Timeframe] = {
    tf.key: tf
    for tf in (
        Timeframe("5m", "5 min", "day", "minute", 5, None, 15, 5, 45),
        Timeframe("15m", "15 min", "day", "minute", 15, None, 30, 5, 90),
        Timeframe("1H", "1 hour", "day", "minute", 30, 60, 90, 15, 365),
        Timeframe("4H", "4 hour", "day", "minute", 30, 240, 365, 30, 730),
        Timeframe("1D", "1 day", "year", "daily", 1, None, 1825, 365, 7300),
    )
}

DEFAULT_TIMEFRAME = "1H"


def get_timeframe(key: str) -> Timeframe:
    try:
        return TIMEFRAMES[key]
    except KeyError:
        raise ValueError(
            f"Unknown timeframe {key!r}. Valid: {', '.join(TIMEFRAMES)}"
        ) from None


def candles_to_closes(candles: list[dict]) -> pd.Series:
    """Schwab candle dicts -> close series indexed by UTC timestamp."""
    if not candles:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC", name="ts"))

    df = pd.DataFrame(candles)
    df["ts"] = pd.to_datetime(df["datetime"], unit="ms", utc=True)
    closes = df.set_index("ts")["close"].sort_index().astype("float64")
    return closes[~closes.index.duplicated(keep="last")]


def _bucket_starts(index: pd.DatetimeIndex, bucket_minutes: int) -> pd.DatetimeIndex:
    """Map each bar timestamp to the start of its session-anchored bucket.

    The arithmetic runs in naive local time and is re-localized afterwards. Adding
    an elapsed Timedelta to a tz-aware midnight would land on 10:30 instead of
    09:30 on a spring-forward day, since the day is only 23 hours long.
    """
    naive = index.tz_convert(MARKET_TZ).tz_localize(None)
    session_open = naive.normalize() + pd.Timedelta(minutes=SESSION_OPEN_MINUTES)

    offset = (naive - session_open) // pd.Timedelta(minutes=1)
    # Floor toward -inf so pre-market bars bucket cleanly into negative slots.
    bucket_index = (offset // bucket_minutes) * bucket_minutes

    starts = session_open + pd.to_timedelta(bucket_index, unit="m")
    return pd.DatetimeIndex(starts).tz_localize(
        MARKET_TZ, ambiguous=True, nonexistent="shift_forward"
    ).tz_convert("UTC")


def resample_last(series: pd.Series, bucket_minutes: int | None) -> pd.Series:
    """Take the closing value of each larger session-anchored bucket."""
    if bucket_minutes is None or series.empty:
        return series

    out = series.groupby(_bucket_starts(series.index, bucket_minutes), sort=True).last()
    out.index.name = "ts"
    return out
