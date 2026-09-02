// Plain-node unit tests for the detail pages' Trend-buckets loader
// (static/js/detail-chart.js). No test framework/dependency - run with:
//   node tests/test_detail_chart.js            (every case)
//   node tests/test_detail_chart.js <text>     (only cases whose name contains it)
// The filter exists for mutation checks: the runner stops at the first
// failure, so proving that a SPECIFIC case catches a broken guard means
// running that case alone against the break.
//
// Two halves. The URL builders are exported and tested directly. The loader is
// not (it lives in the IIFE), so the second half drives it the way the page
// does - through window.updateDetailGroupByFilter - against a DOM stub, with
// fetch settled by hand so two in-flight loads can land in either order. The
// loader is the last sequence-guarded fetch() in static/js, and it carries the
// same ".finally needs the guard too" shape the admin tab race taught: nothing
// else fails if either guard is dropped, so these cases exist to.
//
// The REAL ajax-status.js is loaded rather than stubbed, so the 401/404 reading
// the loader delegates to it is the production one; only its two banner
// functions are replaced with recorders (they need a <main> to render into).
const assert = require('assert');

const MODULE_PATH = require.resolve('../static/js/detail-chart.js');
const AJAX_STATUS_PATH = require.resolve('../static/js/ajax-status.js');
const { detailDataUrl, detailPageUrl } = require(MODULE_PATH);

//< the runner reports through this; freshPage() below replaces console.error
//  to capture what the loader's catch logs
const realConsoleError = console.error;

const tests = [];
function run(name, fn) { tests.push([name, fn]); }

// --- URL builders -----------------------------------------------------------

run('detailDataUrl adds ajax=true', () => {
  assert.strictEqual(detailDataUrl('/song/t1', ''), '/song/t1?ajax=true');
});

run('detailDataUrl preserves existing params and adds ajax=true', () => {
  assert.strictEqual(
    detailDataUrl('/artist/a1', '?groupBy=month'),
    '/artist/a1?groupBy=month&ajax=true',
  );
});

run('detailDataUrl overwrites a stale ajax value rather than duplicating it', () => {
  assert.strictEqual(detailDataUrl('/album/x', '?ajax=false'), '/album/x?ajax=true');
});

run('detailPageUrl sets groupBy for an explicit bucket', () => {
  assert.strictEqual(detailPageUrl('/song/t1', '', 'day'), '/song/t1?groupBy=day');
  assert.strictEqual(detailPageUrl('/song/t1', '', 'week'), '/song/t1?groupBy=week');
  assert.strictEqual(detailPageUrl('/song/t1', '', 'month'), '/song/t1?groupBy=month');
});

run('detailPageUrl drops groupBy entirely for Auto (empty value)', () => {
  assert.strictEqual(detailPageUrl('/song/t1', '?groupBy=month', ''), '/song/t1');
});

run('detailPageUrl strips ajax from the pushed page URL', () => {
  assert.strictEqual(detailPageUrl('/song/t1', '?ajax=true', 'week'), '/song/t1?groupBy=week');
});

run('detailPageUrl preserves unrelated params', () => {
  assert.strictEqual(
    detailPageUrl('/artist/a1', '?foo=bar', 'week'),
    '/artist/a1?foo=bar&groupBy=week',
  );
});

// --- the loader -------------------------------------------------------------

function makeClassList() {
  const names = new Set();
  return {
    add(n) { names.add(n); },
    remove(n) { names.delete(n); },
    contains(n) { return names.has(n); },
  };
}

const INITIAL_SERIES = 'initial';
const HIDDEN_GROUP_BY_SELECTOR = 'form input[type="hidden"][name="groupBy"]';

/* Load a FRESH copy of the module (activeLoad is module state) against a DOM
 * stub carrying the chart canvas, the Group-by select and two hidden groupBy
 * inputs, and a fetch stub whose settlement the test controls. The stub's
 * replaceState updates window.location the way a browser's does, so the fetch
 * URL a case sees is the one the loader built AFTER the URL update. */
