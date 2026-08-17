/* Relative value ratio charts — Schwab price history, mean and 1/2/3σ levels. */

const LWC = window.LightweightCharts;

const BUCKET_SECONDS = { '5m': 300, '15m': 900, '1H': 3600, '4H': 14400, '1D': 86400 };
const REFRESH_MS = 60_000;
const RIGHT_OFFSET = 4; // blank slots kept to the right of the last point
const MIN_BAR_SPACING = 0.05; // low enough that "show everything" always fits

const RECENTS_KEY = 'rvc-custom-pairs';
const MAX_RECENTS = 6;

const state = {
  config: null,
  pair: null,
  custom: null,          // {num, den} when a hand-entered pair is active
  loadedKey: null,       // which pair the drawn series belongs to
  timeframe: null,
  statWindow: 'full',
  lookback: null,
  extended: false,
  live: true,
  demoNotice: null,
  series: null,          // last /api/series payload
  points: [],            // chart-format {time, value}
  realTimes: [],         // true epoch seconds, parallel to points
  priceLines: [],
  decimals: 4,
  loadToken: 0,
  viewMode: 'default',   // 'default' | 'full' | 'manual'
};

const el = (id) => document.getElementById(id);
const css = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* ------------------------------------------------------------------ time */

// Lightweight Charts renders timestamps as UTC, so shift intraday bars by the
// New York offset to make the axis read in exchange time. Offsets are cached per
// UTC hour: a per-day cache would collide across the 02:00 ET DST transitions,
// which fall mid-UTC-day and change the offset by an hour.
const _etOffsets = new Map();
const _etFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York', hour12: false,
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});

function etParts(epochSec) {
  const p = _etFmt.formatToParts(new Date(epochSec * 1000));
  const o = {};
  for (const { type, value } of p) o[type] = value;
  return o;
}

function etOffset(epochSec) {
  const hourKey = Math.floor(epochSec / 3600);
  if (_etOffsets.has(hourKey)) return _etOffsets.get(hourKey);
  const m = etParts(epochSec);
  const asUtc = Date.UTC(+m.year, +m.month - 1, +m.day, +m.hour % 24, +m.minute, +m.second) / 1000;
  const off = Math.round((asUtc - epochSec) / 60) * 60;
  _etOffsets.set(hourKey, off);
  return off;
}

