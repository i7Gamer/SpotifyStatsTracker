// Plain-node unit test for static/js/session-reset.js - the htmx page-snapshot
// cache is dropped when the login page is reached.
//
// No test framework/dependency - run with:
//   node tests/test_session_reset.js
const assert = require('assert');
const { clearHtmxHistoryCache, sessionStorageOrNull, HTMX_HISTORY_CACHE_KEY } =
  require('../static/js/session-reset.js');

function withWindow(stub, fn) {
  const had = Object.prototype.hasOwnProperty.call(global, 'window');
  const previous = global.window;
  global.window = stub;
  try {
    fn();
  } finally {
    if (had) { global.window = previous; } else { delete global.window; }
  }
}

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

function fakeStorage(initial) {
  const data = Object.assign({}, initial);
  return {
    data,
    removed: [],
    removeItem(key) { this.removed.push(key); delete data[key]; },
  };
}

run('the htmx snapshot cache is removed', () => {
  // The whole point: after a logout, Back must not repaint the previous
  // account's pages from storage.
  const storage = fakeStorage({ [HTMX_HISTORY_CACHE_KEY]: '[{"url":"/","content":"..."}]' });

  assert.strictEqual(clearHtmxHistoryCache(storage), true);
  assert.strictEqual(HTMX_HISTORY_CACHE_KEY in storage.data, false);
});

run('it is htmx\'s key that is targeted, and nothing else', () => {
  // The theme lives in localStorage, but a future sessionStorage key would be
  // collateral damage if this cleared the whole store instead.
  const storage = fakeStorage({
    [HTMX_HISTORY_CACHE_KEY]: '[]',
    'something-else': 'keep me',
  });

  clearHtmxHistoryCache(storage);

  assert.deepStrictEqual(storage.removed, [HTMX_HISTORY_CACHE_KEY]);
  assert.strictEqual(storage.data['something-else'], 'keep me');
});

run('the key is htmx\'s own spelling', () => {
  // htmx does not export it, so a typo here would clear nothing and the whole
  // file would be silently inert.
  assert.strictEqual(HTMX_HISTORY_CACHE_KEY, 'htmx-history-cache');
});

run('an already-clean store is not an error', () => {
  const storage = fakeStorage({});

  assert.strictEqual(clearHtmxHistoryCache(storage), true);
});

run('storage that refuses to be touched does not break the login page', () => {
  // Storage access THROWS rather than returning null when site data is blocked.
  // This runs on the page someone is trying to log in on, so it must not take
  // the form down over a cache that may not even exist.
  const storage = {
    removeItem() { throw new Error('SecurityError: access denied'); },
  };

  assert.doesNotThrow(() => clearHtmxHistoryCache(storage));
  assert.strictEqual(clearHtmxHistoryCache(storage), false);
});

run('reading sessionStorage at all is guarded, not just using it', () => {
  // The guard used to be `window.sessionStorage` at the CALL SITE, outside
  // clearHtmxHistoryCache's try - but the property ACCESS is what throws when
  // site data is blocked, which is the exact case the file is written for. So
  // the browser this runs in got an uncaught SecurityError on the login page,
  // and the test above could never see it: it only ever exercised the function.
  withWindow({ get sessionStorage() { throw new Error('SecurityError: access denied'); } }, () => {
    assert.doesNotThrow(() => sessionStorageOrNull());
    assert.strictEqual(sessionStorageOrNull(), null);
  });
});

run('a browser with storage hands it over', () => {
  const storage = fakeStorage({});

  withWindow({ sessionStorage: storage }, () => {
    assert.strictEqual(sessionStorageOrNull(), storage);
  });
});

run('outside a browser there is nothing to clear', () => {
  // Node requiring this module for these very tests is the case.
  assert.strictEqual(sessionStorageOrNull(), null);
});

console.log('All session reset tests passed.');
