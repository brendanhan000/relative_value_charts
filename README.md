# Relative Value Charts

Interactive ratio charts for pairs trading, backed by the Schwab Trader API.

Each chart plots one security divided by another as a line, with a horizontal
**mean** line and **±1σ / ±2σ / ±3σ** levels computed from the ratio's own history.
The mean and bands are recomputed per timeframe, so switching from 5m to 1D gives you
a different — and appropriate — set of levels.

**Pairs**

| Chart | Reads as |
|---|---|
| `GLD/USO` | how many USO units buy one GLD unit (gold vs. crude) |
| `$SPX/USO` | S&P 500 index points per USO unit (equities vs. crude) |
| `GLD/SLV` | the gold/silver ratio, via the ETFs |

Plus **Custom** — type any two Schwab symbols to plot their ratio. Everything else
(timeframes, mean/σ levels, live quotes, the table view) works identically. Symbols
are upper-cased for you; index symbols take Schwab's `$` prefix (`$SPX`), and `$SPX`,
`$NDX` and `$DJI` fall back to `SPY`/`QQQ`/`DIA` if your account isn't entitled to
index history. The last six pairs you plot are remembered as one-click chips.

Precision adapts to the ratio's size — `QQQ/SPY` near 0.14 shows 5 decimals, `$SPX/USO`
near 80 shows 3.

**Timeframes.** Selecting one frames the x axis on its own window, while more history
stays loaded behind it — **double-click the chart to fit both axes to everything
loaded**, or scroll/drag to move around. Switching timeframe or pair re-frames; once
you have panned or zoomed, the view is yours and background refreshes leave it alone.

| Timeframe | X axis shows | History loaded |
|---|---|---|
| 5m | 5 days | 15 days |
| 15m | 5 days | 30 days |
| 1H | 15 days | 90 days |
| 4H | 30 days | 365 days |
| 1D | 1 year | 5 years |

The **Lookback** box changes how much is loaded (and therefore how far out you can
zoom); the x-axis window is clamped to it.

---

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

Python **3.9 or newer**. Note for anyone editing `server/api.py`: FastAPI evaluates
route parameter annotations at import time, so those must use `Optional[int]` rather
than `int | None` — `from __future__ import annotations` does not save you there, and
the failure is an import-time crash rather than a type error.

### 2. Start the Schwab hub

Schwab access is handled by the central **schwab_hub** (`../schwab_hub`), which owns
the credentials and the weekly login. This project holds no Schwab secrets.

```bash
../schwab_hub/run.sh login   # first time, then weekly (Schwab expires refresh tokens after 7 days)
../schwab_hub/run.sh         # leave running
```

### 3. Run

```bash
python -m server
```

Open <http://127.0.0.1:8182>.

### Try it without credentials

```bash
RVC_DEMO=1 python -m server
```

Serves deterministic synthetic prices so the whole UI is exercisable offline. A banner
in the app makes clear the data is not real.

---

## Screener

Scores every S&P 500 and Nasdaq-100 member against its own benchmark and surfaces the
names sitting past ±3σ. **Nasdaq-100 members are measured against QQQ, everything else
against SPY**; the ~87 names in both indices default to QQQ (the tighter peer group)
and can be flipped to SPY with one checkbox.

**The screener reproduces the chart, it does not approximate it.** Both paths run the
same fetch, the same intersection, the same aggregation and the same sample σ over the
same window, so a row reading `MU  -3.4` means opening MU/QQQ on the daily chart shows
a ratio sitting below its −3σ line. A regression test pins the two to identical z and
mean, and clicking any row charts that pair on the settings it was scored with.

Output is the plain ticker — `MU`, not `MU/QQQ` — **colour-coded by benchmark**:
blue for names measured against SPY, orange for QQQ. The colour never carries the
meaning alone: every row also names its benchmark in the `vs` column, the legend gives
counts for each, and the results end with a copyable ticker list split by benchmark.

The threshold is applied in the browser over every scored row, so you can slide it from
3σ to 2σ and back without rescanning.

### Constituent lists