function utcDateString(epochSec) {
  // Schwab stamps daily bars at 00:00 UTC of the session date, so the label is
  // the UTC date. Converting to ET first would show the prior calendar day.
  const d = new Date(epochSec * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}

function toChartTime(epochSec, tfKey) {
  return tfKey === '1D' ? utcDateString(epochSec) : epochSec + etOffset(epochSec);
}

function formatChartTime(time, tfKey) {
  if (typeof time === 'string') return time;
  if (time && typeof time === 'object') {
    return `${time.year}-${String(time.month).padStart(2, '0')}-${String(time.day).padStart(2, '0')}`;
  }
  const d = new Date(time * 1000); // already ET-shifted; read back as UTC
  const pad = (n) => String(n).padStart(2, '0');
  const stamp = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
  if (tfKey === '1D') return stamp;
  return `${stamp} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} ET`;
}

/* --------------------------------------------------------------- numbers */

function fmt(value, decimals = state.decimals) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Number(value).toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function stretchLevel(z) {
  const a = Math.abs(z ?? 0);
  if (z === null || z === undefined) return { label: 'no reading', color: 'var(--ink-muted)', glyph: '–' };
  if (a < 1) return { label: 'inside ±1σ', color: 'var(--good)', glyph: '●' };
  if (a < 2) return { label: 'past ±1σ', color: 'var(--warning)', glyph: '▲' };
  if (a < 3) return { label: 'past ±2σ', color: 'var(--serious)', glyph: '◆' };
  return { label: 'past ±3σ', color: 'var(--critical)', glyph: '■' };
}

/* ----------------------------------------------------------------- chart */

let chart = null;
let ratioLine = null;

function chartTheme() {
  return {
    layout: {
      background: { type: 'solid', color: css('--surface') },
      textColor: css('--ink-muted'),
      fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif',
      fontSize: 11,
    },
    grid: {
      vertLines: { color: css('--grid') },
      horzLines: { color: css('--grid') },
    },
    rightPriceScale: { borderColor: css('--axis') },
    timeScale: {
      borderColor: css('--axis'),
      rightOffset: RIGHT_OFFSET,
      minBarSpacing: MIN_BAR_SPACING,
      secondsVisible: false,
    },
    crosshair: {
      mode: LWC.CrosshairMode.Normal,
      vertLine: { color: css('--ink-muted'), width: 1, style: LWC.LineStyle.Dotted, labelBackgroundColor: css('--ink-2') },
      horzLine: { color: css('--ink-muted'), width: 1, style: LWC.LineStyle.Dotted, labelBackgroundColor: css('--ink-2') },
    },
  };
}

function lineTheme() {
  return {
    color: css('--series-1'),
    lineWidth: 2,
    crosshairMarkerRadius: 4,
    lastValueVisible: true,
    priceLineVisible: true,
    priceLineStyle: LWC.LineStyle.Dotted,
    priceLineColor: css('--series-1'),
  };
}

function buildChart() {
  chart = LWC.createChart(el('chart'), {
    ...chartTheme(),
    autoSize: true,
    localization: { priceFormatter: (p) => fmt(p) },
  });
  ratioLine = chart.addLineSeries({
    ...lineTheme(),
    // Keep the full ±3σ envelope on screen -- the bands are the point of the chart,
    // and price-line levels are not part of the default autoscale. The exception is
    // the zoomed-out 'full' view, where double-click means fit to the data.
    autoscaleInfoProvider: (original) => {
      const res = original();
      const st = state.series?.stats;
      if (state.viewMode === 'full') return res;
      if (!res?.priceRange || !st || st.mean === null) return res;
      const b3 = st.bands.find((b) => b.k === 3);
      if (!b3) return res;
      const pad = (b3.upper - b3.lower) * 0.03;
      return {
        priceRange: {
          minValue: Math.min(res.priceRange.minValue, b3.lower - pad),
          maxValue: Math.max(res.priceRange.maxValue, b3.upper + pad),
        },
        margins: res.margins,
      };
    },
  });
  chart.subscribeCrosshairMove(onCrosshair);

  const target = el('chart');

  // Panning or zooming hands the view to the user; nothing re-frames it after that.
  // Capture phase: the chart's canvas handlers stop these from bubbling.
  for (const evt of ['mousedown', 'wheel', 'touchstart']) {
    target.addEventListener(evt, () => { state.viewMode = 'manual'; }, {
      passive: true,
      capture: true,
    });
  }

  // Double-click zooms all the way out to everything loaded. Fires after the
  // mousedown above, so it wins over the 'manual' flag that click just set.
  chart.subscribeDblClick(() => {
    state.viewMode = 'full';
    applyView();
  });

  // autoSize resizes the pane after the first framing, which would otherwise leave
  // the series crushed against the right edge.
  new ResizeObserver(() => applyView()).observe(target);
}

/* How many trailing points cover the last `days` calendar days. */
function pointsInWindow(days) {
  const n = state.realTimes.length;
  if (!days || !n) return n;
  const cutoff = state.realTimes[n - 1] - days * 86400;
  const first = state.realTimes.findIndex((t) => t >= cutoff);
  return first > 0 ? n - first : n;
}

function plotWidth() {
  const canvas = el('chart').querySelector('canvas');
  const width = canvas ? canvas.getBoundingClientRect().width : el('chart').clientWidth;
  return Math.round(width);
}

/* Framing the x axis. 'default' shows the timeframe's own window (5m -> 5 days,
   1D -> 1 year, ...), 'full' shows every loaded point, 'manual' leaves it alone.
 *
 * This drives barSpacing rather than setVisibleLogicalRange/fitContent: those two
 * queue a range that is only flushed on the chart's next internal repaint, and with
 * this layout that repaint does not reliably come -- the range silently never
 * applies. Setting barSpacing + rightOffset through applyOptions repaints
 * immediately and pins the last point to the right edge.
 */
function applyView() {
  if (state.viewMode === 'manual' || !state.points.length) return;

  requestAnimationFrame(() => {
    const width = plotWidth();
    if (!width) return;

    const visible = state.viewMode === 'full'
      ? state.points.length
      : pointsInWindow(state.series?.timeframe?.view_days);

    chart.applyOptions({
      timeScale: {
        barSpacing: Math.max(MIN_BAR_SPACING, width / (visible + RIGHT_OFFSET)),
        rightOffset: RIGHT_OFFSET,
      },
    });

    // Re-fit the y axis to whatever is now in view, and undo any manual drag of
    // the price scale. Toggling autoScale forces the recompute; setting it to the
    // value it already holds is a no-op.
    const price = chart.priceScale('right');
    price.applyOptions({ autoScale: false });
    price.applyOptions({ autoScale: true });
  });
}

function applyTheme() {
  chart.applyOptions(chartTheme());
  ratioLine.applyOptions(lineTheme());
  drawLevels();
  applyView(); // chartTheme() rewrites timeScale options; keep the framing
}

/* Mean and σ levels are chart chrome, not a series: one muted ink, told apart by
   dash pattern plus an axis label, so nothing depends on hue. */
function drawLevels() {
  for (const line of state.priceLines) ratioLine.removePriceLine(line);
  state.priceLines = [];

  const stats = state.series?.stats;
  if (!stats || stats.mean === null) return;

  const ink2 = css('--ink-2');
  const muted = css('--ink-muted');
  const specs = [
    { price: stats.mean, title: 'mean', color: ink2, width: 2, style: LWC.LineStyle.Solid },
  ];
  const styles = {
    1: LWC.LineStyle.Dashed,
    2: LWC.LineStyle.LargeDashed,
    3: LWC.LineStyle.SparseDotted,
  };
  for (const band of stats.bands) {
    specs.push({ price: band.upper, title: `+${band.k}σ`, color: muted, width: 1, style: styles[band.k] });
    specs.push({ price: band.lower, title: `−${band.k}σ`, color: muted, width: 1, style: styles[band.k] });
  }

  for (const s of specs) {
    state.priceLines.push(ratioLine.createPriceLine({
      price: s.price,
      color: s.color,
      lineWidth: s.width,
      lineStyle: s.style,
      axisLabelVisible: true,
      title: s.title,
    }));
  }
}

function zOf(value) {
  const st = state.series?.stats;
  return st && st.std ? (value - st.mean) / st.std : null;
}

function onCrosshair(param) {
  const point = param?.time ? param.seriesData?.get(ratioLine) : null;
  if (!point) { renderReadoutLatest(); return; }

  const z = zOf(point.value);
  el('readout').innerHTML =
    `<span class="t">${formatChartTime(param.time, state.timeframe)}</span><br>` +
    `<b>${fmt(point.value)}</b>` +
    (z === null ? '' : `  <span class="t">·</span>  z <b>${z.toFixed(2)}</b>`);
}

function renderReadoutLatest() {
  const box = el('readout');
  const s = state.series;
  if (!s || !state.points.length) { box.innerHTML = ''; return; }

  const last = state.points[state.points.length - 1];
  const z = zOf(last.value);
  box.innerHTML =
    `<b>${s.pair.label}</b> <span class="t">· ${s.timeframe.label}</span><br>` +
    `<b>${fmt(last.value)}</b>` +
    (z === null ? '' : `  <span class="t">·</span>  z <b>${z.toFixed(2)}</b>`);
}

/* ------------------------------------------------------------------ tiles */

function renderTiles() {
  const s = state.series;
  const strip = el('strip');
  if (!s) { strip.innerHTML = ''; return; }
  const st = s.stats;
  const lvl = stretchLevel(st.z);
  const spread = st.mean ? ((st.last - st.mean) / st.mean) * 100 : null;

  const tiles = [
    ['Ratio now', `${fmt(st.last)}`, s.pair.unit_hint],
    ['Mean', `${fmt(st.mean)}`, `${st.n} bars`],
    ['Std dev', `${fmt(st.std)}`, st.mean ? `${((st.std / st.mean) * 100).toFixed(2)}% of mean` : ''],
    ['vs mean', st.mean === null ? '—' : `${spread >= 0 ? '+' : ''}${spread.toFixed(2)}%`, 'spread'],
  ];

  strip.innerHTML = tiles
    .map(([k, v, sub]) =>
      `<div class="tile"><div class="k">${k}</div><div class="v">${v}` +
      (sub ? `<small>${sub}</small>` : '') + `</div></div>`)
    .join('') +
    `<div class="tile"><div class="k">Stretch</div><div class="v">` +
    `<span class="chip"><span class="dot" style="background:${lvl.color}"></span>` +
    `<span class="glyph">${lvl.glyph}</span>` +
    `z ${st.z === null ? '—' : st.z.toFixed(2)}<small>${lvl.label}</small></span></div></div>`;
}

function renderTables() {
  const s = state.series;
  if (!s) return;
  const st = s.stats;
  const rows = [];
  if (st.mean !== null) {
    for (const band of [...st.bands].reverse()) {
      rows.push([`+${band.k}σ`, fmt(band.upper)]);
    }
    rows.push(['mean', fmt(st.mean)]);
    for (const band of st.bands) {
      rows.push([`−${band.k}σ`, fmt(band.lower)]);
    }
  }
  el('levels-table').innerHTML =
    `<caption>Mean &amp; standard-deviation levels — ${s.pair.label}, ${s.timeframe.label}</caption>` +
    `<thead><tr><th>Level</th><th>Ratio</th></tr></thead><tbody>` +
    rows.map(([a, b]) => `<tr><td>${a}</td><td>${b}</td></tr>`).join('') +
    `</tbody><tfoot><tr><td>σ</td><td>${fmt(st.std)}</td></tr>` +
    `<tr><td>bars</td><td>${st.n}</td></tr>` +
    `<tr><td>min / max</td><td>${fmt(st.min)} / ${fmt(st.max)}</td></tr></tfoot>`;

  const recent = state.points.slice(-25).reverse();
  el('bars-table').innerHTML =
    `<caption>Last 25 points</caption>` +
    `<thead><tr><th>Time (ET)</th><th>Ratio</th><th>vs mean</th><th>z</th></tr></thead><tbody>` +
    recent.map((p) => {
      const z = st.std ? ((p.value - st.mean) / st.std).toFixed(2) : '—';
      const pct = st.mean ? `${(((p.value - st.mean) / st.mean) * 100).toFixed(2)}%` : '—';
      return `<tr><td>${formatChartTime(p.time, state.timeframe)}</td>` +
             `<td>${fmt(p.value)}</td><td>${pct}</td><td>${z}</td></tr>`;
    }).join('') + `</tbody>`;
}

/* ------------------------------------------------------------------ data */

function showError(message) {
  const box = el('error');
  box.hidden = !message;
  if (message) box.innerHTML = message;
  // Nothing has ever loaded -- an empty chart frame is just noise.
  const blank = Boolean(message) && !state.points.length;
  el('chart-card').hidden = blank;
  document.querySelector('details.table-view').hidden = blank;
}

function showNotice(messages) {
  const box = el('notice');
  if (!messages || !messages.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = messages.join('<br>');
}

/* Identifies the pair being requested, so a failed load can tell "this symbol is
   bad" from "the refresh of what is already drawn hiccuped". */
function pairKey() {
  return state.custom ? `custom:${state.custom.num}/${state.custom.den}` : state.pair;
}

function pairParams() {
  const params = new URLSearchParams();
  if (state.custom) {
    params.set('num', state.custom.num);
    params.set('den', state.custom.den);
  } else {
    params.set('pair', state.pair);
  }
  return params;
}

async function loadSeries({ preserveView = false } = {}) {
  const token = ++state.loadToken;
  const requestKey = pairKey();
  el('loading').hidden = false;
  el('live-stamp').textContent = '';

  const params = pairParams();
  params.set('timeframe', state.timeframe);
  params.set('stat_window', state.statWindow);
  params.set('extended', String(state.extended));
  if (state.lookback) params.set('lookback_days', String(state.lookback));

  let payload;
  try {
    const resp = await fetch(`/api/series?${params}`);
    payload = await resp.json();
    if (!resp.ok) {
      const err = new Error(payload.detail || payload.error || `HTTP ${resp.status}`);
      err.kind = payload.kind || (resp.status === 401 ? 'auth' : 'other');
      throw err;
    }
  } catch (err) {
    if (token !== state.loadToken) return false;
    el('loading').hidden = true;
    // Never leave another pair's series on screen under an error about this one.
    if (state.loadedKey !== requestKey) {
      state.points = [];
      state.realTimes = [];
      state.series = null;
      ratioLine.setData([]);
      drawLevels();
      el('strip').innerHTML = '';
    }
    showError(describeError(err.message, err.kind));
    return false;
  }
  if (token !== state.loadToken) return false;

  showError(null);
  showNotice([...(state.demoNotice ? [state.demoNotice] : []), ...payload.notes]);

  state.series = payload;
  state.loadedKey = requestKey;
  state.decimals = payload.pair.decimals;
  state.points = payload.points.map((p) => ({
    time: toChartTime(p.time, state.timeframe),
    value: p.value,
  }));
  state.realTimes = payload.points.map((p) => p.time);

  ratioLine.applyOptions({
    priceFormat: {
      type: 'price',
      precision: state.decimals,
      minMove: Math.pow(10, -state.decimals),
    },
  });
  ratioLine.setData(state.points);
  // A pair/timeframe switch re-frames; a background refresh keeps whatever the
  // user is looking at (applyView is a no-op once they have panned or zoomed).
  if (!preserveView) state.viewMode = 'default';
  applyView();

  drawLevels();
  renderTiles();
  renderTables();
  renderReadoutLatest();
  renderHeader();
  el('loading').hidden = true;
  return true;
}

function describeError(message, kind) {
  if (kind === 'auth') {
    return `<b>Schwab authorization needed.</b> ${message}<br>` +
           `Set your credentials in <code>.env</code>, run <code>python -m server.login</code>, then Refresh. ` +
           `To try the UI without credentials, start with <code>RVC_DEMO=1 python -m server</code>.`;
  }
  if (kind === 'upstream') {
    return `<b>Schwab rejected the request.</b> ${message}`;
  }
  const hint = state.custom
    ? ' Check the spelling, and note that Schwab wants index symbols as <code>$SPX</code>.'
    : '';
  return `<b>Could not load the series.</b> ${message}${hint}`;
}

function renderHeader() {
  const s = state.series;
  if (!s) return;
  el('pair-title').textContent = `${s.pair.label} · ${s.timeframe.label}`;
  el('pair-desc').textContent = s.pair.description;
  const agg = s.timeframe.aggregated
    ? ` Built from ${s.timeframe.base_frequency} bars, aggregated from the 09:30 ET open.`
    : '';
  el('foot').innerHTML =
    `Showing the last <b>${s.timeframe.view_days} days</b> of ` +
    `${s.timeframe.lookback_days} days loaded — <b>double-click the chart</b> to fit ` +
    `both axes to the full range, or scroll and drag to move around.<br>` +
    `Ratio points are formed at ${s.timeframe.base_frequency} resolution ` +
    `(${s.pair.numerator} ÷ ${s.pair.denominator}) and only on timestamps where both legs printed.${agg} ` +
    `Mean and σ come from all ${s.stats.n} loaded points ` +
    `(${s.stat_window === 'full' ? 'full loaded range' : `last ${s.stat_window}`}), ` +
    `sample standard deviation — the levels do not change as you zoom.`;
}

/* -------------------------------------------------------------- realtime */

async function pollQuote() {
  if (!state.live || !state.series || !state.points.length) return;
  // The chart shows the last successfully loaded series; if a reload failed after
  // a pair/timeframe switch, the current controls no longer describe what is on
  // screen. Compare against the request identity -- for custom pairs the server's
  // pair.key is 'custom:NUM/DEN', which never equals state.pair.
  if (state.loadedKey !== pairKey() || state.series.timeframe.key !== state.timeframe) return;
  let data;
  try {
    const resp = await fetch(`/api/quote?${pairParams()}`);
    data = await resp.json();
  } catch {
    return;
  }
  if (!data.ok) return;
  // A leg may have fallen back server-side (e.g. $SPX quote missing -> SPY). Writing
  // that ratio into a series built from the primary symbol would draw a bogus jump.
  const plotted = state.series.pair;
  if (data.numerator.symbol !== plotted.numerator || data.denominator.symbol !== plotted.denominator) {
    el('live-stamp').textContent = 'live quote uses different legs than the chart — refresh';
    return;
  }

  const bucket = BUCKET_SECONDS[state.timeframe] ?? 3600;
  const lastReal = state.realTimes[state.realTimes.length - 1];
  const nowSec = Date.now() / 1000;

  // Only move the last point while its bucket is still forming; a full refresh
  // (every 60s) is what appends new points.
  if (nowSec - lastReal > bucket * 1.5) {
    el('live-stamp').textContent = `last quote ${fmt(data.ratio)} · session closed`;
    return;
  }

  const last = state.points[state.points.length - 1];
  last.value = data.ratio;
  ratioLine.update(last);

  el('live-stamp').textContent =
    `${data.numerator.symbol} ${data.numerator.price.toFixed(2)} / ` +
    `${data.denominator.symbol} ${data.denominator.price.toFixed(2)} · ` +
    // Exchange time, to match the axis -- not whatever zone the browser is in.
    new Date(data.as_of).toLocaleTimeString('en-US', { timeZone: 'America/New_York' }) + ' ET';

  if (state.series.stats.std) {
    state.series.stats.last = data.ratio;
    state.series.stats.z = (data.ratio - state.series.stats.mean) / state.series.stats.std;
    renderTiles();
  }
  renderReadoutLatest();
}

/* ----------------------------------------------------------------- setup */

function segment(container, items, selected, onPick) {
  container.innerHTML = '';
  for (const item of items) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = item.label;
    b.title = item.title || '';
    b.setAttribute('aria-label', item.title ? `${item.label} — ${item.title}` : item.label);
    b.setAttribute('aria-pressed', String(item.key === selected));
    b.addEventListener('click', () => onPick(item.key));
    container.appendChild(b);
  }
}

const CUSTOM_KEY = '__custom__';

function renderSegments() {
  const items = state.config.pairs.map((p) => ({
    key: p.key, label: p.label, title: p.description,
  }));
  items.push({ key: CUSTOM_KEY, label: 'Custom', title: 'Plot the ratio of any two symbols' });

  segment(
    el('pair-seg'),
    items,
    state.custom ? CUSTOM_KEY : state.pair,
    (key) => {
      if (key === CUSTOM_KEY) {
        el('custom-row').hidden = false;
        el('custom-num').focus();
        return;
      }
      state.custom = null;
      state.pair = key;
      renderSegments();
      loadSeries();
    },
  );
  segment(
    el('tf-seg'),
    state.config.timeframes.map((t) => ({ key: t.key, label: t.key, title: t.label })),
    state.timeframe,
    (key) => {
      state.timeframe = key;
      state.lookback = null;
      el('lookback').value = tfConfig(key).default_lookback_days;
      el('lookback').max = tfConfig(key).max_lookback_days;
      renderSegments();
      loadSeries();
    },
  );
}

function tfConfig(key) {
  return state.config.timeframes.find((t) => t.key === key);
}

/* ---------------------------------------------------------- custom pairs */

function readRecents() {
  try {
    const stored = JSON.parse(localStorage.getItem(RECENTS_KEY) || '[]');
    return Array.isArray(stored) ? stored.filter((r) => r && r.num && r.den) : [];
  } catch {
    return [];
  }
}

function rememberCustom(num, den) {
  const rest = readRecents().filter((r) => !(r.num === num && r.den === den));
  const next = [{ num, den }, ...rest].slice(0, MAX_RECENTS);
  try { localStorage.setItem(RECENTS_KEY, JSON.stringify(next)); } catch { /* private mode */ }
  renderRecents();
}

function renderRecents() {
  const box = el('custom-recents');
  const recents = readRecents();
  box.innerHTML = '';
  for (const { num, den } of recents) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.textContent = `${num}/${den}`;
    chip.title = `Plot ${num} ÷ ${den}`;
    chip.addEventListener('click', () => plotCustom(num, den));
    box.appendChild(chip);
  }
}

