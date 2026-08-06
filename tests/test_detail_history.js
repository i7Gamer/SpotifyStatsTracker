// Plain-node unit test for the detail pages' play-history list
// (static/js/detail-history.js). Run with: node tests/test_detail_history.js
//
// Two things here are subtler than the sibling page drivers:
//
//   * isHistorySwap decides whether an htmx event belongs to THIS list or to
//     the whole detail body. Both fire on document.body and the container sits
//     inside #detailBody, so the only thing separating them is the target
//     check - and detail-page.js, watching the same events, blanks the entire
//     body when it thinks the failure is its own. A too-broad check here means
//     a failed sort wipes the page.
//   * initDetailHistory is called AFTER every body swap, not at load. Every
//     element it binds is replaced wholesale by that swap, so re-resolving them
//     is also what stops listeners stacking - and registering the pagination
//     hook is gated on the list actually being present, because until the
//     deferred body arrives the full-reload fallback is the right behaviour.
//
// The tab switch is the third: it changes no data, so there is no request for
// htmx to own, but the URL still has to follow - and by replaceState, so Back
// leaves the detail page rather than stepping through tab states.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'detail-history.js');

function makeClassList() {
  const classes = new Set();
  return {
    contains: (n) => classes.has(n),
    toggle(n, force) { if (force) { classes.add(n); } else { classes.delete(n); } },
  };
}

function makeElement(extra) {
  return Object.assign({
    dataset: {}, classList: makeClassList(), children: [],
    addEventListener(type, fn) { (this.handlers = this.handlers || {})[type] = fn; },
    contains(node) { return this.children.indexOf(node) !== -1; },
  }, extra || {});
}

function loadDetailHistory(options) {
  options = options || {};
  const calls = {
    replaced: [], pushed: [], ajax: [], bodyListeners: {}, banners: 0, cleared: 0,
  };
  const elements = options.elements || {};
  const selectors = Object.assign(
    { '.stats-filter-button': [], '[data-category]': [] }, options.selectors || {});

  global.window = {
    location: { pathname: '/song/t1', search: options.search || '' },
    history: {
      replaceState(state, title, url) { calls.replaced.push(url); },
      pushState(state, title, url) { calls.pushed.push(url); },
    },
    AjaxStatus: options.noAjaxStatus ? undefined : {
      showBanner(retry) { calls.banners += 1; calls.lastRetry = retry; },
      clearBanner() { calls.cleared += 1; },
    },
  };
  global.document = {
    getElementById(id) { return elements[id] || null; },
    querySelectorAll(selector) { return selectors[selector] || []; },
    body: { addEventListener(type, fn) { calls.bodyListeners[type] = fn; } },
  };
  global.htmx = { ajax(method, url, opts) { calls.ajax.push({ method, url, opts }); } };

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  calls.window = global.window;
  return calls;
}

function tabSetup(options) {
  const topSongs = makeElement({ dataset: { filter: 'top-songs' } });
  const history = makeElement({ dataset: { filter: 'history' } });
  const topSongsPane = makeElement({ dataset: { category: 'top-songs' } });
  const historyPane = makeElement({ dataset: { category: 'history' } });
  const list = makeElement();
  const page = loadDetailHistory(Object.assign({
    selectors: { '.stats-filter-button': [topSongs, history], '[data-category]': [topSongsPane, historyPane] },
    elements: { detailHistoryResults: list },
  }, options || {}));
  page.window.initDetailHistory();
  return { page, topSongs, history, topSongsPane, historyPane, list };
}

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// ----------------------------------------------------------------- the tabs

run('switching to History shows its pane and marks its tab', () => {
  const dom = tabSetup();

  dom.history.handlers.click();

  assert.strictEqual(dom.historyPane.classList.contains('visible'), true);
  assert.strictEqual(dom.topSongsPane.classList.contains('visible'), false);
  assert.strictEqual(dom.history.classList.contains('active'), true);
  assert.strictEqual(dom.topSongs.classList.contains('active'), false);
});

