// Plain-node unit test for the Top Songs/Artists/Albums browser logic
// (static/js/top-list.js). Run with: node tests/test_top_list_page.js
//
// Everything htmx could express declaratively was deleted from this file; what
// is left is the handful of things it could NOT, which is exactly the part with
// no coverage. Two of them are load-bearing:
//
//   * the jump-to-page handler REPLACES the history entry, never pushes. A push
//     would make Back walk through page numbers instead of leaving the page.
//   * the htmx:configRequest veto is scoped to the FORM. A boosted pagination
//     link carries its whole query in its href and has to keep working while
//     the Time Period select sits on a half-typed custom range - the very state
//     that blocks a form request.
//
// The "Full plays only" hidden field used to be a third; it moved to
// static/js/htmx-filters.js (and tests/test_htmx_filters.js with it) when
// /history started rendering the same partial, because this file is loaded by
// the Top pages only.
//
// The stub is hand-rolled (no jsdom). Note HtmxFilters.RANGE_OK is null, not a
// string: the veto below turns on `problem !== RANGE_OK`, so a stub returning
// a truthy "ok" would pass a broken test.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'top-list.js');
const RANGE_OK = null;
const RANGE_INVERTED = 'inverted';

function loadTopList(options) {
  options = options || {};
  const calls = {
    replaced: [], pushed: [], ajax: [], syncedRanges: [], shownErrors: [],
    pruned: [], swapFailure: null, bodyListeners: {},
  };
  const elements = options.elements || {};

  global.window = {
    location: { pathname: '/top-songs', search: options.search || '' },
    history: {
      replaceState(state, title, url) { calls.replaced.push(url); },
      pushState(state, title, url) { calls.pushed.push(url); },
    },
  };
  global.document = {
    getElementById(id) { return elements[id] || null; },
    body: { addEventListener(type, fn) { calls.bodyListeners[type] = fn; } },
  };
  global.htmx = { ajax(method, url, opts) { calls.ajax.push({ method, url, opts }); } };
  global.HtmxFilters = {
    RANGE_OK,
    syncCustomRange(containerId) { calls.syncedRanges.push(containerId); },
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

function configRequest(page, elementId, parameters) {
  const evt = {
    detail: { elt: elementId === null ? null : { id: elementId }, parameters: parameters || {} },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  page.bodyListeners['htmx:configRequest'](evt);
  return evt;
}

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// ------------------------------------------------------------ jump to page

run('a page jump replaces the history entry and never pushes one', () => {
  const page = loadTopList({ search: '?interval=last-30-days' });

  page.window.__paginationAjaxHandler(4);

  assert.strictEqual(page.replaced.length, 1);
  assert.deepStrictEqual(page.pushed, [], 'a push would make Back walk page numbers');
});

run('a page jump keeps the other filters in both the URL and the request', () => {
  const page = loadTopList({ search: '?q=mf+doom&sortBy=plays&page=2' });

  page.window.__paginationAjaxHandler(5);

  const url = new URL(page.replaced[0], 'http://localhost');
  assert.strictEqual(url.pathname, '/top-songs');
  assert.strictEqual(url.searchParams.get('page'), '5', 'the old page is overwritten, not appended');
  assert.strictEqual(url.searchParams.get('q'), 'mf doom');
  assert.strictEqual(url.searchParams.get('sortBy'), 'plays');
  assert.strictEqual(page.ajax[0].url, page.replaced[0], 'the swap asks for the URL just published');
  assert.strictEqual(page.ajax[0].opts.target, '#topListResults');
});

// -------------------------------------------------------- the request veto

run('a half-typed custom range stops the form request and says why', () => {
  const page = loadTopList({ rangeProblem: RANGE_INVERTED });

  const evt = configRequest(page, 'topListFilters');

  assert.strictEqual(evt.prevented, 1);
  assert.deepStrictEqual(page.shownErrors, [RANGE_INVERTED]);
  assert.deepStrictEqual(page.pruned, [], 'a vetoed request is never serialized');
});

run('a valid range lets the request through and prunes its empty params', () => {
  const page = loadTopList({ rangeProblem: RANGE_OK });
  const parameters = { q: '', sortBy: 'plays' };

  const evt = configRequest(page, 'topListFilters', parameters);

  assert.strictEqual(evt.prevented, 0);
  assert.deepStrictEqual(page.shownErrors, [RANGE_OK], 'a stale error is cleared, not left up');
  assert.deepStrictEqual(page.pruned, [parameters]);
});

run('a boosted pagination link is never vetoed, even mid-typo', () => {
  const page = loadTopList({ rangeProblem: RANGE_INVERTED });

  const evt = configRequest(page, 'somePaginationLink');

  assert.strictEqual(evt.prevented, 0, 'its whole query is in its href; the form state is irrelevant');
  assert.deepStrictEqual(page.shownErrors, [], 'and it must not paint an error for a form it is not');
});

run('a request from an element with no id at all is left alone', () => {
  const page = loadTopList({ rangeProblem: RANGE_INVERTED });

  const evt = configRequest(page, null);

  assert.strictEqual(evt.prevented, 0);
});

// ------------------------------------------------------- interval + retry

run('the Time Period select syncs its own custom-range container', () => {
  const page = loadTopList({});

  page.window.updateIntervalFilter();

  assert.deepStrictEqual(page.syncedRanges, ['customDates']);
});

// Off the form, not the address bar: a 4xx/5xx never touches the URL (htmx
// updates history only inside its successful-swap branch), so after a failed
// filter change the controls show the new choice while the URL still says the
// old one - and a retry of the URL rendered the old list under the new
// selection.
run('a failed swap offers a retry that re-serialises the form, not the stale URL', () => {
  const form = { id: 'topListFilters', getAttribute(name) { return name === 'hx-get' ? '/top-songs' : null; } };
  const page = loadTopList({ search: '?sortBy=plays', elements: { topListFilters: form } });
  assert.strictEqual(page.swapFailure.targetId, 'topListResults');

  page.swapFailure.retry();

  assert.strictEqual(page.ajax[0].url, '/top-songs', 'the bare hx-get path: htmx appends the form values itself');
  assert.strictEqual(page.ajax[0].opts.source, form);
  assert.strictEqual(page.ajax[0].opts.target, '#topListResults');
  assert.strictEqual(page.ajax[0].opts.swap, 'innerHTML');
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
  console.log(`all ${results.length} top-list tests passed`);
})();