function freshPage() {
  const wrap = { classList: makeClassList() };
  const canvas = { parentElement: wrap };
  const groupBySelect = { value: '' };
  const hiddenInputs = [{ value: 'original' }, { value: 'original' }];
  const calls = { renders: 0, banners: [], cleared: [], replaced: [], pushed: [], errors: [] };

  function setLocation(url) {
    const parsed = new URL(url, 'http://localhost');
    global.window.location.pathname = parsed.pathname;
    global.window.location.search = parsed.search;
    global.window.location.href = parsed.pathname + parsed.search;
  }

  global.window = {
    location: {},
    history: {
      replaceState(state, title, url) { calls.replaced.push(url); setLocation(url); },
      pushState(state, title, url) { calls.pushed.push(url); setLocation(url); },
    },
    addEventListener() {},
    __chartData: { timeSeries: INITIAL_SERIES },
    renderTimeSeriesChart() { calls.renders += 1; },
  };
  setLocation('/song/t1');

  global.document = {
    getElementById(id) {
      if (id === 'timeSeriesChart') return canvas;
      if (id === 'groupBy') return groupBySelect;
      return null;
    },
    querySelectorAll(selector) { return selector === HIDDEN_GROUP_BY_SELECTOR ? hiddenInputs : []; },
    querySelector() { return null; },
  };

  const pendingFetches = [];
  //< ignores the abort signal on purpose: the cases model a response that
  //  settled BEFORE its abort, which is the one the loader's guards exist for
  global.fetch = (url) => new Promise((resolve, reject) => {
    pendingFetches.push({ url, resolve, reject });
  });

  //< the fade floor is a UX minimum, not part of the ordering under test;
  //  firing it synchronously keeps every case free of the clock
  global.setTimeout = (fn) => { fn(); return 0; };

  console.error = (err) => { calls.errors.push(err); };

  delete require.cache[AJAX_STATUS_PATH];
  require(AJAX_STATUS_PATH);   //< installs the real window.AjaxStatus
  global.window.AjaxStatus.showBanner = (onRetry, message, owner) => {
    calls.banners.push({ onRetry, message, owner });
  };
  global.window.AjaxStatus.clearBanner = (owner) => { calls.cleared.push(owner); };

  delete require.cache[MODULE_PATH];
  require(MODULE_PATH);

  return {
    wrap, groupBySelect, hiddenInputs, calls, pendingFetches,
    chartData: global.window.__chartData,
    load(groupBy) {
      groupBySelect.value = groupBy || '';
      global.window.updateDetailGroupByFilter();
    },
  };
}

function okResponse(timeSeries) {
  return { status: 200, ok: true, json: () => Promise.resolve({ timeSeries }) };
}

//< drains the whole promise chain (fetch -> json -> Promise.all -> then/catch/finally)
function settle() { return new Promise((resolve) => setImmediate(resolve)); }

run('a superseded response does not repaint the chart', async () => {
  const page = freshPage();

  page.load('day');    //< A - settles first, but is no longer the live load
  page.load('week');   //< B
  page.pendingFetches[0].resolve(okResponse('older'));
  await settle();

  assert.strictEqual(page.chartData.timeSeries, INITIAL_SERIES,
                     'the stale series must not be swapped in over a newer load');
  assert.strictEqual(page.calls.renders, 0);

  page.pendingFetches[1].resolve(okResponse('newer'));
  await settle();

  assert.strictEqual(page.chartData.timeSeries, 'newer');
  assert.strictEqual(page.calls.renders, 1);
});

run('a superseded load settling leaves the newer load its fade and its slot', async () => {
  /* The .finally half of the guard. Without it, A's settlement clears the
   * loading-fade B put up AND empties the in-flight slot - so when B lands,
   * its own guard reads an empty slot and drops the data the user asked for. */
  const page = freshPage();

  page.load('day');    //< A
  page.load('week');   //< B
  page.pendingFetches[0].resolve(okResponse('older'));
  await settle();

  assert.ok(page.wrap.classList.contains('loading-fade'),
            "B is still in flight, so its fade must survive A's settlement");

  page.pendingFetches[1].resolve(okResponse('newer'));
  await settle();

  assert.strictEqual(page.chartData.timeSeries, 'newer',
                     "the newer load must still land after the older one's settlement");
  assert.ok(!page.wrap.classList.contains('loading-fade'), 'the live load clears its own fade');
});

run('a failure after supersession shows no banner', async () => {
  const page = freshPage();

  page.load('day');    //< A - fails, but only after B replaced it
  page.load('week');   //< B
  page.pendingFetches[0].reject(new Error('network down'));
  await settle();

  assert.strictEqual(page.calls.banners.length, 0,
                     'an error about a request the user has moved past is not theirs to retry');

  page.pendingFetches[1].resolve(okResponse('newer'));
  await settle();

  assert.strictEqual(page.calls.banners.length, 0);
  assert.strictEqual(page.chartData.timeSeries, 'newer');
});

