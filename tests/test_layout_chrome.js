// Plain-node unit test for the shared page chrome (static/js/layout-chrome.js).
// Run with: node tests/test_layout_chrome.js
//
// This file is loaded by EVERY authenticated page and nothing executed it. Its
// four window exports are called only from inline on*= attributes in templates,
// so no import graph reaches them either - a typo in the clamp below would land
// on /history, /top-songs, /top-artists, /top-albums and /charts at once and
// show up as "the page number box does nothing".
//
// Two of the behaviours here are load-bearing beyond correctness:
//   * handleJumpToPageKeydown CLAMPS to [1, totalPages]. Unclamped it hands the
//     server a page nobody can be on - see routes/charts.py's _movementPage for
//     what that costs on the endpoint that cannot re-derive a row count.
//   * the listener-status poll CLEARS ITS INTERVAL on 401. Without that it kept
//     hitting the server every 10s for the life of the tab; the pill hiding is
//     the visible part, the clearInterval is the fix.
//
// The stub is hand-rolled (no jsdom): these functions touch getElementById,
// classList and location, and nothing else. setInterval/setTimeout MUST be
// stubbed rather than left to node - the script arms a 10s and a 15min interval
// at load, which would otherwise hold the process open and hang the suite.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'layout-chrome.js');

function makeElement() {
  const classes = new Set();
  return {
    style: {},
    className: '',
    title: '',
    textContent: '',
    attributes: {},
    links: [],
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      toggle(name) { if (classes.has(name)) { classes.delete(name); } else { classes.add(name); } },
      contains(name) { return classes.has(name); },
    },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(type, fn) { (this.handlers = this.handlers || {})[type] = fn; },
    querySelectorAll() { return this.links; },
  };
}

// Loads the script fresh against a new stub. Fresh because the module cache
// would otherwise replay the first load's side effects - and this script's
// side effects ARE most of what is under test.
function loadChrome(options) {
  options = options || {};
  const calls = { intervals: [], timeouts: [], clearedTimeouts: [], clearedIntervals: [], fetched: [] };
  const elements = options.elements || {};

  global.window = {
    location: { pathname: '/history', search: options.search || '' },
    clearTimeout(id) { calls.clearedTimeouts.push(id); },
    setTimeout(fn, ms) { calls.timeouts.push({ fn, ms }); return calls.timeouts.length; },
  };
  global.document = { getElementById(id) { return elements[id] || null; } };
  global.setInterval = function (fn, ms) { calls.intervals.push({ fn, ms }); return calls.intervals.length; };
  global.clearInterval = function (id) { calls.clearedIntervals.push(id); };
  global.fetch = function (url) {
    calls.fetched.push(url);
    const responder = (options.responses || {})[url];
    return responder ? responder() : Promise.reject(new Error('no stub for ' + url));
  };

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  calls.window = global.window;
  return calls;
}

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// -------------------------------------------------------------- jump to page

run('a key that is not Enter leaves the page alone', () => {
  const chrome = loadChrome({});
  let handled = 0;
  chrome.window.__paginationAjaxHandler = () => { handled += 1; };

  chrome.window.handleJumpToPageKeydown({ key: 'a', target: { value: '3' }, preventDefault() {} }, 10);

  assert.strictEqual(handled, 0);
});

run('a page number above the last one is clamped to the last one', () => {
  const chrome = loadChrome({});
  const asked = [];
  chrome.window.__paginationAjaxHandler = (page) => asked.push(page);

  chrome.window.handleJumpToPageKeydown({ key: 'Enter', target: { value: '999' }, preventDefault() {} }, 7);

  assert.deepStrictEqual(asked, [7]);
});

run('a page number below one is clamped up to one', () => {
  const chrome = loadChrome({});
  const asked = [];
  chrome.window.__paginationAjaxHandler = (page) => asked.push(page);

  chrome.window.handleJumpToPageKeydown({ key: 'Enter', target: { value: '-4' }, preventDefault() {} }, 7);

  assert.deepStrictEqual(asked, [1]);
});