Schwab has no index-membership endpoint, so the lists come from
[yfiua/index-constituents](https://github.com/yfiua/index-constituents) (refreshed
monthly) with GICS sectors from
[datasets/s-and-p-500-companies](https://github.com/datasets/s-and-p-500-companies).
They are cached to `data/universe.json` and that cache is what a scan reads — the
network is only touched when you ask:

```bash
python -m server.refresh_universe
```

Dataset tickers are mapped to Schwab's symbology (`BRK.B` → `BRK/B`). Expect a handful
of names per scan to be rejected as unknown — the lists drift from Schwab's symbol set
between refreshes. Those are listed by name under the results rather than silently
dropped.

### Timing and rate limits

A full scan is one request per member plus two benchmarks — about **520 requests**,
run in parallel across 8 workers and paced at 1200/min, so a full scan takes roughly
**25–30 seconds**. Progress shows a live ETA.

Those numbers come from measurement, not the published limit. Against a live account:

| Concurrency | Result |
|---|---|
| 8 (default) | clean |
| 16 | 517 symbols in **6.3s**, zero errors |
| 24 | **tripped Schwab's edge protection** — HTTP 429, then a sustained 403 `Access Denied` that took minutes to clear |

So the ceiling is real and it is not the documented 120/min. The defaults sit at half
the proven-clean concurrency and a quarter of its request rate. Tune with
`SCREEN_REQUESTS_PER_MINUTE` and `SCREEN_CONCURRENCY` knowing where the cliff is.

Requests now go through the **schwab_hub**, which also caps concurrent upstream calls
(`HUB_CONCURRENCY`, default 8, set in `../schwab_hub/.env`). `SCREEN_CONCURRENCY` above that cap
just queues at the hub; raise both together, and keep them below the 24-worker cliff. The hub also
caches history for 60s, so a rescan right after a scan is served from memory.

**The scan backs off by itself.** A 429 or 403 halves the request rate, waits with
exponential backoff and jitter, and retries up to four times per symbol before giving
up; clean responses ease the rate back to the configured value. Throttling therefore
costs time rather than coverage, and the progress line says when it is happening. A
404 or other permanent error is recorded against that symbol immediately rather than
retried.

Scans run as a background job with live progress, and can be cancelled — partial
results stay on screen.

### Next earnings date

Every result row carries its next scheduled earnings announcement — date, days away,
and `BMO`/`AMC` when the source states it; hovering shows the fiscal quarter and
consensus EPS estimate. Anything inside a week is highlighted, since an announcement
before you would expect mean reversion is the thing most likely to invalidate the
trade.

Schwab's fundamental projection has dividend dates but **no earnings date**, so this
comes from Nasdaq's public earnings calendar. That calendar is queried one day at a
time, so the whole forward window is pulled once (~20s) and cached to
`data/earnings.json` for 12 hours; lookups are then instant. It rebuilds itself when
stale, or pre-warm it with:

```bash
python -m server.refresh_earnings
```

**Coverage is partial by nature.** Nasdaq publishes confirmed dates about eight weeks
out — measured, the furthest available date was 55 days ahead — so a company that has
just reported has no next date yet and shows **not scheduled**. In a live scan, 6 of
12 results had a date. Estimating the rest from a quarterly cadence would look more
complete and be wrong, so the blank is left honest.

### A caveat on reading the output

A z-score of a *trending* ratio is bounded (a perfectly linear ramp tops out near
1.7σ), so a 3σ reading is not "this has trended a long way" — it is "this moved sharply
relative to how it usually moves." That makes the screener good at finding recent
dislocations and poor at finding slow multi-year divergences. Shorten the stats window
to make it more sensitive to recent action.

---

## How the numbers are computed

**Base bars.** Schwab serves minute bars only at 1/5/10/15/30 and daily bars. 5m and
15m are fetched natively; 1H and 4H are built from 30m bars; 1D is fetched as daily.

**Aggregation is anchored to the 09:30 ET cash open**, not the wall clock — hourly
buckets run 09:30–10:30, 10:30–11:30, and so on, and a bucket never spans the
overnight gap. This is what makes a "4H" point mean something on a 6.5-hour session.

**The ratio is formed before aggregation.** Each base bar produces one ratio value
(`num.close / den.close`), and those are then aggregated up to the target timeframe by
taking the last value in each bucket. Doing it in this order means a 1H point is the
ratio at a single instant, not a ratio of two independently-aggregated bars.

**Legs are intersected, not forward-filled.** A point appears only where both symbols
printed at the same timestamp. A missing print on one leg drops that point rather than
pairing a fresh price against a stale one.

**Mean and σ** are computed on the plotted ratio values over the stats window, using
the sample standard deviation (`ddof=1`). The levels are flat horizontal lines — this
is a "where does the ratio usually sit" reference, not a rolling band. The
**Mean/σ over** control chooses the window: the full loaded range, or the last
100/200/500 points. **Lookback** controls how much history gets loaded in the first
place; changing it changes the mean.

**Live updates.** The browser polls `/api/quote` every 5 seconds and moves the point
whose bucket is still forming; the series reloads every 60 seconds to append completed
points. If the newest point is older than 1.5 buckets, the live quote is shown in the
header but not written into the chart — so a closed session doesn't drift.

### The z-score / "stretch" chip

`z = (last ratio − mean) / σ`. The status chip pairs a color with a glyph and a text
label (`inside ±1σ`, `past ±1σ`, `past ±2σ`, `past ±3σ`) so the reading never depends on
color alone.

---

## Notes and caveats

- **`$SPX` intraday history depends on your Schwab entitlements.** If it returns
  nothing, the app automatically falls back to **SPY** and shows a banner saying so.
  SPY is roughly 1/10 of SPX, so the ratio's absolute level shifts — the mean and σ
  are still computed correctly on whatever series is actually plotted.
- **ETF ratios are not the underlying commodity ratio.** USO holds front-month WTI
  futures and bleeds on contango; GLD and SLV carry expense ratios. GLD/SLV tracks the
  spot gold/silver ratio closely over short horizons and drifts over long ones. Treat
  the 1D chart over multiple years accordingly.
- **The y axis normally keeps the whole ±3σ envelope on screen**, so the bands never
  clip and the level of the ratio is comparable as you switch timeframes. Double-click
  is the exception: it fits the y axis to the data, which packs the series into the
  full plot height and can push an outer band off screen.
- **σ assumes the ratio is roughly stationary over the window.** For a strongly
  trending ratio, "±2σ from the full-range mean" measures trend, not dislocation.
  Shortening the stats window (or the lookback) is usually the right response.
- Extended-hours bars are off by default; the toggle passes
  `needExtendedHoursData=true` through to Schwab.
- Historical prices are cached server-side for 30 seconds (`HISTORY_CACHE_SECONDS`).

---

## Layout

```
server/
  api.py         FastAPI routes + static file serving
  schwab.py      market-data client for the schwab_hub
  ratio.py       pair definitions, ratio construction, mean/σ stats
  timeframes.py  timeframe table, session-anchored resampling
  screen.py      screener job engine, rate limiting, z-scoring
  universe.py    SPY/QQQ membership, benchmark assignment, symbol mapping
  earnings.py    Nasdaq earnings calendar: fetch, cache, per-symbol lookup
  refresh_universe.py   refresh the cached constituent lists
  refresh_earnings.py   pre-warm the earnings calendar cache
  demo.py        synthetic data source for RVC_DEMO=1
  config.py      .env-backed settings
data/
  universe.json  cached constituent lists (regenerate with refresh_universe)
  earnings.json  cached earnings calendar, rebuilt daily (gitignored)
web/
  index.html, app.js, style.css
  vendor/        TradingView Lightweight Charts (vendored, Apache-2.0)
tests/
  test_ratio.py   offline checks -- no credentials needed
  test_screen.py  screener: universe, benchmark rules, z-scoring, pacing
```

### API

| Endpoint | Purpose |
|---|---|
| `GET /api/config` | pairs, timeframes, defaults, auth status |
| `GET /api/series` | ratio points + stats — `pair` **or** `num`+`den`, `timeframe`, `lookback_days`, `stat_window`, `extended` |
| `GET /api/quote` | current ratio from live quotes — `pair` **or** `num`+`den` |
| `GET /api/auth/status` | whether the hub is up and its Schwab token is valid |
| `GET /api/universe` | constituent counts, list age, and the estimated scan time |
| `GET /api/earnings` | next earnings per symbol — `symbols=A,B,C`, optional `refresh=true` |
| `POST /api/screen` | start a scan — `timeframe`, `stat_window`, `indices`, `prefer_qqq`, `lookback_days`, `limit` |
| `GET /api/screen/{id}` | progress + scored rows (`?rows=false` for progress only) |
| `DELETE /api/screen/{id}` | cancel a running scan |

### Tests

```bash
python -m tests.test_ratio && python -m tests.test_screen
```

Covers session-anchored bucketing (including across DST and the overnight gap), ratio
construction, leg misalignment, the `$SPX`→SPY fallback, custom-symbol validation, and
the mean/σ maths. No network or credentials required.

### Adding a preset pair

Add a `Pair` to `PAIRS` in [server/ratio.py](server/ratio.py) — this is only for pairs
you want as a permanent button; anything else can go through **Custom**. Legs take an
optional `fallback_symbol` for symbols whose history may not be entitled, and
`decimals=None` derives the display precision from the ratio's magnitude.
