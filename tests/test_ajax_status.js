// Plain-node unit test for the shared AJAX failure UI (static/js/ajax-status.js).
// Uses a tiny hand-rolled DOM stub (no jsdom dependency) - enough to exercise the
// element-building and the Retry wiring. Run with: node tests/test_ajax_status.js
const assert = require('assert');

function makeNode(byId) {
  const node = {
    className: '', textContent: '', type: '', innerHTML: '', _id: '',
    children: [], _handlers: {}, _parent: null,
    classList: { remove() {}, add() {} },
    appendChild(child) { child._parent = this; this.children.push(child); return child; },
    insertBefore(child) { child._parent = this; this.children.unshift(child); return child; },
    removeChild(child) {
      const i = this.children.indexOf(child);
      if (i >= 0) this.children.splice(i, 1);
      return child;
    },
    addEventListener(type, fn) { this._handlers[type] = fn; },
    get firstChild() { return this.children[0] || null; },
    get parentNode() { return this._parent; },
  };
  Object.defineProperty(node, 'id', {
    get() { return node._id; },
    set(value) { node._id = value; if (value && byId) byId[value] = node; },
  });
  return node;
}

function installDom() {
  const byId = {};
  const main = makeNode(byId);
  global.window = {};
  global.document = {
    createElement() { return makeNode(byId); },
    querySelector(sel) { return sel === 'main' ? main : null; },
    getElementById(id) { return byId[id] || null; },
  };
  return { byId, main };
}

const AjaxStatus = require('../static/js/ajax-status.js');

function run(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { console.error(`FAIL - ${name}`); throw err; }
}

function findButton(node) {
  if (node.type === 'button') return node;
  for (const child of node.children || []) {
    const found = findButton(child);
    if (found) return found;
  }
  return null;
}

run('exports the documented API', () => {
  assert.strictEqual(typeof AjaxStatus.renderInto, 'function');
  assert.strictEqual(typeof AjaxStatus.showBanner, 'function');
  assert.strictEqual(typeof AjaxStatus.clearBanner, 'function');
  assert.ok(AjaxStatus.DEFAULT_MESSAGE && typeof AjaxStatus.DEFAULT_MESSAGE === 'string');
});

run('renderInto builds a Retry button wired to the callback', () => {
  installDom();
  const target = makeNode();
  let retried = 0;
  AjaxStatus.renderInto(target, () => { retried++; });
  const btn = findButton(target);
  assert.ok(btn, 'a button was rendered');
  assert.strictEqual(btn.textContent, 'Retry');
  btn._handlers.click();
  assert.strictEqual(retried, 1);
});

run('renderInto is a no-op on a missing target', () => {
  installDom();
  assert.doesNotThrow(() => AjaxStatus.renderInto(null, () => {}));
});

run('showBanner adds a Retry banner, and Retry clears it then re-fires', () => {
  const { byId, main } = installDom();
  let retried = 0;
  AjaxStatus.showBanner(() => { retried++; });
  const banner = byId['ajax-error-banner'];
  assert.ok(banner, 'banner created');
  assert.ok(main.children.includes(banner), 'banner attached to main');

  const btn = findButton(banner);
  assert.ok(btn && btn.textContent === 'Retry', 'banner has a Retry button');
  btn._handlers.click();
  assert.strictEqual(retried, 1, 'Retry invoked the callback');
  assert.ok(!main.children.includes(banner), 'Retry removed the banner');
});

run('showBanner reuses the existing banner instead of stacking', () => {
  const { main } = installDom();
  AjaxStatus.showBanner(() => {});
  AjaxStatus.showBanner(() => {});
  const banners = main.children.filter(c => c.id === 'ajax-error-banner');
  assert.strictEqual(banners.length, 1);
});

run('clearBanner is safe when no banner exists', () => {
  installDom();
  assert.doesNotThrow(() => AjaxStatus.clearBanner());
});

// --- expired-session handling ------------------------------------------------
// A 302 to /login is followed transparently by fetch(), so the loader used to
// parse the login page's HTML as JSON and show "couldn't load" with a Retry
// that failed identically forever. Routes now answer ?ajax= with a 401 and the
// loaders route it through here.

function installDomWithLocation(pathname, search) {
  const dom = installDom();
  global.window.location = {
    pathname: pathname, search: search, href: pathname + search,
  };
  return dom;
}

