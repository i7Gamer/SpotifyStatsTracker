// Plain-node unit test for the back-button referrer resolution logic
// (static/js/back-button.js). No test framework/dependency - run with:
//   node tests/test_back_button.js
const assert = require('assert');
const {
  resolveBackTarget, hasEarlierHistoryEntry, backButtonLabelStorageKey, initBackButton,
} = require('../static/js/back-button.js');

const ORIGIN = 'http://localhost:5000';
// Second argument of resolveBackTarget: the URL of the detail page the button
// is on. Only its origin matters unless the referrer IS that same page.
const CURRENT_PAGE = `${ORIGIN}/artist/current-page`;
// Third argument: whether this tab has an entry to go back to at all. False is
// the "opened in a new tab" case.
const CAN_GO_BACK = true;
const FRESH_TAB = false;
// Fourth argument: the label a previous load of this same page remembered.
const NOTHING_REMEMBERED = null;

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

function backTo(label) {
  return { hide: false, label };
}

run('empty referrer keeps server-rendered default', () => {
  assert.strictEqual(resolveBackTarget('', CURRENT_PAGE, CAN_GO_BACK), null);
});

run('cross-origin referrer keeps server-rendered default', () => {
  assert.strictEqual(resolveBackTarget('https://google.com/search?q=x', CURRENT_PAGE, CAN_GO_BACK), null);
});

run('unparseable referrer keeps server-rendered default', () => {
  assert.strictEqual(resolveBackTarget('not a url', CURRENT_PAGE, CAN_GO_BACK), null);
});

run('dashboard root referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to Dashboard'));
});

run('history referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/history`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to History'));
});

run('history referrer with search + pagination keeps its label', () => {
  assert.deepStrictEqual(
    resolveBackTarget(`${ORIGIN}/history?q=daft&interval=year&page=2`, CURRENT_PAGE, CAN_GO_BACK),
    backTo('← Back to History'));
});

run('wrapped referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/wrapped`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to Wrapped'));
});

run('compare referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/compare`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to Compare'));
});

run('genres referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/genres`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to Genres'));
});

run('genres referrer with a selected genre keeps its label', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/genres?genre=rock`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to Genres'));
});

run('compare referrer with filters keeps its label', () => {
  assert.deepStrictEqual(
    resolveBackTarget(`${ORIGIN}/compare?with=bob&interval=year&limit=25`, CURRENT_PAGE, CAN_GO_BACK),
    backTo('← Back to Compare'));
});

run('top-songs referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/top-songs`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to Top Songs'));
});

run('top-albums referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/top-albums`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to Top Albums'));
});

run('top-artists referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/top-artists`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to Top Artists'));
});

run('song detail referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/song/abc123`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to Song'));
});

run('album detail referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/album/abc123`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to Album'));
});

run('artist detail referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/artist/abc123`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to Artist'));
});

run('unrecognized same-origin path still allows history.back(), but keeps default label', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/charts`, CURRENT_PAGE, CAN_GO_BACK), backTo(null));
});

run('regression: dashboard search query containing a reserved word is not misread as that page', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/?q=wrapped`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to Dashboard'));
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/?q=top-albums&page=2`, CURRENT_PAGE, CAN_GO_BACK), backTo('← Back to Dashboard'));
});

// Clicking a link to the page you are already on (an artist page's song list
// links its own artist, and so on) reloads it with ITSELF as the referrer, and
// the browser replaces the tab's history entry rather than adding one - so Back
// still leads exactly where it did before the click. The label the previous
// load resolved is carried over instead of being re-derived from that referrer,
// which would have relabelled the button "← Back to Artist" while standing on
// that very artist.

run('a link back to the same page keeps the label the previous load resolved', () => {
  assert.deepStrictEqual(
    resolveBackTarget(CURRENT_PAGE, CURRENT_PAGE, CAN_GO_BACK, '← Back to Top Artists'),
    backTo('← Back to Top Artists'));
});

run('same-page link after an unlabelled referrer still allows history.back()', () => {
  assert.deepStrictEqual(resolveBackTarget(CURRENT_PAGE, CURRENT_PAGE, CAN_GO_BACK, ''), backTo(null));
});

run('same-page link with nothing remembered keeps the server-rendered default', () => {
  assert.strictEqual(
    resolveBackTarget(CURRENT_PAGE, CURRENT_PAGE, CAN_GO_BACK, NOTHING_REMEMBERED), null);
});

run('a same-path link that is not the same URL is a real navigation, not a stay', () => {
  // The list's sort/tab controls replaceState the query in, so a plain link to
  // this page is then a different URL: Back really does land on this page.
  assert.deepStrictEqual(
    resolveBackTarget(`${CURRENT_PAGE}?view=history`, CURRENT_PAGE, CAN_GO_BACK, '← Back to Top Artists'),
    backTo('← Back to Artist'));
});

run('a remembered label cannot resurrect the button in a tab with no earlier entry', () => {
  assert.deepStrictEqual(
    resolveBackTarget(CURRENT_PAGE, CURRENT_PAGE, FRESH_TAB, '← Back to Top Artists'), { hide: true });
});

// The remembered label is keyed by the page it was resolved for, so two detail
// pages open in one tab can't read each other's.
run('storage key is per page, query included', () => {
  assert.notStrictEqual(
    backButtonLabelStorageKey(`${ORIGIN}/artist/a`), backButtonLabelStorageKey(`${ORIGIN}/artist/b`));
  assert.notStrictEqual(
    backButtonLabelStorageKey(`${ORIGIN}/artist/a`), backButtonLabelStorageKey(`${ORIGIN}/artist/a?view=history`));
});

run('storage key ignores the fragment, which a referrer never carries', () => {
  assert.strictEqual(
    backButtonLabelStorageKey(`${ORIGIN}/artist/a#top`), backButtonLabelStorageKey(`${ORIGIN}/artist/a`));
});

