// Plain-node unit test for admin page AJAX tab logic (static/js/admin-page.js).
// Run with: node tests/test_admin_page.js
const assert = require('assert');

// Collected here and run in sequence at the bottom. The navigate() tests are
// async - they resolve two in-flight fetches out of order - and a
// call-immediately runner would report a pass before their assertions ran.
const results = [];
function run(name, fn) { results.push({ name, fn }); }

// Captured before any test runs. setupNavigableWindow stubs both, and a stub
// left installed would silently serve any later test that feature-detects what
// it replaced - `typeof DOMParser !== 'undefined'` is exactly how admin-page.js
// itself decides whether it can parse a response. The last test asserts they
// were handed back. (window/document/history are not in scope here: node has no
// baseline for them, so every test installs its own outright.)
const BASELINE_FETCH = global.fetch;
const BASELINE_PARSER = global.DOMParser;

// Mock DOM environment for node execution
function setupMockWindow(initialUrl = 'http://localhost/admin') {
  const listeners = {};
  let currentState = null;
  let currentUrl = initialUrl;

  const document = {
    querySelector: (sel) => {
      if (sel === '.admin-subnav') {
        return {
          _adminAjaxWired: false,
          addEventListener: (evt, handler) => {
            listeners[evt] = handler;
          },
          querySelectorAll: () => []
        };
      }
      return null;
    },
    getElementById: (id) => {
      if (id === 'admin-tab-body') {
        return {
          innerHTML: '<div>Initial Content</div>',
          classList: {
            add: () => {},
            remove: () => {}
          },
          querySelectorAll: () => []
        };
      }
      return null;
    }
  };

  const history = {
    state: null,
    replaceState: (state, title, url) => {
      currentState = state;
      if (url) currentUrl = url;
      history.state = state;
    },
    pushState: (state, title, url) => {
      currentState = state;
      if (url) currentUrl = url;
      history.state = state;
    }
  };

  const windowMock = {
    location: {
      href: currentUrl,
      origin: 'http://localhost'
    },
    history,
    document,
    addEventListener: (evt, handler) => {
      listeners[evt] = handler;
    },
    listeners,
    getCurrentState: () => currentState
  };

  return windowMock;
}

// A window whose fetches are held open, so a test can decide the order the tab
// responses land in. The body element is a single stable object (the mock above
// hands out a fresh one per call, which is fine for init but would hide every
// swap).
function setupNavigableWindow(url) {
  const body = {
    innerHTML: '<div>Initial</div>',
    //< a real classList is a SET: two navigates add the same class once, so one
    //  of them removing it takes it away from both
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    querySelectorAll: () => [],
  };
  const nav = { _adminAjaxWired: false, addEventListener() {}, querySelectorAll: () => [] };
  const pending = [];
  const priorFetch = global.fetch;
  const priorParser = global.DOMParser;

  global.document = {
    querySelector: (sel) => (sel === '.admin-subnav' ? nav : null),
    getElementById: (id) => (id === 'admin-tab-body' ? body : null),
  };
  global.location = { href: url, origin: 'http://localhost' };
  global.window = { location: global.location, addEventListener() {} };
  global.history = { state: null, replaceState() {}, pushState() {} };
  global.fetch = () => new Promise((resolve) => { pending.push(resolve); });
  global.DOMParser = function () {
    this.parseFromString = (html) => ({
      getElementById: (id) => (id === 'admin-tab-body' ? { innerHTML: html } : null),
    });
  };

  delete require.cache[require.resolve('../static/js/admin-page.js')];
  const AdminPage = require('../static/js/admin-page.js');
  //< called from a finally, so a failed assertion leaves no stub behind either
  const restore = () => { global.fetch = priorFetch; global.DOMParser = priorParser; };
  return { AdminPage, body, pending, restore };
}

function tabResponse(html) {
  return { ok: true, text: () => Promise.resolve(html) };
}

run('a superseded tab response never lands', async () => {
  // Two quick clicks. Nothing cancels the first fetch, so when it answers last
  // it used to swap its body in on top - leaving the Sync tab on screen under a
  // URL and a highlighted nav link that both said Users.
  const { AdminPage, body, pending, restore } = setupNavigableWindow('http://localhost/admin');
  try {
    const first = AdminPage.navigate('/admin?tab=sync');
    const second = AdminPage.navigate('/admin?tab=users');
    assert.strictEqual(pending.length, 2, 'both requests are in flight');

    pending[1](tabResponse('USERS'));
    await second;
    pending[0](tabResponse('SYNC'));
    await first;

    assert.strictEqual(body.innerHTML, 'USERS',
      'the tab the user navigated away from must not overwrite the one they asked for');
  } finally {
    restore();
  }
});

