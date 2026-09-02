// Plain-node unit test for the one piece of filter logic static/js/compare.js
// still owns: which of the form's blank values mean "unset".
//
// It exists as a page-local helper rather than in static/js/htmx-filters.js
// (where /history and the Top lists get theirs) because Compare is the one page
// whose Time Period control has a MEANINGFUL blank value - All Time - while an
// absent interval means something else entirely. That distinction is invisible
// in the markup and would be re-broken by anyone "simplifying" the two into
// one, so it is pinned here.
//
// No test framework/dependency - run with:
//   node tests/test_compare_page.js
const assert = require('assert');
const { pruneCompareAutoParams, COMPARE_AUTO_PARAMS } = require('../static/js/compare.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

run('an untouched Trend buckets control is dropped', () => {
  // Auto is the empty option, and the route derives the bucketing from the
  // range span when groupBy is absent - so leaving it out is how "Auto" is
  // said in a URL, exactly as the old params.delete('groupBy') did.
  assert.deepStrictEqual(pruneCompareAutoParams({ groupBy: '' }), {});
});

run('an explicit Trend buckets choice survives', () => {
  assert.deepStrictEqual(pruneCompareAutoParams({ groupBy: 'week' }), { groupBy: 'week' });
});

run('an empty interval is KEPT - blank is All Time, absent is the saved default', () => {
  // The whole reason this page cannot use HtmxFilters.pruneEmptyParams: an
  // absent interval falls back to the user's default_dashboard_window, so
  // dropping the blank one would move an All Time view to e.g. "Last Month" on
  // the next filter change.
  const result = pruneCompareAutoParams({ interval: '', limit: '10' });
  assert.strictEqual('interval' in result, true);
  assert.strictEqual(result.interval, '');
});

run('no other blank is touched either', () => {
  // Only the named params are candidates. Anything else blank is either
  // impossible (the selects always have a value) or meaningful, and guessing
  // is what the shared helper does - deliberately not this one.
  const params = { with: '', sortBy: '', startDate: '' };
  assert.deepStrictEqual(pruneCompareAutoParams(params), params);
});

run('the object is mutated in place, as htmx requires', () => {
  // htmx reads evt.detail.parameters back off the same object after the event,
  // so returning a fresh copy would prune nothing.
  const params = { groupBy: '', interval: 'week' };
  const returned = pruneCompareAutoParams(params);
  assert.strictEqual(returned, params);
  assert.strictEqual('groupBy' in params, false);
});

run('an already-clean object is left alone', () => {
  assert.deepStrictEqual(pruneCompareAutoParams({ interval: 'week' }), { interval: 'week' });
});

run('the pruned set is exactly the Trend buckets control', () => {
  // A negative control: growing this list is a decision about a param whose
  // blank value stops being a value, not a tidy-up.
  assert.deepStrictEqual(COMPARE_AUTO_PARAMS, ['groupBy']);
});

// ------------------------------------------------------ the category pills
//
// The one piece of DOM wiring compare.js runs at load: the stats-filter
// buttons show/hide the [data-category] sections. The stubs below are the
// minimum that wiring touches; everything else on the page is htmx's.

function makeClassList() {
  const classes = new Set();
  return {
    contains: (n) => classes.has(n),
    add: (n) => classes.add(n),
    remove: (n) => classes.delete(n),
  };
}

function makeNode(dataset) {
  const node = {
    dataset, classList: makeClassList(), attrs: {}, handlers: {},
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
    addEventListener(type, fn) { this.handlers[type] = fn; },
    click() { this.handlers.click(); },
  };
  return node;
}

function loadCompareDom() {
  const all = makeNode({ filter: 'all' });
  const songs = makeNode({ filter: 'top-songs' });
  const songSection = makeNode({ category: 'top-songs' });
  const artistSection = makeNode({ category: 'top-artists' });
  global.window = {};
  global.document = {
    getElementById() { return null; },
    body: { addEventListener() {} },
    querySelectorAll(selector) {
      if (selector === '.stats-filter-button') return [all, songs];
      if (selector === '[data-category]') return [songSection, artistSection];
      return [];
    },
    querySelector(selector) {
      return selector === '.stats-filter-button[data-filter="all"]' ? all : null;
    },
  };
  delete require.cache[require.resolve('../static/js/compare.js')];
  require('../static/js/compare.js');
  delete global.window;
  delete global.document;
  return { all, songs, songSection, artistSection };
}

run('All Stats is pressed at load, and says so', () => {
  const dom = loadCompareDom();

  assert.strictEqual(dom.all.classList.contains('active'), true);
  assert.strictEqual(dom.artistSection.classList.contains('visible'), true);
  //< the state a screen reader hears - written beside the class, every time
  assert.strictEqual(dom.all.getAttribute('aria-pressed'), 'true');
  assert.strictEqual(dom.songs.getAttribute('aria-pressed'), 'false');
});

run('choosing a category moves the pressed state with the class', () => {
  const dom = loadCompareDom();

  dom.songs.click();

  assert.strictEqual(dom.songs.classList.contains('active'), true);
  assert.strictEqual(dom.all.classList.contains('active'), false);
  assert.strictEqual(dom.artistSection.classList.contains('visible'), false);
  assert.strictEqual(dom.songs.getAttribute('aria-pressed'), 'true');
  assert.strictEqual(dom.all.getAttribute('aria-pressed'), 'false');
});

console.log('All compare page tests passed.');