// A tab with nothing behind it has nowhere to go back to, whatever brought it
// there - hide the button rather than show a dead one (history.back() would do
// nothing) or a guess at a page the user never visited (the server default).

// "Open in new tab" from inside the app: the referrer is a real in-app page.
run('in-app referrer in a tab with no earlier entry hides the button', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/top-songs`, CURRENT_PAGE, FRESH_TAB), { hide: true });
});

run('unrecognized in-app referrer in a tab with no earlier entry hides the button', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/charts`, CURRENT_PAGE, FRESH_TAB), { hide: true });
});

// Pasted URL, bookmark, or a shared link opened in a new tab: no referrer at
// all, so the label logic has nothing to work with - but there is still no
// earlier entry, so the button must go.
run('direct visit with no earlier entry hides the button', () => {
  assert.deepStrictEqual(resolveBackTarget('', CURRENT_PAGE, FRESH_TAB), { hide: true });
});

// Shared link followed from another site into a fresh tab.
run('cross-origin referrer with no earlier entry hides the button', () => {
  assert.deepStrictEqual(resolveBackTarget('https://google.com/', CURRENT_PAGE, FRESH_TAB), { hide: true });
});

run('unparseable referrer with no earlier entry hides the button', () => {
  assert.deepStrictEqual(resolveBackTarget('not a url', CURRENT_PAGE, FRESH_TAB), { hide: true });
});

run('Navigation API answer wins over history length when present', () => {
  assert.strictEqual(hasEarlierHistoryEntry({ canGoBack: false }, 5), false);
  assert.strictEqual(hasEarlierHistoryEntry({ canGoBack: true }, 1), true);
});

run('history length is the fallback when the Navigation API is unavailable', () => {
  assert.strictEqual(hasEarlierHistoryEntry(undefined, 1), false);
  assert.strictEqual(hasEarlierHistoryEntry(undefined, 2), true);
});

run('a Navigation object without canGoBack falls back to history length', () => {
  assert.strictEqual(hasEarlierHistoryEntry({}, 1), false);
  assert.strictEqual(hasEarlierHistoryEntry({}, 3), true);
});

// --- initBackButton ---------------------------------------------------------
// The carry-over above only holds up if a load actually remembers what it
// resolved, which is wiring the pure function can't show. A hand-rolled stub
// (same approach as tests/test_ajax_status.js, no jsdom dependency) stands in
// for the page; one `storage` object shared across calls is the tab's
// sessionStorage surviving the reloads.

const SERVER_DEFAULT_LABEL = '← Back to Top Artists';
const SERVER_DEFAULT_HREF = '/top-artists';
const HISTORY_WITH_BACK_ENTRY = 2;
const HISTORY_FRESH_TAB = 1;

function loadPage(href, referrer, storage, historyLength = HISTORY_WITH_BACK_ENTRY) {
  const button = {
    textContent: SERVER_DEFAULT_LABEL, href: SERVER_DEFAULT_HREF, hidden: false, onclick: null,
  };
  global.window = {
    location: { href },
    history: { length: historyLength },
    sessionStorage: {
      getItem(key) { return key in storage ? storage[key] : null; },
      setItem(key, value) { storage[key] = value; },
    },
  };
  global.document = {
    referrer,
    getElementById(id) { return id === 'back-button' ? button : null; },
  };
  initBackButton();
  return button;
}

run('a load remembers its label, and a link back to the same page reuses it', () => {
  const storage = {};
  const arrived = loadPage(CURRENT_PAGE, `${ORIGIN}/genres`, storage);
  assert.strictEqual(arrived.textContent, '← Back to Genres');

  //< the same page again, this time reached from itself
  const stayed = loadPage(CURRENT_PAGE, CURRENT_PAGE, storage);
  assert.strictEqual(stayed.textContent, '← Back to Genres');
  assert.strictEqual(stayed.href, '#');
  assert.strictEqual(typeof stayed.onclick, 'function');
});

run('another page in the same tab cannot read that memory', () => {
  const storage = {};
  loadPage(CURRENT_PAGE, `${ORIGIN}/genres`, storage);

  const other = loadPage(`${ORIGIN}/artist/other`, `${ORIGIN}/artist/other`, storage);
  assert.strictEqual(other.textContent, SERVER_DEFAULT_LABEL);
  assert.strictEqual(other.href, SERVER_DEFAULT_HREF);
});

run('a tab with no earlier entry hides the button and remembers nothing', () => {
  const storage = {};
  const button = loadPage(CURRENT_PAGE, `${ORIGIN}/genres`, storage, HISTORY_FRESH_TAB);
  assert.strictEqual(button.hidden, true);
  assert.deepStrictEqual(storage, {});
});

console.log('All back-button tests passed.');