run('a page number inside the range is passed through untouched', () => {
  const chrome = loadChrome({});
  const asked = [];
  chrome.window.__paginationAjaxHandler = (page) => asked.push(page);

  chrome.window.handleJumpToPageKeydown({ key: 'Enter', target: { value: '4' }, preventDefault() {} }, 7);

  assert.deepStrictEqual(asked, [4]);
});

run('a box with nothing parseable in it asks for no page at all', () => {
  const chrome = loadChrome({});
  const asked = [];
  chrome.window.__paginationAjaxHandler = (page) => asked.push(page);

  chrome.window.handleJumpToPageKeydown({ key: 'Enter', target: { value: 'abc' }, preventDefault() {} }, 7);

  assert.deepStrictEqual(asked, [], 'NaN must not reach the handler as a page');
});

run('a page with no ajax handler navigates, keeping the other query params', () => {
  const chrome = loadChrome({ search: '?q=wu+tang&interval=last-30-days' });
  //< no __paginationAjaxHandler: every page except the htmx-driven ones

  chrome.window.handleJumpToPageKeydown({ key: 'Enter', target: { value: '3' }, preventDefault() {} }, 7);

  const url = new URL(chrome.window.location, 'http://localhost');
  assert.strictEqual(url.pathname, '/history');
  assert.strictEqual(url.searchParams.get('page'), '3');
  assert.strictEqual(url.searchParams.get('q'), 'wu tang', 'the existing filter survives the jump');
  assert.strictEqual(url.searchParams.get('interval'), 'last-30-days');
});

run('Enter is swallowed so the surrounding form does not also submit', () => {
  const chrome = loadChrome({});
  let prevented = 0;
  chrome.window.__paginationAjaxHandler = () => {};

  chrome.window.handleJumpToPageKeydown(
    { key: 'Enter', target: { value: '2' }, preventDefault() { prevented += 1; } }, 7);

  assert.strictEqual(prevented, 1);
});

// ----------------------------------------------------------------- searching

run('the search box applies its filter on Enter and swallows the key', () => {
  const chrome = loadChrome({});
  let applied = 0, prevented = 0;

  chrome.window.handleSearchKeydown(
    { key: 'Enter', preventDefault() { prevented += 1; } }, () => { applied += 1; });

  assert.strictEqual(applied, 1);
  assert.strictEqual(prevented, 1);
});

run('an ordinary keystroke does not reload the page mid-word', () => {
  const chrome = loadChrome({});
  let applied = 0;

  chrome.window.handleSearchKeydown({ key: 'x', preventDefault() {} }, () => { applied += 1; });

  assert.strictEqual(applied, 0);
});

run('leaving the search box unchanged asks for nothing', () => {
  const chrome = loadChrome({ search: '?q=nujabes' });
  let applied = 0;

  chrome.window.handleSearchBlur({ target: { value: '  nujabes  ' } }, () => { applied += 1; });

  assert.strictEqual(applied, 0, 'only whitespace differed, so there is no new query');
});

run('leaving the search box with a different term applies it', () => {
  const chrome = loadChrome({ search: '?q=nujabes' });
  let applied = 0;

  chrome.window.handleSearchBlur({ target: { value: 'madlib' } }, () => { applied += 1; });

  assert.strictEqual(applied, 1);
});

run('a second keystroke cancels the first pending search', () => {
  const searchBox = makeElement();
  const chrome = loadChrome({ elements: { songSearch: searchBox } });

  chrome.window.scheduleSearchFilter('songSearch', () => {}, 500);
  chrome.window.scheduleSearchFilter('songSearch', () => {}, 500);

  assert.strictEqual(chrome.timeouts.length, 2);
  assert.ok(chrome.clearedTimeouts.includes(1), 'the first timer was cancelled, not left to fire');
});

