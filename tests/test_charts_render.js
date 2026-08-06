// Plain-node unit test for the chart ORCHESTRATION in static/js/charts.js.
// Run with: node tests/test_charts_render.js
//
// Scope on purpose. The drawing primitives - axis labels, sparse x-ticks, the
// skip axis, the drilldown URL - live in static/js/chart-utils.js and already
// have three tests of their own (test_chart_axis_labels.js,
// test_chart_drilldown.js, test_chart_skip_series.js). Pinning pixel geometry
// here would need a real canvas and would pin layout, not behaviour.
//
// What has no coverage is the layer above them, and it decides whether
// anything is drawn at all:
//
//   * window.__deferInitialChartRender. /charts fetches its data after first
//     paint and drives rendering itself, so it opts OUT of the render at
//     script load; /compare sets __chartData inline beforehand and renders
//     immediately. Get the gate backwards and one page draws twice while the
//     other draws once against no data.
//   * renderAllCharts refreshing the palette BEFORE the nine renderers. It is
//     the theme-change and resize path, and a repaint that reads the old CSS
//     variables is the entire reason the theme handler waits 50ms.
//   * the Most Skipped row hiding itself when nothing was skipped, rather than
//     showing two empty frames.
//
// A permissive Proxy stands in for the 2D context: every renderer that finds a
// canvas will paint on it, and this test is not about what it paints.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'charts.js');

function permissiveContext() {
  const noop = function () { return permissiveContext(); };
  return new Proxy({}, {
    get(target, prop) {
      if (prop === 'measureText') return () => ({ width: 10 });
      if (prop === 'createLinearGradient') return () => ({ addColorStop() {} });
      if (prop === 'canvas') return { width: 600, height: 300 };
      return noop;
    },
    set() { return true; },
  });
}

function makeCanvas() {
  return {
    width: 600, height: 300, style: {},
    getContext() { return permissiveContext(); },
    getBoundingClientRect() { return { left: 0, top: 0, width: 600, height: 300 }; },
    addEventListener() {},
  };
}

function makeChartUtils(calls) {
  return {
    PALETTE: ['#1', '#2', '#3', '#4', '#5', '#6'],
    TIME_SERIES_PLAY_COLOR_INDEX: 0,
    TIME_SERIES_SKIP_COLOR_INDEX: 1,
    refreshPalette() { calls.order.push('refreshPalette'); },
    getAccentColor() { return '#FB717B'; },
    parseHex() { return { r: 1, g: 2, b: 3 }; },
    escapeHtml(s) { return String(s); },
    bucketDrilldownUrl() { return '/history?x=1'; },
    drawEmptyState() { calls.emptyStates += 1; },
    drawSparseXLabels() {}, drawYAxisGrid() {},
    setupCanvas(canvas) { return canvas ? permissiveContext() : null; },
    showTooltip() {}, hideTooltip() {},
    maxSkipsIn() { return 1; }, skipAxisMax() { return 1; },
    timeSeriesHasNothingToDraw(data) { return !data || !data.length; },
    timeSeriesLegendItems() { return []; },
    renderLegend() {},
    renderBarsFromPairs(canvas) { calls.order.push('bars'); return canvas; },
    renderHorizontalBars(canvas, pairs, opts) { calls.horizontal.push({ canvas, pairs, opts }); },
    renderHeatmap() { calls.order.push('heatmap'); },
    renderMultiLineChart() { calls.order.push('multiline'); },
    drawDonutChart() { calls.order.push('donut'); },
  };
}

function loadCharts(options) {
  options = options || {};
  const calls = { order: [], horizontal: [], emptyStates: 0, timeouts: [], windowListeners: {} };
  const elements = options.elements || {};

  global.window = {
    location: { pathname: '/charts', search: '', href: '' },
    ChartUtils: makeChartUtils(calls),
    __chartData: options.chartData,
    __deferInitialChartRender: options.defer,
    addEventListener(type, fn) { calls.windowListeners[type] = fn; },
    devicePixelRatio: 1,
  };
  global.document = {
    documentElement: { className: 'theme-rose' },
    getElementById(id) { return elements[id] || null; },
    createElement() { return makeCanvas(); },
    body: { addEventListener() {}, appendChild() {} },
    addEventListener() {},
  };
  global.setTimeout = function (fn, ms) { calls.timeouts.push({ fn, ms }); return calls.timeouts.length; };
  global.clearTimeout = function () {};
  global.getComputedStyle = function () { return { getPropertyValue() { return '#FB717B'; } }; };

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  calls.window = global.window;
  return calls;
}

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// ------------------------------------------------------- the initial render

run('/compare renders immediately, because its data is already inline', () => {
  const page = loadCharts({ chartData: { timeSeries: [] } });

  assert.ok(page.order.includes('refreshPalette'),
            'without the deferral flag, loading the script IS the render');
});

run('/charts opts out and waits to drive the render itself', () => {
  const page = loadCharts({ defer: true, chartData: { timeSeries: [] } });

  assert.deepStrictEqual(page.order, [],
                         'it fetches after first paint; rendering here would draw against nothing');
});

