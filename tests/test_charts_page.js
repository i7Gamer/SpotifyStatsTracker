// Plain-node unit test for the /charts browser logic (static/js/charts-page.js).
// Run with: node tests/test_charts_page.js
//
// The third of the htmx-migrated filter pages (see tests/test_history_page.js
// and tests/test_top_list_page.js). What is unique here is the redraw: a
// <canvas> is not markup, so htmx cannot swap it - the series ride along inside
// the swapped card as a JSON island and this file re-reads them afterwards.
// That listener is the reason the file still exists, and it has three ways to
// be wrong that all look like "the charts went blank":
//   * firing for a swap of some OTHER target and stamping window.__chartData
//     with whatever that target happened to contain
//   * not firing at all, leaving the new markup under the old picture
//   * skipping the redraw when a card arrives with no island
//
// Also pinned: the Trend-buckets select is HIDDEN, not disabled, for a
// single-day range - its value has to survive switching back to a multi-day
// one, which a disabled control would not serialize.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'charts-page.js');
const RANGE_OK = null;               //< see tests/test_top_list_page.js
const RANGE_INVERTED = 'inverted';

function makeElement(extra) {
  return Object.assign({ value: '', style: {}, textContent: '' }, extra || {});
}

function loadCharts(options) {
  options = options || {};
  const calls = {
    ajax: [], syncedRanges: [], shownErrors: [], pruned: [],
    swapFailure: null, bodyListeners: {}, redraws: 0,
  };
  const elements = options.elements || {};

  global.window = {
    location: { pathname: '/charts', search: options.search || '' },
    renderAllCharts: options.noRenderer ? undefined : function () { calls.redraws += 1; },
  };
  global.document = {
    getElementById(id) { return elements[id] || null; },
    body: { addEventListener(type, fn) { calls.bodyListeners[type] = fn; } },
  };
  global.htmx = { ajax(method, url, opts) { calls.ajax.push({ method, url, opts }); } };
  global.HtmxFilters = {
    RANGE_OK,
    syncCustomRange(containerId) { calls.syncedRanges.push(containerId); },
    hidesTrendBuckets(interval) { return interval === 'today' || interval === 'yesterday'; },
    rangeProblemFromDom() { return options.rangeProblem === undefined ? RANGE_OK : options.rangeProblem; },
    showRangeError(problem) { calls.shownErrors.push(problem); },
    pruneEmptyParams(parameters) { calls.pruned.push(parameters); },
    onSwapFailure(targetId, retry) { calls.swapFailure = { targetId, retry }; },
  };

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  calls.window = global.window;
  return calls;
}

function swapTarget(id, islandText) {
  return {
    id,
    querySelector(selector) {
      if (selector !== '#chartsData' || islandText === undefined) return null;
      return { textContent: islandText };
    },
  };
}

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// ------------------------------------------------------ the interval filter

run('a single-day range hides the Trend-buckets select', () => {
  const groupBy = makeElement();
  const page = loadCharts({ elements: { groupByContainer: groupBy, interval: makeElement({ value: 'today' }) } });

  page.window.updateChartsIntervalFilter();

  assert.strictEqual(groupBy.style.display, 'none');
});

run('a multi-day range shows it again', () => {
  const groupBy = makeElement({ style: { display: 'none' } });
  const page = loadCharts({
    elements: { groupByContainer: groupBy, interval: makeElement({ value: 'last-30-days' }) },
  });

  page.window.updateChartsIntervalFilter();

  assert.strictEqual(groupBy.style.display, 'flex');
});

run('the interval also syncs the charts custom-range container', () => {
  const page = loadCharts({
    elements: { groupByContainer: makeElement(), interval: makeElement({ value: 'last-30-days' }) },
  });

  page.window.updateChartsIntervalFilter();

  assert.deepStrictEqual(page.syncedRanges, ['chartsCustomDates'],
                         'each page owns a differently-named container');
});

// -------------------------------------------------------- the request veto

function configRequest(page, elementId, parameters) {
  const evt = {
    detail: { elt: elementId === null ? null : { id: elementId }, parameters: parameters || {} },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  page.bodyListeners['htmx:configRequest'](evt);
  return evt;
}

run('an inverted range stops the charts request', () => {
  const page = loadCharts({ rangeProblem: RANGE_INVERTED });

  const evt = configRequest(page, 'chartsFilters');

  assert.strictEqual(evt.prevented, 1);
  assert.deepStrictEqual(page.shownErrors, [RANGE_INVERTED]);
  assert.deepStrictEqual(page.pruned, []);
});

run('a valid range prunes Auto out of the query, keeping auto mode auto', () => {
  const page = loadCharts({ rangeProblem: RANGE_OK });
  const parameters = { groupBy: '', interval: 'last-30-days' };

  const evt = configRequest(page, 'chartsFilters', parameters);

  assert.strictEqual(evt.prevented, 0);
  assert.deepStrictEqual(page.pruned, [parameters],
                         'a pinned empty groupBy would freeze the derived bucket');
});

run('a request from anything but the charts form is left alone', () => {
  const page = loadCharts({ rangeProblem: RANGE_INVERTED });

  const evt = configRequest(page, 'someBoostedLink');

  assert.strictEqual(evt.prevented, 0);
  assert.deepStrictEqual(page.shownErrors, []);
});

// ------------------------------------------------------------- the redraw

run('a swapped charts card re-reads its data island and redraws', () => {
  const page = loadCharts({});

  page.bodyListeners['htmx:afterSwap']({ target: swapTarget('chartsCard', '{"plays":[1,2,3]}') });

  assert.deepStrictEqual(page.window.__chartData, { plays: [1, 2, 3] });
  assert.strictEqual(page.redraws, 1);
});

run('a swap of some other target never touches the chart data', () => {
  const page = loadCharts({});

  page.bodyListeners['htmx:afterSwap']({ target: swapTarget('somethingElse', '{"plays":[9]}') });

  assert.strictEqual(page.window.__chartData, undefined,
                     'another region swapping must not stamp the charts with its contents');
  assert.strictEqual(page.redraws, 0);
});

run('a card that arrives with no island still redraws, rather than going blank', () => {
  const page = loadCharts({});
  page.window.__chartData = { plays: [7] };

  page.bodyListeners['htmx:afterSwap']({ target: swapTarget('chartsCard', undefined) });

  assert.deepStrictEqual(page.window.__chartData, { plays: [7] }, 'the previous series survive');
  assert.strictEqual(page.redraws, 1);
});

run('a swap before charts.js has loaded does not throw', () => {
  const page = loadCharts({ noRenderer: true });

  page.bodyListeners['htmx:afterSwap']({ target: swapTarget('chartsCard', '{"plays":[]}') });

  assert.deepStrictEqual(page.window.__chartData, { plays: [] });
  assert.strictEqual(page.redraws, 0);
});

// --------------------------------------------------------------- the retry

run('a failed swap offers a retry that re-requests the current URL', () => {
  const page = loadCharts({ search: '?interval=last-90-days' });
  assert.strictEqual(page.swapFailure.targetId, 'chartsCard');

  page.swapFailure.retry();

  assert.strictEqual(page.ajax[0].url, '/charts?interval=last-90-days');
  assert.strictEqual(page.ajax[0].opts.target, '#chartsCard');
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
  console.log(`all ${results.length} charts-page tests passed`);
})();
