// Plain-node unit test for the /genres browser logic (static/js/genres.js).
// Run with: node tests/test_genres_page.js
//
// The fourth htmx-migrated filter page, and the one carrying the most that htmx
// could not absorb. Three behaviours here are the kind that fail quietly:
//
//   * readIsland swallows a JSON parse error and KEEPS the previous datasets.
//     Clearing them instead would paint an empty chart, which reads as "you
//     listened to nothing" rather than "the fragment was truncated".
//   * absorbDetailData writes the RESOLVED genre back into the form's hidden
//     field. Without it, changing the time period silently resets the page to
//     that range's top genre - the drill-down has no visible control in the
//     form to carry it.
//   * the afterSwap listener is scoped to two ids, because htmx fires afterSwap
//     on the target AND on every top-level element it inserted. Unguarded, one
//     swap repaints every canvas several times.
//
// The donut's "Other" fold is covered too: it is the one place this file does
// arithmetic, and an off-by-one in the slice cap silently drops a genre.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'genres.js');
const RANGE_OK = null;                //< see tests/test_top_list_page.js
const RANGE_INVERTED = 'inverted';
const PALETTE = ['#1', '#2', '#3', '#4', '#5', '#6'];

function makeElement(extra) {
  const classes = new Set((extra && extra.classes) || []);
  return Object.assign({
    id: '', value: '', style: {}, textContent: '', innerHTML: null,
    classList: { contains: (n) => classes.has(n), add: (n) => classes.add(n) },
  }, extra || {});
}