async function plotCustom(num, den) {
  const n = (num || '').trim().toUpperCase();
  const d = (den || '').trim().toUpperCase();
  el('custom-num').value = n;
  el('custom-den').value = d;

  if (!n || !d) {
    showError('<b>Enter a symbol for both legs.</b> For example GLD and SLV.');
    return;
  }

  state.custom = { num: n, den: d };
  state.lookback = null;
  el('lookback').value = tfConfig(state.timeframe).default_lookback_days;
  renderSegments();

  // Only keep symbols that actually returned data.
  if (await loadSeries()) rememberCustom(n, d);
}

function initTheme() {
  const stored = localStorage.getItem('rvc-theme');
  if (stored) document.documentElement.setAttribute('data-theme', stored);
  const label = () => {
    const dark = document.documentElement.getAttribute('data-theme') === 'dark' ||
      (!document.documentElement.getAttribute('data-theme') &&
        matchMedia('(prefers-color-scheme: dark)').matches);
    el('theme-toggle').textContent = dark ? 'Light' : 'Dark';
    return dark;
  };
  let dark = label();
  el('theme-toggle').addEventListener('click', () => {
    dark = !dark;
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    localStorage.setItem('rvc-theme', dark ? 'dark' : 'light');
    label();
    applyTheme();
  });

  // Follow the OS only while the user hasn't picked a side.
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (localStorage.getItem('rvc-theme')) return;
    dark = label();
    if (chart) applyTheme();
  });
}