run('the tab is written into the URL by REPLACING, never pushing', () => {
  const dom = tabSetup();

  dom.history.handlers.click();

  assert.strictEqual(dom.page.replaced.length, 1);
  assert.deepStrictEqual(dom.page.pushed, [],
                         'Back must leave the detail page, not step through tab states');
  const url = new URL(dom.page.replaced[0], 'http://localhost');
  assert.strictEqual(url.searchParams.get('view'), 'history');
});

run('returning to the default tab drops the param instead of spelling it out', () => {
  const dom = tabSetup({ search: '?view=history' });

  dom.topSongs.handlers.click();

  assert.strictEqual(dom.page.replaced[0], '/song/t1', 'no trailing "?" either');
});

run('switching tabs keeps the other query params', () => {
  const dom = tabSetup({ search: '?groupBy=day&sort=oldest' });

  dom.history.handlers.click();

  const url = new URL(dom.page.replaced[0], 'http://localhost');
  assert.strictEqual(url.searchParams.get('groupBy'), 'day');
  assert.strictEqual(url.searchParams.get('sort'), 'oldest');
  assert.strictEqual(url.searchParams.get('view'), 'history');
});

// ------------------------------------------------------- the pagination hook

run('the jump-to-page hook is registered once the list is on the page', () => {
  const dom = tabSetup();

  assert.strictEqual(typeof dom.page.window.__paginationAjaxHandler, 'function');
});

run('a body that has not arrived yet leaves the full-reload fallback in place', () => {
  const page = loadDetailHistory({ elements: {} });   //< no detailHistoryResults

  page.window.initDetailHistory();

  assert.strictEqual(page.window.__paginationAjaxHandler, undefined,
                     'there is no list to swap, so navigating is the right behaviour');
});

run('a page jump replaces the URL and swaps only the list', () => {
  const dom = tabSetup({ search: '?view=history' });

  dom.page.window.__paginationAjaxHandler(3);

  const url = new URL(dom.page.replaced[0], 'http://localhost');
  assert.strictEqual(url.searchParams.get('page'), '3');
  assert.strictEqual(url.searchParams.get('view'), 'history');
  assert.deepStrictEqual(dom.page.pushed, []);
  assert.strictEqual(dom.page.ajax[0].opts.target, '#detailHistoryResults');
});

// --------------------------------------------------- telling the swaps apart

run('a failure inside the list gets a banner', () => {
  const dom = tabSetup();

  dom.page.bodyListeners['htmx:responseError']({ detail: { target: dom.list } });

  assert.strictEqual(dom.page.banners, 1);
});

run('a failure on a row INSIDE the list still counts as the list', () => {
  const dom = tabSetup();
  const row = makeElement();
  dom.list.children.push(row);

  dom.page.bodyListeners['htmx:sendError']({ detail: { target: row } });

  assert.strictEqual(dom.page.banners, 1);
});

run('a failure elsewhere in the body is left to detail-page.js', () => {
  const dom = tabSetup();

  dom.page.bodyListeners['htmx:responseError']({ detail: { target: makeElement() } });

  assert.strictEqual(dom.page.banners, 0,
                     'claiming it here would let a failed sort blank the whole page');
});

run('a failure before the list exists is nobody\'s business here', () => {
  const page = loadDetailHistory({ elements: {} });

  page.bodyListeners['htmx:responseError']({ detail: { target: makeElement() } });

  assert.strictEqual(page.banners, 0);
});

run('the retry re-requests the current URL into the list', () => {
  const dom = tabSetup({ search: '?view=history&sort=oldest' });

  dom.page.bodyListeners['htmx:responseError']({ detail: { target: dom.list } });
  dom.page.lastRetry();

  assert.strictEqual(dom.page.ajax[0].url, '/song/t1?view=history&sort=oldest');
  assert.strictEqual(dom.page.ajax[0].opts.target, '#detailHistoryResults');
});

run('a list swap that lands clears the banner the last failure left', () => {
  const dom = tabSetup();

  dom.page.bodyListeners['htmx:afterSwap']({ target: dom.list });

  assert.strictEqual(dom.page.cleared, 1);
});

run('a swap of something else does not clear this list\'s banner', () => {
  const dom = tabSetup();

  dom.page.bodyListeners['htmx:afterSwap']({ target: makeElement() });

  assert.strictEqual(dom.page.cleared, 0);
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
  console.log(`all ${results.length} detail-history tests passed`);
})();