run('a superseded response does not clear the loading state of the live one', async () => {
  const { AdminPage, body, pending, restore } = setupNavigableWindow('http://localhost/admin');
  try {
    const first = AdminPage.navigate('/admin?tab=sync');
    const second = AdminPage.navigate('/admin?tab=users');

    pending[0](tabResponse('SYNC'));   //< the abandoned tab answers first
    await first;

    assert.ok(body.classList.contains('admin-tab-loading'),
      'the newer request is still open, so the tab body must still read as loading');

    pending[1](tabResponse('USERS'));
    await second;
    assert.ok(!body.classList.contains('admin-tab-loading'), 'and is cleared once the live one lands');
  } finally {
    restore();
  }
});

run('init seeds its own state even when a foreign one is already present', () => {
  // `!history.state` also skipped the seed for an entry whose state was written
  // by somebody else, and the popstate handler only re-renders on state.adminTab
  // - so Back would leave the swapped tab on screen under the original URL.
  const win = setupMockWindow('http://localhost/admin?tab=overview');
  global.window = win;
  global.document = win.document;
  global.history = win.history;
  global.location = win.location;
  win.history.state = { notOurs: true };

  delete require.cache[require.resolve('../static/js/admin-page.js')];
  const AdminPage = require('../static/js/admin-page.js');

  AdminPage.init();

  assert.strictEqual(win.history.state.adminTab, 'http://localhost/admin?tab=overview');
});

run('init sets initial history state if null', () => {
  const win = setupMockWindow('http://localhost/admin?tab=overview');
  global.window = win;
  global.document = win.document;
  global.history = win.history;
  global.location = win.location;

  // Clear module cache to re-require with fresh window mock
  delete require.cache[require.resolve('../static/js/admin-page.js')];
  const AdminPage = require('../static/js/admin-page.js');

  AdminPage.init();

  assert.notStrictEqual(win.history.state, null, 'history.state should not be null after init');
  assert.strictEqual(win.history.state.adminTab, 'http://localhost/admin?tab=overview');
});

run('modifier clicks on the subnav pass through to the browser', () => {
  // Ctrl/Cmd+click means "open in a new tab", Shift "new window", Alt
  // "download" - the handler used to preventDefault them all, so the only way
  // to open an admin tab beside the current one was the address bar.
  const win = setupMockWindow('http://localhost/admin?tab=overview');
  global.window = win;
  global.document = win.document;
  global.history = win.history;
  global.location = win.location;
  delete require.cache[require.resolve('../static/js/admin-page.js')];
  const AdminPage = require('../static/js/admin-page.js');
  AdminPage.init();
  const seededState = win.history.state;

  const link = { getAttribute: () => '/admin?tab=users' };
  let prevented = 0;
  const modifiers = [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true },
                     { altKey: true }, { button: 1 }];
  for (const mod of modifiers) {
    win.listeners.click(Object.assign({
      target: { closest: () => link },
      preventDefault() { prevented += 1; },
    }, mod));
  }

  assert.strictEqual(prevented, 0, 'a modified click must reach the browser untouched');
  assert.strictEqual(win.history.state, seededState, 'and must push no history entry');
});

run('a plain subnav click is still intercepted', () => {
  const win = setupMockWindow('http://localhost/admin?tab=overview');
  global.window = win;
  global.document = win.document;
  global.history = win.history;
  global.location = win.location;
  const priorFetch = global.fetch;
  global.fetch = () => new Promise(() => {});   //< navigate() fires one; hold it open
  try {
    delete require.cache[require.resolve('../static/js/admin-page.js')];
    const AdminPage = require('../static/js/admin-page.js');
    AdminPage.init();

    let prevented = 0;
    win.listeners.click({
      target: { closest: () => ({ getAttribute: () => '/admin?tab=users' }) },
      preventDefault() { prevented += 1; },
      ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, button: 0,
    });

    assert.strictEqual(prevented, 1, 'the passthrough must not over-return on plain clicks');
  } finally {
    global.fetch = priorFetch;
  }
});

//< runs last on purpose: it checks what the tests above left behind
run('no navigate stub outlived its test', () => {
  assert.strictEqual(global.fetch, BASELINE_FETCH, 'a fetch stub is still installed');
  assert.strictEqual(global.DOMParser, BASELINE_PARSER, 'a DOMParser stub is still installed');
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
  console.log(`All ${results.length} admin-page JS tests passed.`);
})();