/* ------------------------------------------------------------- screener */

const screener = {
  job: null,
  timeframe: '1D',
  rows: [],
  errors: [],
  earnings: null,        // {symbol: {...}} once looked up
  earningsState: 'idle', // idle | loading | ready | failed
  meta: null,
  timer: null,
  universe: null,
};

function scrEl(id) { return el(id); }

async function loadUniverseInfo() {
  try {
    const info = await (await fetch('/api/universe')).json();
    if (info.total === undefined) throw new Error(info.detail || 'unavailable');
    screener.universe = info;
    const mins = Math.max(1, Math.round(info.estimated_seconds / 60));
    const when = info.fetched_at ? new Date(info.fetched_at).toLocaleDateString() : 'unknown';
    scrEl('scr-sub').innerHTML =
      `${info.total} members (S&amp;P 500 ${info.counts.sp500}, Nasdaq-100 ${info.counts.nasdaq100}), ` +
      `list updated ${when}. A full scan is ~${info.total} requests — about ` +
      `${mins} min at ${info.requests_per_minute}/min.`;
  } catch (err) {
    scrEl('scr-sub').innerHTML =
      'Constituent list missing — run <code>python -m server.refresh_universe</code>.';
  }
}

function screenerIndices() {
  const wanted = [];
  if (scrEl('scr-sp500').checked) wanted.push('sp500');
  if (scrEl('scr-ndx').checked) wanted.push('nasdaq100');
  return wanted;
}