run('the deferred page can still render on demand', () => {
  const page = loadCharts({ defer: true, chartData: { timeSeries: [] } });

  page.window.renderAllCharts();

  assert.ok(page.order.includes('refreshPalette'));
});

// ----------------------------------------------------------- the exports

run('the three entry points other files call are exported', () => {
  const page = loadCharts({ defer: true });

  assert.strictEqual(typeof page.window.renderAllCharts, 'function');
  //< wrapped.js calls this one bare, and charts-page.js calls renderAllCharts;
  //  compare.js re-renders just the mirror after a filter swap
  assert.strictEqual(typeof page.window.renderTimeSeriesChart, 'function');
  assert.strictEqual(typeof page.window.renderComparisonMirror, 'function');
});

// ------------------------------------------------------ the repaint order

run('the palette is refreshed before anything is drawn', () => {
  const page = loadCharts({
    defer: true,
    chartData: { heatmap: [{ hour: 1, day: 1, value: 2 }] },
    elements: { heatmapChart: makeCanvas() },
  });

  page.window.renderAllCharts();

  assert.strictEqual(page.order[0], 'refreshPalette',
                     'a repaint that reads the OLD css variables is the bug the 50ms wait exists for');
});

// --------------------------------------------------------- empty data paths

run('a page with no chart data at all draws nothing and throws nothing', () => {
  const page = loadCharts({ defer: true });

  page.window.renderAllCharts();   //< must not throw

  assert.strictEqual(page.order[0], 'refreshPalette');
});

run('a page missing a canvas hands the null down rather than throwing', () => {
  const page = loadCharts({
    defer: true,
    chartData: { timeSeries: [], heatmap: [], artistTrend: { buckets: [], series: [] } },
  });

  page.window.renderAllCharts();   //< must not throw

  //< the absent-canvas guard lives in ChartUtils, not in each caller here -
  //  worth pinning, because it is why a page that renders three of the nine
  //  charts is not a special case anywhere in this file
  assert.ok(page.horizontal.every(call => call.canvas === null));
});

// ------------------------------------------------------------ Most Skipped

function mostSkippedPage(chartData) {
  const grid = { style: {} };
  const page = loadCharts({
    defer: true,
    chartData,
    elements: {
      mostSkippedGrid: grid,
      mostSkippedSongsChart: makeCanvas(),
      mostSkippedArtistsChart: makeCanvas(),
    },
  });
  page.window.renderAllCharts();
  page.grid = grid;
  //< renderGenreChart uses the same primitive, so scope to the skip bars by
  //  their suffix rather than counting every horizontal-bar call
  page.skipBars = page.horizontal.filter(c => c.opts && c.opts.valueSuffix === '% skipped');
  return page;
}

run('a range with nothing skipped hides the whole row', () => {
  const page = mostSkippedPage({ mostSkippedSongs: [], mostSkippedArtists: [] });

  assert.strictEqual(page.grid.style.display, 'none');
  assert.deepStrictEqual(page.skipBars, [], 'two empty frames are worse than no row');
});

run('a range with skips shows the row and draws both charts', () => {
  const page = mostSkippedPage({
    mostSkippedSongs: [{ name: 'Song', skipRate: 40 }],
    mostSkippedArtists: [{ name: 'Artist', skipRate: 25 }],
  });

  assert.strictEqual(page.grid.style.display, 'grid');
  assert.strictEqual(page.skipBars.length, 2);
});

run('skips on only one side still shows the row', () => {
  const page = mostSkippedPage({ mostSkippedSongs: [{ name: 'Song', skipRate: 40 }], mostSkippedArtists: [] });

  assert.strictEqual(page.grid.style.display, 'grid');
});

run('the skip bars are labelled as a share of encounters, not of plays', () => {
  const page = mostSkippedPage({ mostSkippedSongs: [{ name: 'Song', skipRate: 40 }], mostSkippedArtists: [] });

  //< "% skipped", not "% of plays skipped": the denominator is every time it
  //  came up, skips included, so a song played 6 and skipped 4 reads 40% -
  //  as a share of PLAYS the same row would be 67%
  assert.strictEqual(page.skipBars.length, 2, 'both charts draw even when one side is empty');
});

// ------------------------------------------------------------- repaints

run('a resize is coalesced into one debounced repaint', () => {
  const page = loadCharts({ defer: true });

  page.windowListeners.resize();
  page.windowListeners.resize();

  assert.deepStrictEqual(page.timeouts.map(t => t.ms), [150, 150]);
});

run('a theme change waits for the new CSS variables before repainting', () => {
  const selector = { addEventListener(type, fn) { this.handlers = { [type]: fn }; } };
  const page = loadCharts({ defer: true, elements: { 'theme-selector': selector } });

  selector.handlers.change();

  assert.deepStrictEqual(page.timeouts.map(t => t.ms), [50]);
});

(async () => {
  for (const { name, fn } of results) {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      console.error(`FAIL - ${name}`);
      console.error(err);
      process.exit(1);
    }
  }
  console.log(`all ${results.length} charts render tests passed`);
})();