run('the debounced filter runs the callback when its timer fires', () => {
  const searchBox = makeElement();
  const chrome = loadChrome({ elements: { songSearch: searchBox } });
  let applied = 0;

  chrome.window.scheduleSearchFilter('songSearch', () => { applied += 1; }, 500);
  assert.strictEqual(applied, 0, 'not before the delay');
  chrome.timeouts[0].fn();

  assert.strictEqual(applied, 1);
});

run('a search box that is not on this page is a no-op, not a crash', () => {
  const chrome = loadChrome({});

  chrome.window.scheduleSearchFilter('missingBox', () => {}, 500);

  assert.strictEqual(chrome.timeouts.length, 0);
});

// ------------------------------------------------------------ version badge

run('the badge appears only when a newer release exists', async () => {
  const badge = makeElement();
  const text = makeElement();
  const chrome = loadChrome({
    elements: { 'version-badge': badge, 'version-badge-text': text },
    responses: { '/version_status': () => Promise.resolve({ json: () => Promise.resolve({ current: '1.46.5', latest: '1.47.0' }) }) },
  });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(badge.style.display, 'inline-flex');
  assert.ok(text.textContent.includes('1.47.0'), text.textContent);
  assert.ok(text.textContent.includes('1.46.5'), 'it also says which one you are on');
  //< `includes`, not equality: loading this file also arms the listener-status
  //  poll, which fetches on its own before the first interval tick
  assert.ok(chrome.fetched.includes('/version_status'));
});

run('an up-to-date instance shows no badge', async () => {
  const badge = makeElement();
  const text = makeElement();
  loadChrome({
    elements: { 'version-badge': badge, 'version-badge-text': text },
    responses: { '/version_status': () => Promise.resolve({ json: () => Promise.resolve({ current: '1.46.5', latest: null }) }) },
  });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(badge.style.display, 'none');
});

// ----------------------------------------------------- listener status pill

function pillResponses(status) {
  return { '/api/listener-status': () => Promise.resolve({ status: 200, json: () => Promise.resolve({ status }) }) };
}

run('the sync pill reports the listener state', async () => {
  const pill = makeElement();
  loadChrome({ elements: { 'listener-status-pill': pill }, responses: pillResponses('active') });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(pill.className, 'status-pill status-active');
  assert.strictEqual(pill.title, 'Sync Status: Active');
  assert.strictEqual(pill.style.display, 'inline-block');
});

run('an expired session stops the poll instead of hiding the pill and polling on', async () => {
  const pill = makeElement();
  const chrome = loadChrome({
    elements: { 'listener-status-pill': pill },
    responses: { '/api/listener-status': () => Promise.resolve({ status: 401, json: () => Promise.resolve({}) }) },
  });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(pill.style.display, 'none');
  const listenerInterval = chrome.intervals.find(i => i.ms === 10 * 1000);
  assert.ok(listenerInterval, 'the 10s poll is armed at load');
  assert.ok(chrome.clearedIntervals.length >= 1,
            'the interval is cleared, or the tab keeps hitting the server every 10s forever');
});

// ------------------------------------------------------------- nav menu

run('the burger reports its state to screen readers, both ways', () => {
  const navToggle = makeElement();
  const navMenu = makeElement();
  loadChrome({ elements: { 'nav-toggle': navToggle, 'nav-menu': navMenu } });

  navToggle.handlers.click();
  assert.strictEqual(navToggle.attributes['aria-expanded'], 'true');

  navToggle.handlers.click();
  assert.strictEqual(navToggle.attributes['aria-expanded'], 'false');
});

run('following a link closes the menu and says so', () => {
  const navToggle = makeElement();
  const navMenu = makeElement();
  const link = makeElement();
  navMenu.links = [link];
  loadChrome({ elements: { 'nav-toggle': navToggle, 'nav-menu': navMenu } });
  navToggle.handlers.click();   //< open it first

  link.handlers.click();

  assert.strictEqual(navMenu.classList.contains('active'), false);
  assert.strictEqual(navToggle.attributes['aria-expanded'], 'false');
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
  console.log(`all ${results.length} layout-chrome tests passed`);
})();