function setScanning(active) {
  scrEl('scr-run').hidden = active;
  scrEl('scr-cancel').hidden = !active;
  scrEl('scr-progress').hidden = !active;
}

function showScreenError(message) {
  const box = scrEl('scr-error');
  box.hidden = !message;
  if (message) box.innerHTML = message;
}

async function runScan() {
  const indices = screenerIndices();
  if (!indices.length) {
    showScreenError('<b>Pick at least one index.</b>');
    return;
  }
  showScreenError(null);
  screener.rows = [];
  screener.earnings = null;
  screener.earningsState = 'idle';
  scrEl('scr-results').innerHTML = '';

  const params = new URLSearchParams({
    timeframe: screener.timeframe,
    stat_window: scrEl('scr-stat').value,
    prefer_qqq: String(scrEl('scr-prefer').checked),
    indices: indices.join(','),
  });

  let job;
  try {
    const resp = await fetch(`/api/screen?${params}`, { method: 'POST' });
    job = await resp.json();
    if (!resp.ok) throw new Error(job.detail || job.error || `HTTP ${resp.status}`);
  } catch (err) {
    showScreenError(`<b>Could not start the scan.</b> ${err.message}`);
    return;
  }

  screener.job = job.id;
  setScanning(true);
  pollScan();
}

