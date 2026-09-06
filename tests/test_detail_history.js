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
    dataset: {}, classList: makeClassList(), children: [], attrs: {},
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
    hasAttribute(name) { return name in this.attrs; },
    addEventListener(type, fn) { (this.handlers = this.handlers || {})[type] = fn; },
    contains(node) { return this.children.indexOf(node) !== -1; },
    focusCalls: 0,
    focus() { this.focusCalls += 1; },
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
      showBanner(retry, message, owner) {
        calls.banners += 1; calls.lastRetry = retry; calls.bannerOwner = owner;
      },
      clearBanner(owner) { calls.cleared += 1; calls.clearedOwner = owner; },
    },
  };
  //< detail-history.js now registers more than one listener for the same
  //  event type (e.g. htmx:responseError: reportHistoryFailure AND
  //  disarmShowMoreFocusRestore) - dispatch to every handler registered for
  //  a type, not just the last one, while keeping calls.bodyListeners[type]
  //  a single callable the way every existing test already uses it
  const bodyListenerFns = {};
  global.document = {
    activeElement: options.activeElement || null,
    getElementById(id) { return elements[id] || null; },
    querySelectorAll(selector) { return selectors[selector] || []; },
    body: {
      addEventListener(type, fn) {
        if (!bodyListenerFns[type]) {
          bodyListenerFns[type] = [];
          calls.bodyListeners[type] = function (evt) {
            bodyListenerFns[type].forEach((handler) => handler(evt));
          };
        }
        bodyListenerFns[type].push(fn);
      },
    },
  };
  global.htmx = { ajax(method, url, opts) { calls.ajax.push({ method, url, opts }); } };

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  calls.window = global.window;
  //< exposed so a test can move focus between events, and so the
  //  elements map (closed over by getElementById) can gain a NEW button
  //  before an afterSettle dispatch
  calls.document = global.document;
  calls.elements = elements;
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
  //< the state a screen reader hears - written beside the class, every time
  assert.strictEqual(dom.history.getAttribute('aria-pressed'), 'true');
  assert.strictEqual(dom.topSongs.getAttribute('aria-pressed'), 'false');
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