function makeChartUtils(calls) {
  return {
    PALETTE,
    refreshPalette() { calls.palettes += 1; },
    escapeHtml(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;'); },
    renderHorizontalBars(canvas, pairs, opts) { calls.bars.push({ canvas, pairs, opts }); },
    drawDonutChart(canvas, slices, total) { calls.donuts.push({ canvas, slices, total }); },
    renderMultiLineChart(canvas, legend, data) { calls.lines.push({ canvas, legend, data }); },
    renderHeatmap(canvas, data) { calls.heatmaps.push({ canvas, data }); },
  };
}

function loadGenres(options) {
  options = options || {};
  const calls = {
    palettes: 0, bars: [], donuts: [], lines: [], heatmaps: [],
    ajax: [], syncedRanges: [], shownErrors: [], pruned: [],
    bodyListeners: {}, windowListeners: {}, timeouts: [], banners: 0, cleared: 0,
  };
  const elements = options.elements || {};

  global.window = {
    location: { pathname: '/genres', search: options.search || '' },
    ChartUtils: options.noChartUtils ? undefined : makeChartUtils(calls),
    __genreData: options.seedData,
    AjaxStatus: options.noAjaxStatus ? undefined : {
      showBanner(retry) { calls.banners += 1; calls.lastRetry = retry; },
      clearBanner() { calls.cleared += 1; },
    },
    addEventListener(type, fn) { calls.windowListeners[type] = fn; },
  };
  global.document = {
    getElementById(id) { return elements[id] || null; },
    body: { addEventListener(type, fn) { calls.bodyListeners[type] = fn; } },
  };
  global.htmx = { ajax(method, url, opts) { calls.ajax.push({ method, url, opts }); } };
  global.HtmxFilters = {
    RANGE_OK,
    syncCustomRange(containerId) { calls.syncedRanges.push(containerId); },
    hidesTrendBuckets(interval) { return interval === 'today'; },
    rangeProblemFromDom() { return options.rangeProblem === undefined ? RANGE_OK : options.rangeProblem; },
    showRangeError(problem) { calls.shownErrors.push(problem); },
    pruneEmptyParams(parameters) { calls.pruned.push(parameters); },
  };
  global.setTimeout = function (fn, ms) { calls.timeouts.push({ fn, ms }); return calls.timeouts.length; };
  global.clearTimeout = function () {};

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  calls.window = global.window;
  return calls;
}

function island(text) { return makeElement({ textContent: text }); }

function afterSwap(page, targetId) {
  page.bodyListeners['htmx:afterSwap']({ target: { id: targetId } });
}

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// ------------------------------------------------------------ the CU guard

run('a page without ChartUtils wires up nothing at all', () => {
  const page = loadGenres({ noChartUtils: true });

  assert.deepStrictEqual(Object.keys(page.bodyListeners), [],
                         'there is nothing to draw on, so nothing should be listening');
});

// ------------------------------------------------------- absorbing islands

const OVERVIEW = JSON.stringify({
  distributionPairs: [['jazz', 10], ['lofi', 5]],
  breadthPairs: [['jazz', 3]],
  mixTrend: { buckets: ['a'], series: [{ name: 'jazz' }] },
});

run('a results swap reads the overview island into the chart data', () => {
  const page = loadGenres({ elements: { 'genres-overview-data': island(OVERVIEW) } });

  afterSwap(page, 'genresResults');

  assert.deepStrictEqual(page.window.__genreData.distributionPairs, [['jazz', 10], ['lofi', 5]]);
  assert.deepStrictEqual(page.window.__genreData.breadthPairs, [['jazz', 3]]);
});

run('a truncated island leaves the previous data standing', () => {
  const page = loadGenres({
    seedData: { distributionPairs: [['prior', 99]] },
    elements: { 'genres-overview-data': island('{"distributionPairs": [[') },
  });

  afterSwap(page, 'genresResults');

  assert.deepStrictEqual(page.window.__genreData.distributionPairs, [['prior', 99]],
                         'an empty chart would read as "you listened to nothing"');
});

run('the resolved genre is written back into the form, so the period change keeps it', () => {
  const field = makeElement();
  const page = loadGenres({
    elements: {
      'genres-detail-data': island(JSON.stringify({ genre: 'shoegaze', selectedTrend: {}, clock: [] })),
      genresSelectedGenre: field,
    },
  });

  afterSwap(page, 'genreExplore');

  assert.strictEqual(field.value, 'shoegaze');
});

run('a detail island with no genre clears the field rather than leaving a stale one', () => {
  const field = makeElement({ value: 'shoegaze' });
  const page = loadGenres({
    elements: {
      'genres-detail-data': island(JSON.stringify({ selectedTrend: {}, clock: [] })),
      genresSelectedGenre: field,
    },
  });

  afterSwap(page, 'genreExplore');

  assert.strictEqual(field.value, '');
});

// ------------------------------------------------------- afterSwap scoping

run('a chip swap redraws the drill-down but not the overview', () => {
  const page = loadGenres({
    elements: {
      'genres-overview-data': island(OVERVIEW),
      'genres-detail-data': island(JSON.stringify({ genre: 'jazz', selectedTrend: {}, clock: [] })),
    },
  });

  afterSwap(page, 'genreExplore');

  assert.strictEqual(page.window.__genreData.distributionPairs, undefined,
                     'a genre switch does not change the overview datasets');
  assert.strictEqual(page.heatmaps.length, 1, 'but it does repaint the clock');
});

run('a swap of an unrelated region repaints nothing', () => {
  const page = loadGenres({ elements: { 'genres-overview-data': island(OVERVIEW) } });

  afterSwap(page, 'someOtherRegion');

  assert.strictEqual(page.palettes, 0);
  assert.deepStrictEqual(page.bars, [], 'htmx fires afterSwap per inserted element - this must not multiply');
});

run('a landed swap clears whatever error banner was up', () => {
  const page = loadGenres({ elements: { 'genres-overview-data': island(OVERVIEW) } });

  afterSwap(page, 'genresResults');

  assert.strictEqual(page.cleared, 1);
});

// ------------------------------------------------------------- the donut

function donutFor(pairs) {
  const legend = makeElement();
  const page = loadGenres({
    elements: {
      'genres-overview-data': island(JSON.stringify({ distributionPairs: pairs, breadthPairs: [], mixTrend: {} })),
      genreShareChart: makeElement(),
      genreShareLegend: legend,
    },
  });
  afterSwap(page, 'genresResults');
  page.legend = legend;
  return page;
}

run('the donut folds everything past the palette into one Other slice', () => {
  //< PALETTE holds 6, so 5 are coloured and the rest collapse
  const pairs = [['a', 10], ['b', 9], ['c', 8], ['d', 7], ['e', 6], ['f', 5], ['g', 4]];
  const page = donutFor(pairs);

  const slices = page.donuts[0].slices;
  assert.strictEqual(slices.length, 6, '5 coloured + Other');
  assert.strictEqual(slices[5].label, 'Other');
  assert.strictEqual(slices[5].value, 9, 'f + g');
  assert.strictEqual(page.donuts[0].total, 49, 'the total is every genre, not just the shown ones');
});

run('a genre count that fits the palette gets no Other slice', () => {
  const page = donutFor([['a', 10], ['b', 5]]);

  const slices = page.donuts[0].slices;
  assert.strictEqual(slices.length, 2);
  assert.ok(!slices.some(s => s.label === 'Other'));
});

run('a genre name is escaped before it is spliced into the legend', () => {
  //< the legend is the one place this file builds an innerHTML string, and
  //  genre names arrive from Last.fm, not from a controlled vocabulary
  const page = donutFor([['<img src=x onerror=alert(1)>', 5]]);

  assert.ok(!page.legend.innerHTML.includes('<img'), page.legend.innerHTML);
  assert.ok(page.legend.innerHTML.includes('&lt;img'), page.legend.innerHTML);
});

run('an empty genre set leaves the legend blank rather than half-built', () => {
  const legend = makeElement();
  const page = loadGenres({
    elements: {
      'genres-overview-data': island(JSON.stringify({ distributionPairs: [], breadthPairs: [], mixTrend: {} })),
      genreShareChart: makeElement(),
      genreShareLegend: legend,
    },
  });

  afterSwap(page, 'genresResults');

  assert.strictEqual(legend.innerHTML, '');
});

// ---------------------------------------------------------- the chip veto

function configRequest(page, elt, parameters) {
  const evt = {
    detail: { elt, parameters: parameters || {} },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  page.bodyListeners['htmx:configRequest'](evt);
  return evt;
}

run('clicking the chip already showing its genre re-fetches nothing', () => {
  const page = loadGenres({});

  const evt = configRequest(page, makeElement({ classes: ['genre-chip', 'selected'] }));

  assert.strictEqual(evt.prevented, 1);
});

run('clicking a different chip is allowed, and skips the range check entirely', () => {
  const page = loadGenres({ rangeProblem: RANGE_INVERTED });

  const evt = configRequest(page, makeElement({ classes: ['genre-chip'] }));

  assert.strictEqual(evt.prevented, 0);
  assert.deepStrictEqual(page.shownErrors, [],
                         'a boosted chip carries its own query; the form state is irrelevant');
});

run('an inverted range still stops the FILTER form', () => {
  const page = loadGenres({ rangeProblem: RANGE_INVERTED });

  const evt = configRequest(page, { id: 'genresFilters' });

  assert.strictEqual(evt.prevented, 1);
  assert.deepStrictEqual(page.shownErrors, [RANGE_INVERTED]);
});

run('a valid range lets the form through and prunes its empty params', () => {
  const page = loadGenres({ rangeProblem: RANGE_OK });
  const parameters = { groupBy: '' };

  const evt = configRequest(page, { id: 'genresFilters' }, parameters);

  assert.strictEqual(evt.prevented, 0);
  assert.deepStrictEqual(page.pruned, [parameters]);
});

run('a request with no originating element is ignored', () => {
  const page = loadGenres({ rangeProblem: RANGE_INVERTED });

  const evt = configRequest(page, null);

  assert.strictEqual(evt.prevented, 0);
});

// ------------------------------------------------------- interval + errors

run('a single-day range hides the Trend-buckets select here too', () => {
  const groupBy = makeElement();
  const page = loadGenres({
    elements: { groupByContainer: groupBy, interval: makeElement({ value: 'today' }) },
  });

  page.window.updateGenresIntervalFilter();

  assert.strictEqual(groupBy.style.display, 'none');
  assert.deepStrictEqual(page.syncedRanges, ['genresCustomDates']);
});

run('a failed swap retries the request that actually failed', () => {
  const target = makeElement({ id: 'genreExplore' });
  const page = loadGenres({});

  page.bodyListeners['htmx:responseError']({
    detail: { target, pathInfo: { finalRequestPath: '/genres?genre=jazz' } },
  });
  page.lastRetry();

  assert.strictEqual(page.banners, 1);
  assert.strictEqual(page.ajax[0].url, '/genres?genre=jazz');
  assert.strictEqual(page.ajax[0].opts.target, target, 'not always the overview region');
});

run('a send error with no path info falls back to the current URL', () => {
  const page = loadGenres({ search: '?interval=last-30-days', elements: { genresResults: makeElement() } });

  page.bodyListeners['htmx:sendError']({ detail: {} });
  page.lastRetry();

  assert.strictEqual(page.ajax[0].url, '/genres?interval=last-30-days');
});

// ------------------------------------------------------------- repaints

run('a resize is coalesced into one debounced repaint', () => {
  const page = loadGenres({});

  page.windowListeners.resize();
  page.windowListeners.resize();

  assert.deepStrictEqual(page.timeouts.map(t => t.ms), [150, 150]);
});

run('a theme change repaints after letting the new CSS variables land', () => {
  const selector = makeElement();
  selector.addEventListener = function (type, fn) { this.handlers = { [type]: fn }; };
  const page = loadGenres({ elements: { 'theme-selector': selector } });

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
  console.log(`all ${results.length} genres tests passed`);
})();