async function pollScan() {
  clearTimeout(screener.timer);
  if (!screener.job) return;

  let status;
  try {
    const resp = await fetch(`/api/screen/${screener.job}`);
    status = await resp.json();
    if (!resp.ok) {
      // The job is gone -- almost always a server restart mid-scan. Say so instead
      // of freezing the progress bar with no explanation.
      setScanning(false);
      screener.job = null;
      showScreenError(`<b>Scan lost.</b> ${status.detail || 'The server restarted; start the scan again.'}`);
      return;
    }
  } catch {
    screener.timer = setTimeout(pollScan, 3000);
    return;
  }

  screener.rows = status.rows || [];
  screener.meta = status.meta;
  screener.errors = status.errors || [];
  renderScanProgress(status);
  renderHits();

  if (status.state === 'running' || status.state === 'pending') {
    screener.timer = setTimeout(pollScan, 2000);
    return;
  }

  setScanning(false);
  screener.job = null;
  renderHits();  // triggers the earnings lookup for whatever is on screen
  if (status.state === 'failed') {
    showScreenError(`<b>Scan failed.</b> ${status.message || 'Unknown error.'}`);
  } else if (status.state === 'cancelled') {
    showScreenError('<b>Scan cancelled.</b> Partial results are shown below.');
  }
}

function renderScanProgress(status) {
  const { done, total, pct } = status.progress;
  scrEl('scr-bar').style.width = `${pct}%`;

  const parts = [`${status.phase} — ${done}/${total} (${pct}%)`];
  if (status.error_count) parts.push(`${status.error_count} skipped`);

  // A running scan with no hits yet is the normal case, not a stall -- say so,
  // and show an ETA so a slow scan does not read as a broken one.
  if (status.state === 'running' && done && done < total && status.started_at) {
    const elapsed = (Date.now() - Date.parse(status.started_at)) / 1000;
    const remaining = Math.round((elapsed / done) * (total - done));
    if (remaining > 2) parts.push(`~${remaining}s left`);
  }
  scrEl('scr-status').textContent = parts.join(' · ');
}

/* Names and sectors come from a third-party CSV via universe.json -- escape them
   before they go anywhere near innerHTML. */
