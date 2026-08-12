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

### 2. Get Schwab API credentials

1. Sign in at [developer.schwab.com](https://developer.schwab.com) and create an app.
2. Choose the **Market Data Production** product (that's all this project uses — no
   trading scopes needed).
3. Set the app's **Callback URL** to `https://127.0.0.1`.
4. Wait for the app status to become **Ready For Use**, then copy the App Key and Secret.

```bash
cp .env.example .env
```

Fill in `SCHWAB_APP_KEY` and `SCHWAB_APP_SECRET`. Keep `SCHWAB_REDIRECT_URI` exactly
equal to the callback URL you registered.

### 3. Log in

```bash
python -m server.login
```

This opens Schwab's consent page. After you approve, the browser tries to load
`https://127.0.0.1...` and shows an error page — that's expected. Copy the **full URL
from the address bar** and paste it back into the prompt. Tokens are written to
`schwab_token.json` (gitignored, mode 0600).

> Schwab expires refresh tokens after **7 days**, with no way to extend them. Re-run
> `python -m server.login` weekly. Access tokens (30 min) refresh automatically.

### 4. Run

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
  schwab.py      OAuth token store, refresh, market-data calls
  login.py       one-time authorization flow (python -m server.login)
  ratio.py       pair definitions, ratio construction, mean/σ stats
  timeframes.py  timeframe table, session-anchored resampling
  demo.py        synthetic data source for RVC_DEMO=1
  config.py      .env-backed settings
web/
  index.html, app.js, style.css
  vendor/        TradingView Lightweight Charts (vendored, Apache-2.0)
tests/
  test_ratio.py  offline checks -- no credentials needed
```

### API

| Endpoint | Purpose |
|---|---|
| `GET /api/config` | pairs, timeframes, defaults, auth status |
| `GET /api/series` | ratio points + stats — `pair`, `timeframe`, `lookback_days`, `stat_window`, `extended` |
| `GET /api/quote` | current ratio from live quotes — `pair` |
| `GET /api/auth/status` | token state, and the authorization URL when re-login is needed |

### Tests

```bash
python -m tests.test_ratio
```

Covers session-anchored bucketing (including across DST and the overnight gap), ratio
construction, leg misalignment, the `$SPX`→SPY fallback, and the mean/σ maths. No
network or credentials required.

### Adding a pair

Add a `Pair` to `PAIRS` in [server/ratio.py](server/ratio.py). Legs take an optional
`fallback_symbol` for symbols whose history may not be entitled.
