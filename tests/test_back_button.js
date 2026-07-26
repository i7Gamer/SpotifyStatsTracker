// Plain-node unit test for the back-button referrer resolution logic
// (static/js/back-button.js). No test framework/dependency - run with:
//   node tests/test_back_button.js
const assert = require('assert');
const { resolveBackTarget, hasEarlierHistoryEntry } = require('../static/js/back-button.js');

const ORIGIN = 'http://localhost:5000';
// Third argument of resolveBackTarget: whether this tab has an entry to go
// back to at all. False is the "opened in a new tab" case.
const CAN_GO_BACK = true;
const FRESH_TAB = false;

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
  assert.strictEqual(resolveBackTarget('', ORIGIN, CAN_GO_BACK), null);
});

run('cross-origin referrer keeps server-rendered default', () => {
  assert.strictEqual(resolveBackTarget('https://google.com/search?q=x', ORIGIN, CAN_GO_BACK), null);
});

run('unparseable referrer keeps server-rendered default', () => {
  assert.strictEqual(resolveBackTarget('not a url', ORIGIN, CAN_GO_BACK), null);
});

run('dashboard root referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/`, ORIGIN, CAN_GO_BACK), backTo('← Back to Dashboard'));
});

run('history referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/history`, ORIGIN, CAN_GO_BACK), backTo('← Back to History'));
});

run('history referrer with search + pagination keeps its label', () => {
  assert.deepStrictEqual(
    resolveBackTarget(`${ORIGIN}/history?q=daft&interval=year&page=2`, ORIGIN, CAN_GO_BACK),
    backTo('← Back to History'));
});

run('wrapped referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/wrapped`, ORIGIN, CAN_GO_BACK), backTo('← Back to Wrapped'));
});

run('compare referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/compare`, ORIGIN, CAN_GO_BACK), backTo('← Back to Compare'));
});

run('genres referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/genres`, ORIGIN, CAN_GO_BACK), backTo('← Back to Genres'));
});

run('genres referrer with a selected genre keeps its label', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/genres?genre=rock`, ORIGIN, CAN_GO_BACK), backTo('← Back to Genres'));
});

run('compare referrer with filters keeps its label', () => {
  assert.deepStrictEqual(
    resolveBackTarget(`${ORIGIN}/compare?with=bob&interval=year&limit=25`, ORIGIN, CAN_GO_BACK),
    backTo('← Back to Compare'));
});

run('top-songs referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/top-songs`, ORIGIN, CAN_GO_BACK), backTo('← Back to Top Songs'));
});

run('top-albums referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/top-albums`, ORIGIN, CAN_GO_BACK), backTo('← Back to Top Albums'));
});

run('top-artists referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/top-artists`, ORIGIN, CAN_GO_BACK), backTo('← Back to Top Artists'));
});

run('song detail referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/song/abc123`, ORIGIN, CAN_GO_BACK), backTo('← Back to Song'));
});

run('album detail referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/album/abc123`, ORIGIN, CAN_GO_BACK), backTo('← Back to Album'));
});

run('artist detail referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/artist/abc123`, ORIGIN, CAN_GO_BACK), backTo('← Back to Artist'));
});

run('unrecognized same-origin path still allows history.back(), but keeps default label', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/charts`, ORIGIN, CAN_GO_BACK), backTo(null));
});

run('regression: dashboard search query containing a reserved word is not misread as that page', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/?q=wrapped`, ORIGIN, CAN_GO_BACK), backTo('← Back to Dashboard'));
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/?q=top-albums&page=2`, ORIGIN, CAN_GO_BACK), backTo('← Back to Dashboard'));
});

// A tab with nothing behind it has nowhere to go back to, whatever brought it
// there - hide the button rather than show a dead one (history.back() would do
// nothing) or a guess at a page the user never visited (the server default).

// "Open in new tab" from inside the app: the referrer is a real in-app page.
run('in-app referrer in a tab with no earlier entry hides the button', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/top-songs`, ORIGIN, FRESH_TAB), { hide: true });
});

run('unrecognized in-app referrer in a tab with no earlier entry hides the button', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/charts`, ORIGIN, FRESH_TAB), { hide: true });
});

// Pasted URL, bookmark, or a shared link opened in a new tab: no referrer at
// all, so the label logic has nothing to work with - but there is still no
// earlier entry, so the button must go.
run('direct visit with no earlier entry hides the button', () => {
  assert.deepStrictEqual(resolveBackTarget('', ORIGIN, FRESH_TAB), { hide: true });
});

// Shared link followed from another site into a fresh tab.
run('cross-origin referrer with no earlier entry hides the button', () => {
  assert.deepStrictEqual(resolveBackTarget('https://google.com/', ORIGIN, FRESH_TAB), { hide: true });
});

run('unparseable referrer with no earlier entry hides the button', () => {
  assert.deepStrictEqual(resolveBackTarget('not a url', ORIGIN, FRESH_TAB), { hide: true });
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

console.log('All back-button tests passed.');