function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* Earnings dates for whatever the scan surfaced. Fetched after the scan rather than
   during it: only the hits need annotating, and the first call may have to build the
   calendar (~20s), which should never hold up the scan itself. */
async function loadEarningsFor(symbols) {
  // Only the rows on screen need annotating -- asking for all ~513 scored names
  // would blow the endpoint's cap and is wasted work.
  const missing = symbols.filter((s) => !(screener.earnings || {})[s]);
  if (!missing.length || screener.earningsState === 'loading') return;

  screener.earningsState = 'loading';
  renderHits();
  try {
    const params = new URLSearchParams({ symbols: missing.slice(0, 400).join(',') });
    const resp = await fetch(`/api/earnings?${params}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    screener.earnings = { ...(screener.earnings || {}), ...data.symbols };
    screener.earningsCalendar = data.calendar;
    screener.earningsState = 'ready';
  } catch {
    screener.earningsState = 'failed';
  }
  renderHits();
}

function earningsCell(symbol) {
  if (screener.earningsState === 'loading') return '<span class="muted">…</span>';
  if (screener.earningsState === 'failed') return '<span class="muted">n/a</span>';

  const info = screener.earnings?.[symbol];
  if (!info) return '<span class="muted">—</span>';
  if (!info.scheduled) {
    return '<span class="muted" title="Nasdaq publishes confirmed dates about 8 weeks out; ' +
           'a company that just reported has no next date yet">not scheduled</span>';
  }

  const when = new Date(`${info.date}T00:00:00Z`).toLocaleDateString('en-US',
    { month: 'short', day: 'numeric', timeZone: 'UTC' });
  const soon = info.days_away <= 7 ? ' soon' : '';
  const tip = [
    info.fiscal_quarter ? `Q ending ${info.fiscal_quarter}` : '',
    info.eps_forecast ? `consensus EPS ${info.eps_forecast}` : '',
  ].filter(Boolean).join(' · ');

  return `<span class="earn${soon}" title="${tip}">${when}` +
    `<small>${info.days_away}d${info.when ? ` · ${info.when}` : ''}</small></span>`;
}

function renderHits() {
  const box = scrEl('scr-results');
  const rows = screener.rows;
  if (!rows.length) { box.innerHTML = ''; return; }

  const threshold = Math.abs(parseFloat(scrEl('scr-threshold').value) || 0);
  const hits = rows.filter((r) => Math.abs(r.z) >= threshold);
  const rich = hits.filter((r) => r.z > 0).length;
  const meta = screener.meta || {};

  const skipped = screener.errors || [];
  const skippedNote = skipped.length
    ? ` <span class="skipped">${skipped.length} skipped — ` +
      `${skipped.slice(0, 8).map((e) => esc(e.symbol)).join(', ')}` +
      `${skipped.length > 8 ? '…' : ''} (not recognised by Schwab).</span>`
    : '';

  const summary =
    `<p class="summary"><b>${hits.length}</b> of ${rows.length} scored beyond ` +
    `<b>±${threshold}σ</b> — ${rich} rich, ${hits.length - rich} cheap. ` +
    `${meta.timeframe_label || ''} bars, ${meta.lookback_days || '?'}-day lookback, ` +
    `mean/σ over ${meta.stat_window === 'full' ? 'the full range' : `the last ${meta.stat_window}`}. ` +
    `Click a row to chart it.${skippedNote}</p>`;

  if (!hits.length) {
    const scanning = Boolean(screener.job);
    box.innerHTML = summary + `<p class="empty">` + (scanning
      ? `No name past ±${threshold}σ yet — still scanning.`
      : `Nothing past ±${threshold}σ. Lower the threshold to see the widest names.`) +
      `</p>`;
    return;
  }

  // Benchmark is carried by colour AND by the "vs" column, never colour alone.
  const bySpy = hits.filter((r) => r.benchmark === 'SPY');
  const byQqq = hits.filter((r) => r.benchmark === 'QQQ');
  const key =
    `<p class="bench-key">` +
    `<span><i style="background:var(--series-1)"></i>compared with <b>SPY</b> — ${bySpy.length}</span>` +
    `<span><i style="background:var(--series-2)"></i>compared with <b>QQQ</b> — ${byQqq.length}</span>` +
    `</p>`;

  const body = hits.map((r) => {
    const arrow = r.z > 0 ? '▲' : '▼';
    return `<tr class="bench-${r.benchmark}" data-num="${esc(r.symbol)}" data-den="${esc(r.benchmark)}">` +
      `<td><span class="ticker">${esc(r.symbol)}</span></td>` +
      `<td class="name" title="${esc(r.name)}">${esc(r.name)}</td>` +
      `<td class="sector">${esc(r.sector) || '—'}</td>` +
      `<td>${esc(r.benchmark)}</td>` +
      `<td class="num">${r.z > 0 ? '+' : ''}${r.z.toFixed(2)}</td>` +
      `<td class="num">${r.pct_from_mean >= 0 ? '+' : ''}${r.pct_from_mean.toFixed(1)}%</td>` +
      `<td>${arrow} ${r.direction}</td>` +
      `<td class="earn-cell">${earningsCell(r.symbol)}</td>` +
      `<td class="num">${r.n}</td></tr>`;
  }).join('');

  // A plain, copyable ticker list -- the thing you actually take away from a scan.
  const listFor = (label, group) => group.length
    ? `<div class="bench-${label}"><b>vs ${label}:</b> ` +
      group.map((r) => `<code>${esc(r.symbol)}</code>`).join(' ') + `</div>`
    : '';

  box.innerHTML = summary + key +
    `<div class="hits"><table class="hit-table">` +
    `<thead><tr><th>Ticker</th><th>Company</th><th>Sector</th><th>vs</th>` +
    `<th>z</th><th>vs mean</th><th>Read</th><th>Next earnings</th><th>Bars</th></tr></thead>` +
    `<tbody>${body}</tbody></table></div>` +
    `<div class="ticker-list">${listFor('SPY', bySpy)}${listFor('QQQ', byQqq)}</div>`;

  // Lazily annotate whatever is displayed; a lower threshold reveals more rows.
  if (!screener.job) loadEarningsFor(hits.map((r) => r.symbol));

  for (const tr of box.querySelectorAll('tbody tr')) {
    tr.addEventListener('click', () => {
      // Chart it on the settings it was scored with, so the z on screen is the z
      // in the row -- a different timeframe would show a different number.
      if (meta.timeframe && meta.timeframe !== state.timeframe) {
        state.timeframe = meta.timeframe;
        state.lookback = null;
        el('lookback').value = tfConfig(state.timeframe).default_lookback_days;
        el('lookback').max = tfConfig(state.timeframe).max_lookback_days;
        renderSegments();
      }
      if (meta.stat_window && meta.stat_window !== state.statWindow) {
        state.statWindow = meta.stat_window;
        el('stat-window').value = meta.stat_window;
      }
      plotCustom(tr.dataset.num, tr.dataset.den);
      document.querySelector('.chart-card').scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }
}

function renderScreenerSegments() {
  segment(
    scrEl('scr-tf-seg'),
    state.config.timeframes.map((t) => ({ key: t.key, label: t.key, title: t.label })),
    screener.timeframe,
    (key) => { screener.timeframe = key; renderScreenerSegments(); },
  );
}

function initScreener() {
  renderScreenerSegments();
  scrEl('scr-run').addEventListener('click', runScan);
  scrEl('scr-cancel').addEventListener('click', async () => {
    if (!screener.job) return;
    await fetch(`/api/screen/${screener.job}`, { method: 'DELETE' });
  });
  scrEl('scr-threshold').addEventListener('input', renderHits);
  loadUniverseInfo();
}

async function main() {
  // Theme first: buildChart reads the CSS custom properties, so the stored
  // data-theme has to be on the document before the chart exists.
  initTheme();
  buildChart();

  let config;
  try {
    config = await (await fetch('/api/config')).json();
  } catch (err) {
    showError('<b>Server unreachable.</b> Is <code>python -m server</code> running?');
    return;
  }
  state.config = config;
  state.pair = config.defaults.pair;
  state.timeframe = config.defaults.timeframe;

  const tf = tfConfig(state.timeframe);
  el('lookback').value = tf.default_lookback_days;
  el('lookback').max = tf.max_lookback_days;

  renderSegments();
  initScreener();

  if (config.demo) {
    state.demoNotice = 'Demo mode (<code>RVC_DEMO=1</code>) — bars are synthetic, not market data. ' +
      'Unset RVC_DEMO and run <code>python -m server.login</code> for live Schwab data.';
  }

  if (!config.auth.ready) {
    const why = config.auth.credentials_configured
      ? 'No valid Schwab refresh token — they expire 7 days after each login.'
      : 'SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set.';
    showError(describeError(why, 'auth'));
  }

  renderRecents();
  const submitCustom = (e) => {
    e.preventDefault();
    plotCustom(el('custom-num').value, el('custom-den').value);
  };
  el('custom-row').addEventListener('submit', submitCustom);
  // Explicit Enter handling: implicit form submission does not fire reliably here,
  // and preventDefault keeps it from double-firing where it does.
  for (const id of ['custom-num', 'custom-den']) {
    el(id).addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submitCustom(e);
    });
  }

  el('refresh').addEventListener('click', () => loadSeries());
  el('stat-window').addEventListener('change', (e) => {
    state.statWindow = e.target.value;
    loadSeries({ preserveView: true });
  });
  el('extended').addEventListener('change', (e) => {
    state.extended = e.target.checked;
    loadSeries();
  });
  el('live').addEventListener('change', (e) => {
    state.live = e.target.checked;
    if (!state.live) el('live-stamp').textContent = '';
  });
  let lookbackTimer;
  el('lookback').addEventListener('input', (e) => {
    clearTimeout(lookbackTimer);
    lookbackTimer = setTimeout(() => {
      const v = parseInt(e.target.value, 10);
      state.lookback = Number.isFinite(v) && v > 0 ? v : null;
      loadSeries();
    }, 500);
  });

  await loadSeries();

  setInterval(pollQuote, Math.max(2, config.quote_poll_seconds) * 1000);
  setInterval(() => { if (state.live) loadSeries({ preserveView: true }); }, REFRESH_MS);
}

main();