run('a 401 navigates to the login page', () => {
  installDomWithLocation('/charts', '?interval=week');

  const handled = AjaxStatus.redirectIfUnauthorized({ status: 401 });

  assert.strictEqual(handled, true);
  assert.strictEqual(
    global.window.location.href,
    '/login?next=' + encodeURIComponent('/charts?interval=week'));
});

run('the login redirect comes back to the page the user was on', () => {
  installDomWithLocation('/top-songs', '?sortBy=plays&page=3');

  AjaxStatus.redirectIfUnauthorized({ status: 401 });

  assert.ok(global.window.location.href.indexOf(encodeURIComponent('page=3')) > -1);
});

run('a normal response is left alone', () => {
  installDomWithLocation('/charts', '');

  assert.strictEqual(AjaxStatus.redirectIfUnauthorized({ status: 200, ok: true }), false);
  assert.strictEqual(global.window.location.href, '/charts');
});

run('a server error is left alone (it gets the retry banner instead)', () => {
  installDomWithLocation('/charts', '');

  assert.strictEqual(AjaxStatus.redirectIfUnauthorized({ status: 500 }), false);
  assert.strictEqual(global.window.location.href, '/charts');
});

run('a missing response is not treated as unauthorized', () => {
  installDomWithLocation('/charts', '');

  assert.strictEqual(AjaxStatus.redirectIfUnauthorized(null), false);
});

run('the unauthorized sentinel is recognized so no banner flashes', () => {
  installDom();

  assert.strictEqual(
    AjaxStatus.isUnauthorizedError(new Error(AjaxStatus.UNAUTHORIZED_ERROR)), true);
  assert.strictEqual(AjaxStatus.isUnauthorizedError(new Error('network down')), false);
  assert.strictEqual(AjaxStatus.isUnauthorizedError(null), false);
});

// --- readJsonOrThrow ---------------------------------------------------------
// The two checks every loader must make before touching a payload, written once
// instead of nine times. Both were separately forgotten in production, so this is
// now the function whose failure would break every page at once.

function response(status, body) {
  return {
    status: status,
    ok: status >= 200 && status < 300,
    json: () => Promise.resolve(body),
  };
}

run('a 2xx yields the parsed body', () => {
  installDomWithLocation('/charts', '');
  const payload = { resultsHtml: '<p>ok</p>' };

  const result = AjaxStatus.readJsonOrThrow(response(200, payload), 'charts');

  assert.ok(result && typeof result.then === 'function', 'returns resp.json()');
  return result.then((data) => assert.deepStrictEqual(data, payload));
});

run('a 401 navigates instead of handing back its body', () => {
  // The bug: a 401's body is VALID JSON, so .json() resolves and the payload key
  // is merely absent - innerHTML then wrote the string "undefined" over the page.
  installDomWithLocation('/', '?ajax=true');

  assert.throws(
    () => AjaxStatus.readJsonOrThrow(response(401, { error: 'Not logged in' }), 'dashboard'),
    (err) => AjaxStatus.isUnauthorizedError(err));
  assert.ok(global.window.location.href.startsWith('/login?next='));
});

run('a 500 throws a labelled error and does not navigate', () => {
  installDomWithLocation('/charts', '');

  assert.throws(() => AjaxStatus.readJsonOrThrow(response(500, null), 'charts'),
    /charts fetch failed: 500/);
  assert.strictEqual(global.window.location.href, '/charts');
});

run('a 404 throws too, so it cannot be mistaken for an empty answer', () => {
  installDomWithLocation('/song/t1', '');

  assert.throws(() => AjaxStatus.readJsonOrThrow(response(404, null), 'detail body'),
    /detail body fetch failed: 404/);
});

run('a server error is not reported as an unauthorized navigation', () => {
  // Otherwise every loader's catch would swallow real server errors silently.
  installDomWithLocation('/charts', '');

  try {
    AjaxStatus.readJsonOrThrow(response(503, null), 'charts');
    assert.fail('should have thrown');
  } catch (err) {
    assert.strictEqual(AjaxStatus.isUnauthorizedError(err), false);
  }
});

run('a missing label still produces a usable message', () => {
  installDomWithLocation('/charts', '');

  assert.throws(() => AjaxStatus.readJsonOrThrow(response(500, null)), /ajax fetch failed: 500/);
});

console.log('All ajax-status tests passed.');
