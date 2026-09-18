"""Screener: score every SPY/QQQ member against its benchmark and surface the tails.

A full scan is ~520 price-history calls, which is minutes of wall time under Schwab's
rate limit. So a scan is a background job: start it, poll it, read partial results as
they land. Every scored row is returned -- the ±3σ cut is applied client-side so the
threshold can be changed without rescanning.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

from .config import SETTINGS
from .ratio import STAT_WINDOWS, build_ratio_series, compute_stats, fetch_symbol_closes
from .schwab import SchwabAPIError, SchwabAuthError
from .timeframes import Timeframe, get_timeframe
from .universe import BENCHMARKS, Member, members, summary

MAX_JOBS = 8


# Retry budget per symbol when Schwab pushes back.
MAX_ATTEMPTS = 4
# Statuses that mean "you are going too fast", not "this symbol is bad". Schwab
# answers a burst with 429, and its CDN starts returning 403 Access Denied once
# the burst trips the edge protection -- both must back off rather than give up.
THROTTLE_STATUSES = (429, 403)

# A z-score from a handful of overlapping bars is mostly noise (a recent IPO or a
# thinly-printed name intersects to very few points). Rows below this floor are
# excluded rather than ranked on an unreliable sigma.
MIN_SCORE_BARS = 60


class RateLimiter:
    """Paces requests across all workers, and slows itself down under pushback.

    Additive-increase / multiplicative-decrease: a throttle response doubles the
    spacing, and clean responses ease it back toward the configured rate. Without
    this a scan that trips the edge limiter would lose most of its symbols.
    """

    def __init__(self, per_minute: int, min_per_minute: int = 30):
        self._base = 60.0 / max(1, per_minute)
        self._floor = 60.0 / max(1, min_per_minute)
        self._interval = self._base
        self._lock = asyncio.Lock()
        self._next = 0.0
        self.throttled = 0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._next = max(self._next, now)
            delay = self._next - now
            self._next += self._interval
        if delay > 0:
            await asyncio.sleep(delay)

    async def penalise(self) -> None:
        async with self._lock:
            self._interval = min(self._floor, self._interval * 2)
            self.throttled += 1

    async def relax(self) -> None:
        if self._interval <= self._base:
            return
        async with self._lock:
            self._interval = max(self._base, self._interval * 0.9)

    @property
    def per_minute(self) -> float:
        return 60.0 / self._interval


@dataclass
class ScreenParams:
    timeframe: str = "1D"
    lookback_days: int | None = None
    stat_window: str = "full"
    prefer_qqq: bool = True
    indices: tuple[str, ...] = ("sp500", "nasdaq100")
    extended: bool = False
    limit: int | None = None  # cap the symbol count (useful for a quick look)


@dataclass
class ScreenJob:
    id: str
    params: ScreenParams
    state: str = "pending"  # pending | running | done | failed | cancelled
    total: int = 0
    done: int = 0
    phase: str = "starting"
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    message: str = ""
    started_at: str = ""
    finished_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    task: asyncio.Task | None = None

    def payload(self, include_rows: bool = True) -> dict[str, Any]:
        out = {
            "id": self.id,
            "state": self.state,
            "phase": self.phase,
            "progress": {
                "done": self.done,
                "total": self.total,
                "pct": round(100 * self.done / self.total, 1) if self.total else 0.0,
            },
            "scored": len(self.rows),
            "errors": self.errors[:50],
            "error_count": len(self.errors),
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "meta": self.meta,
        }
        if include_rows:
            out["rows"] = sorted(self.rows, key=lambda r: -abs(r["z"]))
        return out


_JOBS: dict[str, ScreenJob] = {}


def get_job(job_id: str) -> ScreenJob | None:
    return _JOBS.get(job_id)


def _prune_jobs() -> None:
    if len(_JOBS) <= MAX_JOBS:
        return
    finished = [j for j in _JOBS.values() if j.state in {"done", "failed", "cancelled"}]
    finished.sort(key=lambda j: j.finished_at or "")
    for job in finished[: len(_JOBS) - MAX_JOBS]:
        _JOBS.pop(job.id, None)


def _score(
    member: Member,
    benchmark: str,
    symbol_closes: pd.Series,
    bench_closes: pd.Series,
    tf: Timeframe,
    window: int | None,
) -> dict[str, Any] | None:
    ratio = build_ratio_series(symbol_closes, bench_closes, tf)
    if ratio.empty:
        return None
    if len(ratio) < MIN_SCORE_BARS:
        # Too little overlap to trust sigma (recent IPO, thin name): a z from a
        # handful of bars ranks noise next to 1,200-bar names.
        return None
    stats = compute_stats(ratio, window)
    if stats["mean"] is None or not stats["std"]:
        return None

    z = stats["z"]
    if z is None:
        return None
    return {
        "symbol": member.symbol,
        "name": member.name,
        "sector": member.sector,
        "indices": list(member.indices),
        "benchmark": benchmark,
        "pair": f"{member.symbol}/{benchmark}",
        "ratio": stats["last"],
        "mean": stats["mean"],
        "std": stats["std"],
        "z": z,
        "n": stats["n"],
        "pct_from_mean": 100 * (stats["last"] - stats["mean"]) / stats["mean"],
        "direction": "rich" if z > 0 else "cheap",
    }


async def _run(client: Any, job: ScreenJob) -> None:
    params = job.params
    tf = get_timeframe(params.timeframe)
    days = min(params.lookback_days or tf.default_lookback_days, tf.max_lookback_days)
    window = STAT_WINDOWS.get(params.stat_window)

    job.state = "running"
    job.started_at = datetime.now(timezone.utc).isoformat()

    try:
        roster = members(indices=params.indices)
    except FileNotFoundError as exc:
        job.state = "failed"
        job.message = str(exc)
        job.finished_at = datetime.now(timezone.utc).isoformat()
        return

    if params.limit:
        roster = roster[: params.limit]

    job.total = len(roster)
    job.meta = {
        "timeframe": tf.key,
        "timeframe_label": tf.label,
        "lookback_days": days,
        "stat_window": params.stat_window,
        "prefer_qqq": params.prefer_qqq,
        "universe": summary(),
        "symbols": len(roster),
    }

    limiter = RateLimiter(SETTINGS.screen_requests_per_minute)
    gate = asyncio.Semaphore(SETTINGS.screen_concurrency)

    async def fetch_with_retry(symbol: str):
        """Fetch one symbol, backing off and retrying while Schwab pushes back."""
        for attempt in range(MAX_ATTEMPTS):
            async with gate:
                await limiter.wait()
                try:
                    closes = await fetch_symbol_closes(
                        client, symbol, tf, days, extended=params.extended
                    )
                    await limiter.relax()
                    return closes, None
                except SchwabAuthError:
                    raise
                except SchwabAPIError as exc:
                    if exc.status not in THROTTLE_STATUSES:
                        return None, f"HTTP {exc.status}"
                    await limiter.penalise()
                    last = f"HTTP {exc.status} after {MAX_ATTEMPTS} tries"
                except Exception as exc:  # noqa: BLE001 - one bad symbol must not kill the scan
                    return None, exc.__class__.__name__

            # Backoff happens outside the semaphore so a stalled symbol does not
            # hold a worker slot while it sleeps.
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(min(30.0, 2.0 * (2**attempt)) * (0.7 + 0.6 * random.random()))
        return None, last

    # Benchmarks first: every row needs them, and they must not be refetched.
    job.phase = "fetching benchmarks"
    bench: dict[str, pd.Series] = {}
    for symbol in BENCHMARKS:
        closes, error = await fetch_with_retry(symbol)
        if error is not None or closes is None or closes.empty:
            job.state = "failed"
            job.message = (
                f"Could not load benchmark {symbol}: {error or 'no history returned'}."
            )
            job.finished_at = datetime.now(timezone.utc).isoformat()
            return
        bench[symbol] = closes

    job.phase = "scoring members"

    async def one(member: Member) -> None:
        benchmark = member.benchmark(params.prefer_qqq)
        closes, error = await fetch_with_retry(member.symbol)
        job.phase = (
            f"scoring members (throttled, {limiter.per_minute:.0f}/min)"
            if limiter.throttled
            else "scoring members"
        )

        if error is not None:
            job.errors.append({"symbol": member.symbol, "error": error})
            job.done += 1
            return

        if closes.empty:
            job.errors.append({"symbol": member.symbol, "error": "no history"})
            job.done += 1
            return

        row = _score(member, benchmark, closes, bench[benchmark], tf, window)
        if row is None:
            job.errors.append({"symbol": member.symbol, "error": "no overlapping bars"})
        else:
            job.rows.append(row)
        job.done += 1

    try:
        await asyncio.gather(*(one(m) for m in roster))
    except asyncio.CancelledError:
        job.state = "cancelled"
        job.finished_at = datetime.now(timezone.utc).isoformat()
        raise
    except SchwabAuthError as exc:
        job.state = "failed"
        job.message = str(exc)
        job.finished_at = datetime.now(timezone.utc).isoformat()
        return

    job.state = "done"
    job.phase = "complete"
    job.finished_at = datetime.now(timezone.utc).isoformat()


async def _run_guarded(client: Any, job: ScreenJob) -> None:
    """Nothing may leave a job stuck in `running`.

    The scan runs as a detached task, so an exception it does not handle would be
    swallowed by the task object and the job would poll as `running` forever.
    """
    try:
        await _run(client, job)
    except asyncio.CancelledError:
        job.state = "cancelled"
        job.finished_at = datetime.now(timezone.utc).isoformat()
        raise
    except BaseException as exc:  # noqa: BLE001 - report it, whatever it is
        job.state = "failed"
        job.message = f"{exc.__class__.__name__}: {exc}"
        job.finished_at = datetime.now(timezone.utc).isoformat()
        raise


def start_screen(client: Any, params: ScreenParams) -> ScreenJob:
    job = ScreenJob(id=uuid.uuid4().hex[:12], params=params)
    _JOBS[job.id] = job
    _prune_jobs()
    job.task = asyncio.create_task(_run_guarded(client, job))
    # Detached task: consume the exception so it does not surface as "never retrieved".
    job.task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return job


def cancel_screen(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    if not job or job.state not in {"pending", "running"}:
        return False
    if job.task:
        job.task.cancel()
    job.state = "cancelled"
    job.finished_at = datetime.now(timezone.utc).isoformat()
    return True


def estimate_seconds(count: int) -> float:
    """Rough wall time for a scan of `count` symbols at the configured rate."""
    per_minute = max(1, SETTINGS.screen_requests_per_minute)
    return 60.0 * (count + len(BENCHMARKS)) / per_minute


def index_options() -> Iterable[str]:
    return ("sp500", "nasdaq100")