run('a failure of the live load shows a banner whose Retry reloads', async () => {
  //< control for the case above: the guard must not silence the live load
  const page = freshPage();

  page.load('day');
  page.pendingFetches[0].reject(new Error('network down'));
  await settle();

  assert.strictEqual(page.calls.banners.length, 1);
  assert.strictEqual(page.calls.errors.length, 1, 'a real failure is logged');
  assert.ok(!page.wrap.classList.contains('loading-fade'));

  page.calls.banners[0].onRetry();

  assert.strictEqual(page.pendingFetches.length, 2, 'Retry issues a fresh load');
});

run('the chart claims its banner and clears only its own', async () => {
  /* The play-log list (detail-history.js) reports through the same banner
   * slot on the same page. Unowned, its sort landing took down the chart's
   * failure banner - and the Retry a chart still showing the previous series
   * under the new label needed. Both sides name themselves now. */
  const page = freshPage();

  page.load('day');
  page.pendingFetches[0].reject(new Error('network down'));
  await settle();
  assert.strictEqual(page.calls.banners[0].owner, 'detail-chart');

  page.load('week');
  page.pendingFetches[1].resolve(okResponse('newer'));
  await settle();

  assert.deepStrictEqual(page.calls.cleared, ['detail-chart'],
                         "a chart success must not take down the play log's banner");
});

// An entity removed after the page loaded (an overwrite import or a merge
// mid-session) answers the bucket change with 404 {redirectUrl} - the JSON
// twin of the HX-Redirect the deferred body gets (routes/charts.py's
// _missingEntityResponse, pinned by tests/test_detail_htmx.py -k json_404).
// The shared helper throws on every non-2xx before reading a body, so the
// loader used to show "couldn't load" with a Retry that failed identically
// forever, while a reload of the same URL redirected to the top list.

function notFound(body) {
  return { status: 404, ok: false, json: () => Promise.resolve(body) };
}

run('a 404 carrying a redirectUrl navigates there and shows no banner', async () => {
  const page = freshPage();

  page.load('day');
  page.pendingFetches[0].resolve(notFound({ redirectUrl: '/top-songs' }));
  await settle();

  assert.strictEqual(global.window.location.href, '/top-songs');
  assert.strictEqual(page.calls.banners.length, 0, 'leaving the page is not a failure to retry');
  assert.strictEqual(page.calls.errors.length, 0, 'nor one to log');
  assert.strictEqual(page.chartData.timeSeries, INITIAL_SERIES);
});

run('a 404 without a redirectUrl still gets the banner', async () => {
  const page = freshPage();

  page.load('day');
  page.pendingFetches[0].resolve(notFound({ error: 'not found' }));
  await settle();

  assert.strictEqual(global.window.location.href, '/song/t1?groupBy=day', 'nowhere to go');
  assert.strictEqual(page.calls.banners.length, 1);
  assert.match(String(page.calls.errors[0]), /detail chart fetch failed: 404/,
               'the shared helper still names the failure');
});

run("the previous load's abort is neither logged nor reported", async () => {
  /* What a real fetch() does to the load a newer bucket change aborted: reject
   * with an AbortError. The catch filters it by name - the reason the
   * AbortController here is documented safe where it was declined elsewhere. */
  const page = freshPage();

  page.load('day');    //< A - aborted by B
  page.load('week');   //< B
  const abort = new Error('The operation was aborted');
  abort.name = 'AbortError';
  page.pendingFetches[0].reject(abort);
  await settle();

  assert.strictEqual(page.calls.errors.length, 0, 'an abort is not a failure to log');
  assert.strictEqual(page.calls.banners.length, 0);
});

run('updateDetailGroupByFilter replaces the URL, syncs every hidden groupBy, then loads', () => {
  const page = freshPage();

  page.load('week');

  assert.deepStrictEqual(page.calls.replaced, ['/song/t1?groupBy=week']);
  assert.deepStrictEqual(page.calls.pushed, [],
                         'Back must leave the detail page, not step through bucket states');
  assert.deepStrictEqual(page.hiddenInputs.map((input) => input.value), ['week', 'week'],
                         'the admin refresh form must redirect back with the visible choice');
  assert.strictEqual(page.pendingFetches.length, 1);
  assert.strictEqual(page.pendingFetches[0].url, '/song/t1?groupBy=week&ajax=true',
                     'the fetch reads the URL the replaceState just wrote');
});

(async () => {
  const only = process.argv[2] || '';
  const selected = tests.filter(([name]) => name.includes(only));
  assert.ok(selected.length > 0, `no case matches "${only}"`);
  for (const [name, fn] of selected) {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      realConsoleError(`FAIL - ${name}`);
      realConsoleError(err);
      process.exit(1);
    }
  }
  console.log(`All ${selected.length} detail-chart tests passed.`);
})();
