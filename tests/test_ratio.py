"""Offline checks for the maths: no Schwab credentials required.

Run with: python -m tests.test_ratio
"""

from __future__ import annotations

import asyncio
import sys

import numpy as np
import pandas as pd

from server.ratio import (
    PAIRS,
    _cache,
    build_ratio_series,
    build_series,
    compute_stats,
    custom_pair,
    decimals_for,
    normalize_symbol,
    resolve_pair,
)
from server.schwab import SchwabAPIError
from server.timeframes import (
    MARKET_TZ,
    _bucket_starts,
    TIMEFRAMES,
    candles_to_closes,
    get_timeframe,
    resample_last,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def synthetic_candles(start_et: str, bars: int, minutes: int, base: float, drift: float):
    """Regular-session bars starting at `start_et`, skipping to the next session."""
    ts = pd.Timestamp(start_et, tz=MARKET_TZ)
    session_end = ts.normalize() + pd.Timedelta(hours=16)
    out = []
    price = base
    for i in range(bars):
        if ts >= session_end:
            ts = (ts.normalize() + pd.Timedelta(days=1)).replace(hour=9, minute=30)
            session_end = ts.normalize() + pd.Timedelta(hours=16)
        price += drift
        out.append(
            {
                "datetime": int(ts.timestamp() * 1000),
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price + 0.1,
                "volume": 1000 + i,
            }
        )
        ts = ts + pd.Timedelta(minutes=minutes)
    return out


def test_candles_to_closes() -> None:
    s = candles_to_closes(synthetic_candles("2026-08-10 09:30", 5, 30, 100, 1))
    check("candles_to_closes builds a UTC index", str(s.index.tz) == "UTC")
    check("candles_to_closes keeps 5 points", len(s) == 5)
    check("values are the candle closes", s.iloc[0] == 101.1, f"{s.iloc[0]}")
    check("empty candle list -> empty series", candles_to_closes([]).empty)

    dupes = synthetic_candles("2026-08-10 09:30", 2, 30, 100, 1)
    dupes[1]["datetime"] = dupes[0]["datetime"]
    dupes[1]["close"] = 999.0
    check(
        "duplicate timestamps keep the last print",
        candles_to_closes(dupes).tolist() == [999.0],
    )


def test_session_anchored_resample() -> None:
    # 13 half-hour bars from 09:30 -> covers 09:30..15:30 of one session.
    s = candles_to_closes(synthetic_candles("2026-08-10 09:30", 13, 30, 100, 1))
    hourly = resample_last(s, 60)

    local = hourly.index.tz_convert(MARKET_TZ)
    starts = {f"{t.hour:02d}:{t.minute:02d}" for t in local}
    check(
        "hourly buckets anchor to 09:30 ET",
        starts == {"09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"},
        f"got {sorted(starts)}",
    )
    check(
        "bucket value = last base value in the bucket",
        hourly.iloc[0] == s.iloc[1],
        f"{hourly.iloc[0]} vs {s.iloc[1]}",
    )
    check("last bucket carries the final print", hourly.iloc[-1] == s.iloc[-1])
    check("no bucketing when bucket_minutes is None", resample_last(s, None).equals(s))


def test_buckets_never_span_sessions() -> None:
    # 20 half-hour bars rolls past one session close into the next morning.
    s = candles_to_closes(synthetic_candles("2026-08-10 14:30", 20, 30, 100, 1))
    four_hour = resample_last(s, 240)
    local = four_hour.index.tz_convert(MARKET_TZ)
    dates = sorted({str(t.date()) for t in local})
    check("4H points span multiple sessions", len(dates) >= 2, f"dates={dates}")

    counts = pd.Series(1, index=local).groupby(local.date).count()
    check(
        "no session gets a bucket spanning the overnight gap",
        all(c <= 4 for c in counts),
        f"per-session bucket counts={counts.to_dict()}",
    )


def test_ratio_series() -> None:
    tf = get_timeframe("1H")
    num = candles_to_closes(synthetic_candles("2026-08-10 09:30", 13, 30, 300, 0.5))
    den = candles_to_closes(synthetic_candles("2026-08-10 09:30", 13, 30, 75, 0.1))
    ratio = build_ratio_series(num, den, tf)

    check("ratio series is non-empty", not ratio.empty, f"{len(ratio)} points")

    expected = num.iloc[1] / den.iloc[1]
    check(
        "first 1H value = ratio of the bucket's last 30m closes",
        abs(ratio.iloc[0] - expected) < 1e-12,
        f"{ratio.iloc[0]:.9f} vs {expected:.9f}",
    )

    # Ratio-then-aggregate must equal aggregating the ratio, not ratio of aggregates.
    base = build_ratio_series(num, den, get_timeframe("15m"))  # no bucketing
    check(
        "1H values are a subset of the base-resolution ratio",
        set(ratio.round(12)).issubset(set(base.round(12))),
    )
    check("aggregation reduces the point count", len(ratio) < len(base))


def test_misaligned_legs_intersect() -> None:
    tf = get_timeframe("15m")
    num = candles_to_closes(synthetic_candles("2026-08-10 09:30", 10, 15, 300, 0.5))
    den_candles = synthetic_candles("2026-08-10 09:30", 10, 15, 75, 0.1)
    dropped = den_candles.pop(3)  # denominator missed a print
    den = candles_to_closes(den_candles)

    ratio = build_ratio_series(num, den, tf)
    check("misaligned point is dropped, not forward-filled", len(ratio) == 9, f"{len(ratio)}")
    missing = pd.Timestamp(dropped["datetime"], unit="ms", tz="UTC")
    check("the dropped timestamp is absent from the ratio", missing not in ratio.index)


def test_stats() -> None:
    idx = pd.date_range("2026-08-01", periods=1000, freq="h", tz="UTC")
    values = pd.Series(np.linspace(0, 1, 1000) * 0 + 10.0, index=idx)
    values.iloc[::2] += 1.0  # exact mean 10.5, exact sample sd close to 0.5

    stats = compute_stats(values, None)
    check("mean is exact", abs(stats["mean"] - 10.5) < 1e-9, f"{stats['mean']}")
    check("sigma is sample sd (ddof=1)", abs(stats["std"] - values.std(ddof=1)) < 1e-12)
    check("three bands returned", [b["k"] for b in stats["bands"]] == [1, 2, 3])
    band3 = stats["bands"][2]
    check(
        "3σ band = mean ± 3·sigma",
        abs(band3["upper"] - (stats["mean"] + 3 * stats["std"])) < 1e-12
        and abs(band3["lower"] - (stats["mean"] - 3 * stats["std"])) < 1e-12,
    )
    check("n counts the window", stats["n"] == 1000)

    windowed = compute_stats(values, 100)
    check("stat window truncates n", windowed["n"] == 100)
    # z is OUT-OF-SAMPLE: the last point scored against the window minus itself.
    ref = values.iloc[-100:-1]  # the 100-bar window, minus the scored point
    want_z = (values.iloc[-1] - ref.mean()) / ref.std(ddof=1)
    check(
        "z-score is out-of-sample (last point excluded from its own reference)",
        abs(windowed["z"] - want_z) < 1e-12,
        f"got {windowed['z']}, want {want_z}",
    )
    check(
        "reference stats are exposed",
        abs(windowed["z_ref_mean"] - ref.mean()) < 1e-12
        and abs(windowed["z_ref_std"] - ref.std(ddof=1)) < 1e-12,
    )

    check("too-few-points is handled", compute_stats(values.head(1), None)["mean"] is None)


def test_z_is_out_of_sample() -> None:
    """A shock must not pull its own reference toward itself (the damping bug)."""
    rng = np.random.default_rng(1)
    n = 100
    vals = pd.Series(rng.normal(10, 0.5, n), index=pd.date_range("2026-01-01", periods=n, freq="B", tz="UTC"))
    vals.iloc[-1] = 13.0  # ~6-sigma shock vs the first 99

    stats = compute_stats(vals, 100)
    base = vals.iloc[:-1]
    z_oos = (vals.iloc[-1] - base.mean()) / base.std(ddof=1)
    z_in = (vals.iloc[-1] - vals.mean()) / vals.std(ddof=1)
    check("out-of-sample z exceeds the damped in-sample z", stats["z"] > z_in,
          f"oos={stats['z']:.2f} in={z_in:.2f}")
    check("z matches the true out-of-sample value", abs(stats["z"] - z_oos) < 1e-9,
          f"{stats['z']:.4f} vs {z_oos:.4f}")
    # Mean/σ lines are unchanged: still the full window.
    check("mean line still uses the full window", abs(stats["mean"] - vals.mean()) < 1e-12)
    check("sigma line still uses the full window", abs(stats["std"] - vals.std(ddof=1)) < 1e-12)


def test_mean_varies_by_timeframe() -> None:
    """The horizontal mean should differ across timeframes when lookbacks differ."""
    num = candles_to_closes(synthetic_candles("2026-08-03 09:30", 400, 30, 300, 0.2))
    den = candles_to_closes(synthetic_candles("2026-08-03 09:30", 400, 30, 75, 0.02))

    means = {}
    for key in ("1H", "4H"):
        r = build_ratio_series(num, den, get_timeframe(key))
        means[key] = compute_stats(r, 100)["mean"]

    check(
        "mean over last 100 bars differs between 1H and 4H",
        means["1H"] is not None and means["4H"] is not None and means["1H"] != means["4H"],
        f"{means}",
    )


def test_dst_boundary() -> None:
    """Buckets stay 09:30-anchored across both DST transitions.

    US DST in 2026 starts Sunday 2026-03-08 and ends Sunday 2026-11-01. The
    synthetic feed emits bars every calendar day so the transition day is covered.
    """
    for label, start in (("spring forward", "2026-03-06 09:30"), ("fall back", "2026-10-30 09:30")):
        s = candles_to_closes(synthetic_candles(start, 5 * 13, 30, 100, 0.1))
        crossed = len({t.utcoffset() for t in s.index.tz_convert(MARKET_TZ)}) > 1
        check(f"{label}: test data actually crosses the transition", crossed)

        for bucket, name in ((60, "1H"), (240, "4H")):
            out = resample_last(s, bucket)
            local = out.index.tz_convert(MARKET_TZ)
            offsets = {(t.hour * 60 + t.minute) - (9 * 60 + 30) for t in local}
            check(
                f"{label}: every {name} bucket sits a whole multiple of {bucket}m from 09:30 ET",
                all(o % bucket == 0 for o in offsets),
                f"offsets={sorted(offsets)}",
            )
            check(
                f"{label}: every {name} value comes from the source series",
                set(out).issubset(set(s)),
            )

            # The invariant that actually pins the anchoring down: a point must land
            # in a bucket that starts at or before it, and no more than one bucket
            # earlier. Alignment-modulo alone passes even when every point on a DST
            # day is pushed into the following bucket.
            starts = _bucket_starts(s.index, bucket)
            delta = (s.index - starts) / pd.Timedelta(minutes=1)
            check(
                f"{label}: every point falls inside its own {name} bucket",
                bool(((delta >= 0) & (delta < bucket)).all()),
                f"min={delta.min():.0f}m max={delta.max():.0f}m (want 0..{bucket})",
            )


def test_symbol_validation() -> None:
    for raw, want in (
        ("gld", "GLD"), (" spy ", "SPY"), ("$SPX", "$SPX"),
        ("brk.b", "BRK.B"), ("/es", "/ES"), ("x", "X"),
        # Surrounding whitespace is a paste artifact, not a typo -- strip it.
        ("GLD\n", "GLD"), ("\tSPY ", "SPY"),
    ):
        check(f"normalize_symbol({raw!r}) -> {want}", normalize_symbol(raw) == want,
              f"got {normalize_symbol(raw)!r}")

    for bad, why in (
        ("", "empty"), ("   ", "blank"), ("GL D", "space"), ("GLD;DROP", "punctuation"),
        ("A" * 25, "too long"), ("<script>", "markup"), ("GL\nD", "interior newline"),
        ("GLD SPY", "two symbols in one field"),
    ):
        try:
            normalize_symbol(bad)
            check(f"rejects {why}", False, f"{bad!r} was accepted")
        except ValueError:
            check(f"rejects {why}", True)


def test_custom_pair() -> None:
    pair = custom_pair(" qqq ", "spy")
    check("custom pair upper-cases both legs", pair.label == "QQQ/SPY", pair.label)
    check("custom key is namespaced", pair.key == "custom:QQQ/SPY", pair.key)
    check("unit hint reads correctly", pair.unit_hint == "SPY units per QQQ unit")
    check("decimals are derived, not fixed", pair.decimals is None)

    for same in ("GLD", "gld"):
        try:
            custom_pair("GLD", same)
            check(f"rejects identical legs ({same})", False, "accepted")
        except ValueError:
            check(f"rejects identical legs ({same})", True)

    # A hand-typed index symbol should inherit the same fallback the presets get.
    spx = custom_pair("$SPX", "GLD")
    check("hand-typed $SPX carries its fallback", spx.numerator.fallback_symbol == "SPY")
    check("plain tickers get no fallback", spx.denominator.fallback_symbol is None)


def test_resolve_pair() -> None:
    check("preset key resolves", resolve_pair("GLD_USO", None, None) is PAIRS["GLD_USO"])
    check("num+den wins over pair", resolve_pair("GLD_USO", "TLT", "GLD").label == "TLT/GLD")

    for num, den in (("TLT", None), (None, "GLD"), ("TLT", "")):
        try:
            resolve_pair("GLD_USO", num, den)
            check(f"half a custom pair ({num},{den}) is rejected", False, "accepted")
        except ValueError:
            check(f"half a custom pair ({num},{den}) is rejected", True)

    try:
        resolve_pair("NOPE", None, None)
        check("unknown preset raises KeyError", False, "accepted")
    except KeyError:
        check("unknown preset raises KeyError", True)


def test_decimals_for() -> None:
    for value, want in ((6300.0, 2), (88.9, 3), (4.4, 4), (0.5, 5), (0.004, 6)):
        check(f"decimals_for({value}) == {want}", decimals_for(value) == want,
              f"got {decimals_for(value)}")
    check("negatives use magnitude", decimals_for(-88.9) == 3)


def test_custom_pair_series() -> None:
    """A custom pair produces a normal payload with derived precision."""
    _cache.clear()
    stub = _StubClient(blank=set())
    payload = asyncio.run(build_series(stub, custom_pair("SPY", "$SPX"), "1H"))
    check("custom series is flagged", payload["pair"]["custom"] is True)
    check("label uses the typed symbols", payload["pair"]["label"] == "SPY/$SPX",
          payload["pair"]["label"])
    # SPY/$SPX is about 0.1, so precision should go finer than the preset default.
    check("precision derived from magnitude", payload["pair"]["decimals"] == 5,
          f"decimals={payload['pair']['decimals']} on a ratio near "
          f"{payload['stats']['mean']:.4f}")
    check("stats computed as usual", payload["stats"]["mean"] is not None)

    _cache.clear()
    preset = asyncio.run(build_series(_StubClient(blank=set()), PAIRS["GLD_SLV"], "1H"))
    check("preset keeps its fixed precision", preset["pair"]["decimals"] == 4)
    check("preset is not flagged custom", preset["pair"]["custom"] is False)
    _cache.clear()


def test_view_windows() -> None:
    """The default x-axis window must fit inside the history that gets loaded."""
    expected = {"5m": 5, "15m": 5, "1H": 15, "4H": 30, "1D": 365}
    for key, want in expected.items():
        tf = TIMEFRAMES[key]
        check(f"{key}: default view window is {want} days", tf.default_view_days == want,
              f"got {tf.default_view_days}")
        check(
            f"{key}: view window fits inside the default lookback",
            tf.default_view_days <= tf.default_lookback_days,
            f"view={tf.default_view_days} lookback={tf.default_lookback_days}",
        )
        check(
            f"{key}: there is history to zoom out into",
            tf.default_lookback_days > tf.default_view_days,
        )

    check("every timeframe declares a view window", set(expected) == set(TIMEFRAMES))


def test_view_days_clamped_to_lookback() -> None:
    """A hand-set lookback shorter than the view window clamps the window."""
    _cache.clear()
    stub = _StubClient(blank=set())
    payload = asyncio.run(
        build_series(stub, PAIRS["GLD_USO"], "1D", lookback_days=10)
    )
    check(
        "view_days never exceeds lookback_days",
        payload["timeframe"]["view_days"] <= payload["timeframe"]["lookback_days"],
        f"view={payload['timeframe']['view_days']} lookback={payload['timeframe']['lookback_days']}",
    )
    _cache.clear()


class _StubClient:
    """Returns bars for everything except the symbols listed in `blank`."""

    def __init__(self, blank: set[str], raise_for: set[str] | None = None):
        self.blank = blank
        self.raise_for = raise_for or set()
        self.asked: list[str] = []

    async def price_history(self, symbol: str, **_: object):
        self.asked.append(symbol)
        if symbol in self.raise_for:
            raise SchwabAPIError(400, f"{symbol} not entitled")
        if symbol in self.blank:
            return []
        base = {"SPY": 630.0, "$SPX": 6300.0}.get(symbol, 72.0)
        return synthetic_candles("2026-06-01 09:30", 300, 30, base, 0.05)


def test_fallback_symbol() -> None:
    """A leg with no history falls back to its alternate and says so."""
    for label, stub in (
        ("empty response", _StubClient(blank={"$SPX"})),
        ("API error", _StubClient(blank=set(), raise_for={"$SPX"})),
    ):
        _cache.clear()
        payload = asyncio.run(build_series(stub, PAIRS["SPX_USO"], "1H"))
        check(f"{label}: falls back to SPY", payload["pair"]["numerator"] == "SPY",
              f"asked={stub.asked}")
        check(f"{label}: label reflects the symbol actually used",
              payload["pair"]["label"] == "SPY/USO")
        check(f"{label}: a note explains the substitution",
              any("SPY" in n for n in payload["notes"]), f"{payload['notes']}")
        check(f"{label}: stats still computed", payload["stats"]["mean"] is not None)

    _cache.clear()
    stub = _StubClient(blank={"$SPX", "SPY"})
    try:
        asyncio.run(build_series(stub, PAIRS["SPX_USO"], "1H"))
        check("both symbols blank raises a clear error", False, "no exception raised")
    except ValueError as exc:
        check("both symbols blank raises a clear error", "SPY" in str(exc), str(exc))
    _cache.clear()


def test_no_overlap_errors() -> None:
    """Legs that never print at the same time produce an explicit error."""
    class Disjoint:
        async def price_history(self, symbol: str, **_: object):
            start = "2026-06-01 09:30" if symbol == "GLD" else "2026-07-01 09:30"
            return synthetic_candles(start, 60, 30, 100.0, 0.05)

    _cache.clear()
    try:
        asyncio.run(build_series(Disjoint(), PAIRS["GLD_USO"], "1H"))
        check("non-overlapping legs raise", False, "no exception raised")
    except ValueError as exc:
        check("non-overlapping legs raise", "overlapping" in str(exc), str(exc))
    _cache.clear()


def test_demo_daily_stamp_convention() -> None:
    """Demo daily bars must carry 00:00 UTC of the session date, like Schwab's.

    The UI labels 1D points from the UTC date of the timestamp; a demo stamp at
    any other hour would shift the label (and diverge from real data).
    """
    from datetime import datetime, timezone

    from server.demo import DemoClient

    async def pull():
        client = DemoClient()
        start = int(datetime(2026, 8, 3, tzinfo=timezone.utc).timestamp() * 1000)
        end = int(datetime(2026, 8, 11, tzinfo=timezone.utc).timestamp() * 1000)
        return await client.price_history(
            "GLD",
            period_type="year",
            frequency_type="daily",
            frequency=1,
            start_ms=start,
            end_ms=end,
        )

    candles = asyncio.run(pull())
    check("demo returns daily candles", len(candles) > 0)
    stamps = [datetime.fromtimestamp(c["datetime"] / 1000, timezone.utc) for c in candles]
    check(
        "every demo daily stamp is 00:00 UTC",
        all(s.hour == 0 and s.minute == 0 for s in stamps),
        f"got {[s.isoformat() for s in stamps[:3]]}",
    )
    check(
        "demo daily stamps are weekdays",
        all(s.weekday() < 5 for s in stamps),
    )


def test_hub_auth_and_availability_errors() -> None:
    """Hub 401 / hub down are auth problems (UI shows guidance), other statuses are upstream."""
    import httpx

    from server.schwab import SchwabAPIError, SchwabAuthError, SchwabClient

    def client(handler):
        c = SchwabClient("http://hub")
        c._client = httpx.AsyncClient(base_url="http://hub", transport=httpx.MockTransport(handler))
        return c

    async def outcome(handler):
        try:
            await client(handler)._get("/pricehistory", {})
        except Exception as exc:  # noqa: BLE001
            return type(exc)
        return None

    def hub_401(_req):
        return httpx.Response(401, json={"error": "schwab_auth", "detail": "expired", "hint": "run login"})

    def down(_req):
        raise httpx.ConnectError("refused")

    def upstream_400(_req):
        return httpx.Response(400, text="not entitled")

    check("hub 401 raises SchwabAuthError", asyncio.run(outcome(hub_401)) is SchwabAuthError)
    check("hub down raises SchwabAuthError", asyncio.run(outcome(down)) is SchwabAuthError)
    check("upstream 400 raises SchwabAPIError", asyncio.run(outcome(upstream_400)) is SchwabAPIError)


if __name__ == "__main__":
    test_candles_to_closes()
    test_session_anchored_resample()
    test_buckets_never_span_sessions()
    test_ratio_series()
    test_misaligned_legs_intersect()
    test_stats()
    test_z_is_out_of_sample()
    test_mean_varies_by_timeframe()
    test_dst_boundary()
    test_fallback_symbol()
    test_no_overlap_errors()
    test_view_windows()
    test_view_days_clamped_to_lookback()
    test_symbol_validation()
    test_custom_pair()
    test_resolve_pair()
    test_decimals_for()
    test_custom_pair_series()
    test_demo_daily_stamp_convention()
    test_hub_auth_and_availability_errors()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failing check(s): {', '.join(FAILURES)}")
        sys.exit(1)
    print("all checks passed")