// htmx does the replacing (its `replace` option is forwarded into the same
// history update hx-replace-url feeds), and only inside its successful-swap
// branch. The address bar used to be rewritten HERE, before the request, so a
// failed jump left it claiming a page the list never showed.
run('a page jump lets htmx replace the URL on success and is issued off the list', () => {
  const dom = tabSetup({ search: '?view=history' });

  dom.page.window.__paginationAjaxHandler(3);

  const url = new URL(dom.page.ajax[0].url, 'http://localhost');
  assert.strictEqual(url.searchParams.get('page'), '3');
  assert.strictEqual(url.searchParams.get('view'), 'history');
  assert.deepStrictEqual(dom.page.replaced, [], 'rewritten before the request = a failed jump lies');
  assert.deepStrictEqual(dom.page.pushed, []);
  assert.strictEqual(dom.page.ajax[0].opts.replace, dom.page.ajax[0].url);
  assert.strictEqual(dom.page.ajax[0].opts.push, undefined);
  //< the list's hx-target/hx-swap/hx-sync are inherited from it, so a jump
  //  during an in-flight sort change is serialised like every swap into it
  assert.strictEqual(dom.page.ajax[0].opts.source, dom.list);
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

// The Trend-buckets chart (detail-chart.js) reports through the same banner
// slot on the same page. Both used to clear it on their own success, so a sort
// that landed took down the chart's failure banner and its Retry. The banner
// is owned now (see ajax-status.js), and this list must name itself on both
// sides: unowned, its own success could not take down its own banner, and its
// clear would take down the chart's.

run('the banner is claimed for this list', () => {
  const dom = tabSetup();

  dom.page.bodyListeners['htmx:responseError']({ detail: { target: dom.list } });

  assert.strictEqual(dom.page.bannerOwner, 'detail-history');
});

run('the clear names this list, so it leaves the chart\'s banner alone', () => {
  const dom = tabSetup();

  dom.page.bodyListeners['htmx:afterSwap']({ target: dom.list });

  assert.strictEqual(dom.page.clearedOwner, 'detail-history');
});

// --------------------------------------------- R8: "Show More" keeps focus
// #showMorePlaysBtn carries hx-disabled-elt="this": htmx disables it right
// after htmx:beforeRequest, which blurs it, so by afterSettle nothing in the
// swapped #timelineActions holds focus - and the swap is outerHTML, so the
// OLD button (and the old #timelineActions) is detached by then too. htmx's
// own by-id restore cannot help either: the old button was already disabled
// (and unfocused) before the swap, so there is no live element for it to
// match against. beforeRequest fires before the disable, so that is the last
// point document.activeElement can still be the button.
//
// The `elements` map passed into loadDetailHistory is closed over by the
// getElementById stub, so a test can put a NEW element at the same id
// in-place between dispatching beforeRequest and afterSettle to model the
// swap having landed - exactly like `dom.page.elements.showMorePlaysBtn = ...`
// below.

const SHOW_MORE_BUTTON_ID = 'showMorePlaysBtn';

function showMoreSetup(options) {
  const list = makeElement();
  const button = makeElement({ id: SHOW_MORE_BUTTON_ID });
  const elements = Object.assign(
    { detailHistoryResults: list, showMorePlaysBtn: button },
    (options && options.elements) || {},
  );
  const page = loadDetailHistory(Object.assign({}, options, { elements }));
  return { page, list, button };
}

run('R8(a): the button focused before the request is refocused via the NEW button after settle', () => {
  const dom = showMoreSetup();
  dom.page.document.activeElement = dom.button;

  dom.page.bodyListeners['htmx:beforeRequest']({ detail: { elt: dom.button } });

  //< the swap landed: a brand new button object now sits at the same id
  const newButton = makeElement({ id: SHOW_MORE_BUTTON_ID });
  dom.page.elements.showMorePlaysBtn = newButton;
  dom.page.bodyListeners['htmx:afterSettle']({});

  assert.strictEqual(newButton.focusCalls, 1);
  assert.strictEqual(dom.button.focusCalls, 0, 'the OLD, disabled button must never be focused');
});

run('R8(b): a request not fired by a focused Show More button arms nothing', () => {
  const dom = showMoreSetup();
  dom.page.document.activeElement = makeElement();   //< focus was elsewhere

  dom.page.bodyListeners['htmx:beforeRequest']({ detail: { elt: dom.button } });

  const newButton = makeElement({ id: SHOW_MORE_BUTTON_ID });
  dom.page.elements.showMorePlaysBtn = newButton;
  dom.page.bodyListeners['htmx:afterSettle']({});

  assert.strictEqual(newButton.focusCalls, 0);
});

run('R8(c): the last batch (no new button rendered) falls back to the results container', () => {
  const dom = showMoreSetup();
  dom.page.document.activeElement = dom.button;

  dom.page.bodyListeners['htmx:beforeRequest']({ detail: { elt: dom.button } });
  delete dom.page.elements.showMorePlaysBtn;   //< the batch was exhausted: no new button

  dom.page.bodyListeners['htmx:afterSettle']({});

  assert.strictEqual(dom.list.attrs.tabindex, '-1',
                     'a container needs a tabindex to be focusable at all');
  assert.strictEqual(dom.list.focusCalls, 1);
});

run('R8(d): a sendAbort between beforeRequest and afterSettle disarms the restore', () => {
  //< the button inherits hx-sync="#detailHistoryResults:replace", so a sort
  //  change firing mid-batch aborts it rather than queuing behind it
  const dom = showMoreSetup();
  dom.page.document.activeElement = dom.button;

  dom.page.bodyListeners['htmx:beforeRequest']({ detail: { elt: dom.button } });
  dom.page.bodyListeners['htmx:sendAbort']({});

  const newButton = makeElement({ id: SHOW_MORE_BUTTON_ID });
  dom.page.elements.showMorePlaysBtn = newButton;
  dom.page.bodyListeners['htmx:afterSettle']({});

  assert.strictEqual(newButton.focusCalls, 0, 'the aborted batch must not arm a later, unrelated settle');
});

run('R8(d): a responseError between beforeRequest and afterSettle also disarms the restore', () => {
  const dom = showMoreSetup();
  dom.page.document.activeElement = dom.button;

  dom.page.bodyListeners['htmx:beforeRequest']({ detail: { elt: dom.button } });
  dom.page.bodyListeners['htmx:responseError']({ detail: { target: makeElement() } });

  const newButton = makeElement({ id: SHOW_MORE_BUTTON_ID });
  dom.page.elements.showMorePlaysBtn = newButton;
  dom.page.bodyListeners['htmx:afterSettle']({});

  assert.strictEqual(newButton.focusCalls, 0);
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
