// Plain-node unit test for the detail pages' "Refresh Last.fm Data" form
// (static/js/admin-refresh.js). Run with: node tests/test_admin_refresh.js
//
// The whole point of this file is that the refresh does NOT navigate: the route
// answers an XHR post with {kind, message} JSON so the page keeps its tab, sort
// and page state. That contract has two halves, and only the server half was
// tested - the request has to actually carry X-Requested-With, or the route
// takes its redirect branch (see adminRefreshLastfmEntity) and the reply this
// code tries to parse as JSON is an HTML page.
//
// The button re-enable is the other thing worth pinning: it lives in a
// `finally`, so a failed refresh has to leave the button usable. Without that
// one bad response makes the control dead until a reload.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'admin-refresh.js');
const FORM_SELECTOR = 'form[action*="/admin/lastfm/refresh/"]';

function makeFlash() {
  return { innerHTML: null, children: [], appendChild(node) { this.children.push(node); } };
}

function makeForm(button) {
  const handlers = {};
  return {
    handlers,
    action: '/admin/lastfm/refresh/artist/art1',
    addEventListener(type, fn) { handlers[type] = fn; },
    querySelector(selector) { return selector === 'button[type="submit"]' ? button : null; },
  };
}

function loadAdminRefresh(options) {
  options = options || {};
  const calls = { requests: [], created: [] };

  global.FormData = function FormDataStub(form) { this.form = form; };
  global.document = {
    querySelector(selector) { return selector === FORM_SELECTOR ? (options.form || null) : null; },
    getElementById(id) { return id === 'detail-flash' ? (options.flash || null) : null; },
    createElement(tag) {
      const node = { tag, className: '', style: {}, textContent: '' };
      calls.created.push(node);
      return node;
    },
  };
  global.fetch = function (url, init) {
    calls.requests.push({ url, init });
    return options.respond ? options.respond() : Promise.reject(new Error('no stub'));
  };

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  return calls;
}

function jsonResponse(body) {
  return () => Promise.resolve({ json: () => Promise.resolve(body) });
}

async function submitScenario(options) {
  const button = { disabled: false, history: [] };
  const form = makeForm(button);
  const flash = makeFlash();
  const calls = loadAdminRefresh({ form, flash, respond: options.respond });
  const evt = { prevented: 0, preventDefault() { this.prevented += 1; } };

  form.handlers.submit(evt);
  const disabledDuringRequest = button.disabled;
  //< two ticks: the fetch chain is .then().then().catch().finally()
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));

  return { evt, calls, button, flash, disabledDuringRequest };
}

const results = [];
function run(name, fn) { results.push({ name, fn }); }

run('a page with no refresh form (any non-admin) wires up nothing', () => {
  const calls = loadAdminRefresh({ form: null });

  assert.deepStrictEqual(calls.requests, []);
});

run('submitting posts to the form action instead of navigating', async () => {
  const { evt, calls } = await submitScenario({ respond: jsonResponse({ kind: 'success', message: 'Refreshed' }) });

  assert.strictEqual(evt.prevented, 1, 'the browser must not submit this form normally');
  assert.strictEqual(calls.requests.length, 1);
  assert.strictEqual(calls.requests[0].url, '/admin/lastfm/refresh/artist/art1');
  assert.strictEqual(calls.requests[0].init.method, 'POST');
});

run('the request announces itself as XHR, or the route answers with a redirect', async () => {
  const { calls } = await submitScenario({ respond: jsonResponse({ kind: 'success', message: 'ok' }) });

  assert.strictEqual(calls.requests[0].init.headers['X-Requested-With'], 'XMLHttpRequest');
});

run('the button is disabled while the refresh is in flight', async () => {
  const { disabledDuringRequest } = await submitScenario({ respond: jsonResponse({ kind: 'success', message: 'ok' }) });

  assert.strictEqual(disabledDuringRequest, true);
});

run('the button comes back after a successful refresh', async () => {
  const { button } = await submitScenario({ respond: jsonResponse({ kind: 'success', message: 'ok' }) });

  assert.strictEqual(button.disabled, false);
});

run('the button comes back after a FAILED refresh too', async () => {
  const { button } = await submitScenario({ respond: () => Promise.reject(new Error('offline')) });

  assert.strictEqual(button.disabled, false, 'one bad response must not kill the control until reload');
});

run('a success message is rendered as text, in the success colour', async () => {
  const { flash } = await submitScenario({ respond: jsonResponse({ kind: 'success', message: 'Refreshed Nujabes' }) });

  assert.strictEqual(flash.children.length, 1);
  assert.strictEqual(flash.children[0].textContent, 'Refreshed Nujabes');
  assert.strictEqual(flash.children[0].className, 'success');
  assert.strictEqual(flash.children[0].style.color, '#4aff4a');
});

run('an error message gets the error colour', async () => {
  const { flash } = await submitScenario({ respond: jsonResponse({ kind: 'error', message: 'No API key' }) });

  assert.strictEqual(flash.children[0].style.color, '#ff4a4a');
});

run('a kind nobody recognises still shows, in the error colour', async () => {
  const { flash } = await submitScenario({ respond: jsonResponse({ kind: 'weird', message: 'Hmm' }) });

  assert.strictEqual(flash.children[0].textContent, 'Hmm');
  assert.strictEqual(flash.children[0].style.color, '#ff4a4a', 'an unknown kind must not be styled as success');
});

run('a network failure says so rather than failing silently', async () => {
  const { flash } = await submitScenario({ respond: () => Promise.reject(new Error('offline')) });

  assert.strictEqual(flash.children[0].textContent, 'Refresh failed - try again.');
});

run('the previous message is cleared before the new one lands', async () => {
  const { flash } = await submitScenario({ respond: jsonResponse({ kind: 'success', message: 'ok' }) });

  assert.strictEqual(flash.innerHTML, '', 'two refreshes must not stack two messages');
});

run('a refresh with nowhere to report to does not throw', async () => {
  const button = { disabled: false };
  const form = makeForm(button);
  loadAdminRefresh({ form, flash: null, respond: jsonResponse({ kind: 'success', message: 'ok' }) });

  form.handlers.submit({ preventDefault() {} });
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(button.disabled, false, 'the finally still ran');
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
  console.log(`all ${results.length} admin-refresh tests passed`);
})();
