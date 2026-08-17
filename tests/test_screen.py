"""Offline checks for the screener: universe handling and z-scoring.

Run with: python -m tests.test_screen
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from server.earnings import annotate, is_stale
from server.earnings import summary as earnings_summary
from server.schwab import SchwabAPIError
from server.screen import RateLimiter, ScreenParams, _score, estimate_seconds
from server.timeframes import MARKET_TZ, get_timeframe
from server.universe import (
    Member,
    load_universe,
    members,
    repair_text,
    summary,
    to_schwab_symbol,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def series(values: list[float], start: str = "2026-01-02") -> pd.Series:
    idx = pd.date_range(start, periods=len(values), freq="B", tz=MARKET_TZ).tz_convert("UTC")
    return pd.Series(values, index=idx, dtype="float64")


# ------------------------------------------------------------------ universe


def test_symbol_mapping() -> None:
    for raw, want in (("BRK.B", "BRK/B"), ("BF.B", "BF/B"), ("aapl", "AAPL"), (" msft ", "MSFT")):
        check(f"to_schwab_symbol({raw!r}) -> {want}", to_schwab_symbol(raw) == want,
              f"got {to_schwab_symbol(raw)}")


def test_repair_text() -> None:
    check("double-encoded name is repaired",
          repair_text("EstÃ©e Lauder") == "Estée Lauder", repair_text("EstÃ©e Lauder"))
    for clean in ("Estée Lauder", "Apple Inc.", "Coca-Cola", ""):
        check(f"clean text untouched: {clean!r}", repair_text(clean) == clean)


def test_benchmark_assignment() -> None:
    ndx_only = Member("ARM", "Arm", indices=("nasdaq100",))
    sp_only = Member("XOM", "Exxon", indices=("sp500",))
    both = Member("AAPL", "Apple", indices=("sp500", "nasdaq100"))

    check("Nasdaq-100-only -> QQQ", ndx_only.benchmark() == "QQQ")
    check("S&P-only -> SPY", sp_only.benchmark() == "SPY")
    check("dual member -> QQQ by default", both.benchmark() == "QQQ")
    check("dual member -> SPY when preferred", both.benchmark(prefer_qqq=False) == "SPY")
    check("Nasdaq-only stays QQQ even when SPY preferred",
          ndx_only.benchmark(prefer_qqq=False) == "QQQ")
    check("S&P-only stays SPY either way", sp_only.benchmark(prefer_qqq=False) == "SPY")


def test_universe_cache() -> None:
    try:
        data = load_universe(force=True)
    except FileNotFoundError as exc:
        check("universe cache present", False, str(exc))
        return

    info = summary(data)
    check("cache has both indices", set(info["counts"]) == {"sp500", "nasdaq100"}, str(info["counts"]))
    check("S&P 500 count is plausible", 450 <= info["counts"]["sp500"] <= 520,
          str(info["counts"]["sp500"]))
    check("Nasdaq-100 count is plausible", 90 <= info["counts"]["nasdaq100"] <= 110,
          str(info["counts"]["nasdaq100"]))

    all_members = members(data)
    symbols = [m.symbol for m in all_members]
    check("no duplicate symbols", len(symbols) == len(set(symbols)))
    check("symbols are Schwab-formatted", not any("." in s for s in symbols),
          str([s for s in symbols if "." in s][:5]))
    check("every member carries an index", all(m.indices for m in all_members))
    check("no mojibake survived ingest",
          not any(mark in m.name for m in all_members for mark in ("Ã", "Â", "â€")))

    ndx = members(data, indices=("nasdaq100",))
    sp = members(data, indices=("sp500",))
    check("index filter narrows the roster", len(ndx) < len(all_members) and len(sp) < len(all_members),
          f"ndx={len(ndx)} sp={len(sp)} all={len(all_members)}")
    check("filtered roster is internally consistent",
          all("nasdaq100" in m.indices for m in ndx))
    check("union covers everything", len({m.symbol for m in ndx} | {m.symbol for m in sp}) == len(all_members))


def test_missing_cache_is_explicit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope.json"
        try:
            load_universe(missing, force=True)
            check("missing cache raises", False, "no exception")
        except FileNotFoundError as exc:
            check("missing cache raises with a fix", "refresh_universe" in str(exc), str(exc))
    load_universe(force=True)  # restore the module cache


def test_universe_reload_on_mtime_change() -> None:
    """A refresh while the server runs must take effect without a restart."""
    import json
    import os
    import time

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "universe.json"
        payload = {
            "fetched_at": "2026-08-01T00:00:00+00:00",
            "sources": {},
            "counts": {"sp500": 1},
            "members": [
                {"symbol": "AAA", "name": "First", "sector": "", "indices": ["sp500"]}
            ],
        }
        path.write_text(json.dumps(payload))
        first = load_universe(path, force=True)
        check("first load reads the file", first["counts"]["sp500"] == 1)

        # Rewrite with different contents and a later mtime, then load WITHOUT force:
        # the mtime check must pick the new file up.
        payload["counts"]["sp500"] = 2
        payload["members"].append(
            {"symbol": "BBB", "name": "Second", "sector": "", "indices": ["sp500"]}
        )
        path.write_text(json.dumps(payload))
        later = time.time() + 5
        os.utime(path, (later, later))
        second = load_universe(path)
        check("mtime change reloads without force", second["counts"]["sp500"] == 2,
              f"got {second['counts']['sp500']}")
        check("reloaded roster has both members", len(second["members"]) == 2)

    load_universe(force=True)  # restore the module cache


# -------------------------------------------------------------------- scoring


def test_score_flags_three_sigma() -> None:
    """A quiet ratio with a sharp final move must score past 3 sigma."""
    tf = get_timeframe("1D")
    member = Member("TEST", "Test Co", "Industrials", ("sp500",))

    rng = np.random.default_rng(7)
    n = 260
    bench = series(list(100 + rng.normal(0, 0.1, n)))
    # Symbol tracks the benchmark, then gaps 25% higher on the final bar.
    sym_vals = bench.to_numpy() * 2.0
    sym_vals[-1] *= 1.25
    symbol = series(list(sym_vals))

    row = _score(member, "SPY", symbol, bench, tf, None)
    check("scored row produced", row is not None)
    if row is None:
        return
    check("z is past +3 sigma", row["z"] > 3, f"z={row['z']:.2f}")
    check("direction reads rich", row["direction"] == "rich", row["direction"])
    check("pair is symbol/benchmark", row["pair"] == "TEST/SPY", row["pair"])
    check("pct_from_mean is positive", row["pct_from_mean"] > 0, f"{row['pct_from_mean']:.1f}%")
    check("sector carried through", row["sector"] == "Industrials")

    # Mirror it downward.
    sym_vals[-1] = bench.to_numpy()[-1] * 2.0 * 0.75
    down = _score(member, "SPY", series(list(sym_vals)), bench, tf, None)
    check("downside gap scores past -3 sigma", down is not None and down["z"] < -3,
          f"z={down['z']:.2f}" if down else "no row")
    check("direction reads cheap", down is not None and down["direction"] == "cheap")


def test_score_ignores_flat_and_empty() -> None:
    tf = get_timeframe("1D")
    member = Member("TEST", "Test Co", "", ("sp500",))

    flat = series([100.0] * 50)
    check("a constant ratio yields no row (sigma is zero)",
          _score(member, "SPY", flat, flat, tf, None) is None)

    a = series([1.0] * 20, start="2026-01-02")
    b = series([1.0] * 20, start="2026-06-01")
    check("non-overlapping legs yield no row", _score(member, "SPY", a, b, tf, None) is None)


def test_score_matches_manual_z() -> None:
    tf = get_timeframe("1D")
    member = Member("TEST", "Test Co", "", ("sp500",))
    bench = series([10.0] * 40)
    values = [1.0] * 39 + [1.5]
    row = _score(member, "SPY", series([v * 10 for v in values]), bench, tf, None)
    ratios = pd.Series(values)
    want = (ratios.iloc[-1] - ratios.mean()) / ratios.std(ddof=1)
    check("z matches a hand-computed sample z", abs(row["z"] - want) < 1e-9,
          f"{row['z']:.6f} vs {want:.6f}")
    check("n counts every ratio point", row["n"] == 40, str(row["n"]))


def test_stat_window_narrows_scoring() -> None:
    """The stats window should change the score, not just the row count."""
    tf = get_timeframe("1D")
    member = Member("TEST", "Test Co", "", ("sp500",))
    bench = series([10.0] * 300)
    values = [1.0] * 150 + [2.0] * 149 + [2.5]
    symbol = series([v * 10 for v in values])

    full = _score(member, "SPY", symbol, bench, tf, None)
    recent = _score(member, "SPY", symbol, bench, tf, 100)
    check("full-range and 100-bar windows disagree", full["z"] != recent["z"],
          f"full={full['z']:.3f} recent={recent['z']:.3f}")
    check("the 100-bar window uses 100 points", recent["n"] == 100, str(recent["n"]))
    check("the full window uses every point", full["n"] == 300, str(full["n"]))


# --------------------------------------------------------------------- pacing


def test_screener_matches_the_daily_chart() -> None:
    """The screener must reproduce the chart's own numbers, not approximate them.

    A row that says MU is past 3 sigma is only meaningful if opening MU/QQQ on the
    daily chart shows the same reading, so both paths are pinned to the same result.
    """
    from server.demo import DemoClient
    from server.ratio import build_series, custom_pair, fetch_symbol_closes

    async def compare() -> list[tuple[str, float, float, float, float]]:
        client = DemoClient()
        tf = get_timeframe("1D")
        days = tf.default_lookback_days
        bench = {b: await fetch_symbol_closes(client, b, tf, days) for b in ("SPY", "QQQ")}

        out = []
        for symbol, benchmark in (("MU", "QQQ"), ("AAPL", "QQQ"), ("JNJ", "SPY"), ("XOM", "SPY")):
            chart = await build_series(client, custom_pair(symbol, benchmark), "1D")
            closes = await fetch_symbol_closes(client, symbol, tf, days)
            row = _score(
                Member(symbol, symbol, "", ("sp500",)), benchmark, closes, bench[benchmark], tf, None
            )
            out.append(
                (f"{symbol}/{benchmark}", chart["stats"]["z"], row["z"],
                 chart["stats"]["mean"], row["mean"])
            )
        return out

    for pair, chart_z, screen_z, chart_mean, screen_mean in asyncio.run(compare()):
        check(f"{pair}: screener z == daily-chart z", abs(chart_z - screen_z) < 1e-12,
              f"chart={chart_z:.6f} screen={screen_z:.6f}")
        check(f"{pair}: screener mean == daily-chart mean", abs(chart_mean - screen_mean) < 1e-12,
              f"chart={chart_mean:.6f} screen={screen_mean:.6f}")


def test_rate_limiter_paces() -> None:
    async def run() -> float:
        limiter = RateLimiter(per_minute=600)  # 0.1s apart
        loop = asyncio.get_event_loop()
        start = loop.time()
        await asyncio.gather(*(limiter.wait() for _ in range(6)))
        return loop.time() - start

    elapsed = asyncio.run(run())
    # Six requests at 0.1s spacing: the last one waits ~0.5s.
    check("rate limiter spreads concurrent waiters", 0.4 <= elapsed <= 1.2, f"{elapsed:.2f}s")


def test_rate_limiter_backs_off_and_recovers() -> None:
    async def run() -> tuple[float, float, float, int]:
        limiter = RateLimiter(per_minute=600)  # 0.1s spacing
        base = limiter.per_minute
        await limiter.penalise()
        await limiter.penalise()
        slowed = limiter.per_minute
        for _ in range(40):
            await limiter.relax()
        return base, slowed, limiter.per_minute, limiter.throttled

    base, slowed, recovered, throttled = asyncio.run(run())
    check("throttling halves the rate each time", abs(slowed - base / 4) < 1e-6,
          f"base={base:.0f}/min slowed={slowed:.0f}/min")
    check("throttle events are counted", throttled == 2, str(throttled))
    check("clean responses restore the configured rate", abs(recovered - base) < 1e-6,
          f"{recovered:.1f} vs {base:.1f}")
    check("recovery never exceeds the configured rate", recovered <= base + 1e-9)


def test_scan_retries_through_throttling() -> None:
    """A burst of 429/403 must cost time, not symbols."""
    from server.demo import DemoClient
    from server.screen import ScreenJob, _run

    class ThrottlingClient(DemoClient):
        """Rejects the first two calls for every symbol, then behaves."""

        def __init__(self) -> None:
            super().__init__()
            self.seen: dict[str, int] = {}

        async def price_history(self, symbol: str, **kw):
            n = self.seen.get(symbol, 0) + 1
            self.seen[symbol] = n
            if n <= 2:
                raise SchwabAPIError(429 if n == 1 else 403, "slow down")
            return await super().price_history(symbol, **kw)

    async def run() -> ScreenJob:
        job = ScreenJob(id="test", params=ScreenParams(timeframe="1D", limit=6))
        await _run(ThrottlingClient(), job)
        return job

    job = asyncio.run(run())
    check("scan still completes under throttling", job.state == "done", f"{job.state}: {job.message}")
    check("every symbol was recovered, none dropped", len(job.rows) == 6,
          f"rows={len(job.rows)} errors={job.errors}")
    check("no symbol reported as an error", job.errors == [], str(job.errors))


def test_permanent_error_is_not_retried_forever() -> None:
    """A 404 is about the symbol, not the rate -- record it and move on."""
    from server.demo import DemoClient
    from server.screen import ScreenJob, _run

    class NotFoundClient(DemoClient):
        async def price_history(self, symbol: str, **kw):
            if symbol in ("SPY", "QQQ"):
                return await super().price_history(symbol, **kw)
            raise SchwabAPIError(404, "no such symbol")

    async def run() -> ScreenJob:
        job = ScreenJob(id="test", params=ScreenParams(timeframe="1D", limit=4))
        await _run(NotFoundClient(), job)
        return job

    job = asyncio.run(run())
    check("scan completes despite bad symbols", job.state == "done", job.state)
    check("bad symbols are reported, not scored", len(job.errors) == 4 and not job.rows,
          f"errors={len(job.errors)} rows={len(job.rows)}")
    check("the status code is surfaced", all("404" in e["error"] for e in job.errors),
          str(job.errors[:2]))


# ------------------------------------------------------------------ earnings


def _calendar(today: str, entries: dict) -> dict:
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "horizon_days": 60,
        "from": today,
        "to": today,
        "count": len(entries),
        "entries": entries,
    }


def test_earnings_annotate() -> None:
    cal = _calendar("2026-08-15", {
        "CRWD": {"date": "2026-08-26", "when": "AMC", "fiscal_quarter": "Jul/2026",
                 "eps_forecast": "$0.05"},
        "BRK.B": {"date": "2026-09-04", "when": "", "fiscal_quarter": "", "eps_forecast": ""},
    })

    out = annotate(["CRWD", "AMD", "brk/b"], cal)

    check("scheduled symbol is marked scheduled", out["CRWD"]["scheduled"] is True)
    check("days_away counts from the calendar's own date", out["CRWD"]["days_away"] == 11,
          str(out["CRWD"]["days_away"]))
    check("timing is carried through", out["CRWD"]["when"] == "AMC")
    check("eps forecast is carried through", out["CRWD"]["eps_forecast"] == "$0.05")

    check("unscheduled symbol is explicit, not missing", out["AMD"] == {"scheduled": False})

    # Schwab writes share classes with a slash, Nasdaq with a dot.
    check("BRK/B resolves to Nasdaq's BRK.B", out["BRK/B"]["scheduled"] is True,
          str(out["BRK/B"]))
    check("lower-case input is normalised", "BRK/B" in out)


def test_earnings_staleness() -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    fresh = _calendar(today, {"A": {"date": today}})
    check("a calendar built today is fresh", is_stale(fresh) is False)
    check("a missing calendar is stale", is_stale(None) is True)

    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    # Built yesterday but only minutes ago: still stale, because "days away" shifted.
    rolled = _calendar(yesterday, {"A": {"date": today}})
    check("a calendar from a previous day is stale even if recently fetched",
          is_stale(rolled) is True)

    old = _calendar(today, {"A": {"date": today}})
    old["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    check("a calendar older than the TTL is stale", is_stale(old) is True)

    check("a malformed calendar is stale, not a crash", is_stale({"junk": 1}) is True)


def test_earnings_summary() -> None:
    check("no calendar reports unavailable", earnings_summary(None) == {"available": False})
    info = earnings_summary(_calendar("2026-08-15", {"A": {"date": "2026-08-20"}}))
    check("summary reports availability and count",
          info["available"] is True and info["count"] == 1, str(info))


def test_estimate_seconds() -> None:
    check("estimate scales with count", estimate_seconds(500) > estimate_seconds(100))
    check("estimate is positive", estimate_seconds(1) > 0)


def test_params_defaults() -> None:
    p = ScreenParams()
    check("defaults to daily bars", p.timeframe == "1D", p.timeframe)
    check("defaults to both indices", p.indices == ("sp500", "nasdaq100"), str(p.indices))
    check("defaults to QQQ for dual members", p.prefer_qqq is True)


if __name__ == "__main__":
    test_symbol_mapping()
    test_repair_text()
    test_benchmark_assignment()
    test_universe_cache()
    test_missing_cache_is_explicit()
    test_universe_reload_on_mtime_change()
    test_score_flags_three_sigma()
    test_score_ignores_flat_and_empty()
    test_score_matches_manual_z()
    test_stat_window_narrows_scoring()
    test_screener_matches_the_daily_chart()
    test_rate_limiter_paces()
    test_rate_limiter_backs_off_and_recovers()
    test_scan_retries_through_throttling()
    test_permanent_error_is_not_retried_forever()
    test_earnings_annotate()
    test_earnings_staleness()
    test_earnings_summary()
    test_estimate_seconds()
    test_params_defaults()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failing check(s): {', '.join(FAILURES)}")
        sys.exit(1)
    print("all checks passed")
