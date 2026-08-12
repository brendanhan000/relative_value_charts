/* Relative value ratio charts — Schwab price history, mean and 1/2/3σ levels. */

const LWC = window.LightweightCharts;

const BUCKET_SECONDS = { '5m': 300, '15m': 900, '1H': 3600, '4H': 14400, '1D': 86400 };
const REFRESH_MS = 60_000;
const RIGHT_OFFSET = 4; // blank slots kept to the right of the last point
const MIN_BAR_SPACING = 0.05; // low enough that "show everything" always fits

const state = {
  config: null,
  pair: null,
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

async function loadSeries({ preserveView = false } = {}) {
  const token = ++state.loadToken;
  el('loading').hidden = false;
  el('live-stamp').textContent = '';

  const params = new URLSearchParams({
    pair: state.pair,
    timeframe: state.timeframe,
    stat_window: state.statWindow,
    extended: String(state.extended),
  });
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
    if (token !== state.loadToken) return;
    el('loading').hidden = true;
    showError(describeError(err.message, err.kind));
    return;
  }
  if (token !== state.loadToken) return;

  showError(null);
  showNotice([...(state.demoNotice ? [state.demoNotice] : []), ...payload.notes]);

  state.series = payload;
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
  return `<b>Could not load the series.</b> ${message}`;
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
  // a pair/timeframe switch, state.pair no longer describes what is on screen.
  if (state.series.pair.key !== state.pair || state.series.timeframe.key !== state.timeframe) return;
  let data;
  try {
    const resp = await fetch(`/api/quote?pair=${encodeURIComponent(state.pair)}`);
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

function renderSegments() {
  segment(
    el('pair-seg'),
    state.config.pairs.map((p) => ({ key: p.key, label: p.label, title: p.description })),
    state.pair,
    (key) => { state.pair = key; renderSegments(); loadSeries(); },
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
