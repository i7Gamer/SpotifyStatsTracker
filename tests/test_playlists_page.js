// Plain-node unit test for the playlist builder (static/js/playlists.js).
// Run with: node tests/test_playlists_page.js
//
// The behaviour worth the most here is the preview TOKEN. Every tag toggle
// fires a preview request, and the file's own comment records why the guard
// exists: two quick toggles could resolve out of order and leave the count -
// and the Download button's enabled state - describing a selection the user has
// already moved past, "including re-enabling Download after the selection was
// cleared to nothing". A stale response re-enabling Download is a real export
// of the wrong tracks, so the test below resolves the two responses backwards
// on purpose rather than trusting them to arrive in order.
//
// The destructive halves are covered too: delete-tag is guarded by confirm()
// and removes a tag from every item, and both mutations must SAY SO when the
// server refuses - they used to no-op silently, leaving the old name on screen.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'playlists.js');
// Captured before any test replaces global.console. The stub below swallows
// console.error so a test can assert on it - and would otherwise swallow this
// runner's own failure report too, turning a red test into a silent exit 1.
const realConsole = console;

function makeChip(tag) {
  const handlers = {};
  return {
    dataset: { tag }, style: {}, handlers,
    addEventListener(type, fn) { handlers[type] = fn; },
    click() { handlers.click(); },
  };
}

function makeControl(value) {
  const handlers = {};
  return {
    value: value === undefined ? '' : value, textContent: '', disabled: false, handlers,
    addEventListener(type, fn) { handlers[type] = fn; },
  };
}

// A promise plus the handles to settle it later - what lets a test resolve two
// in-flight previews in the wrong order.
function deferred() {
  const box = {};
  box.promise = new Promise((resolve, reject) => { box.resolve = resolve; box.reject = reject; });
  return box;
}

function loadPlaylists(options) {
  options = options || {};
  const calls = { fetched: [], prompts: [], confirms: [], alerts: [], errors: [], reloads: 0,
                  loginRedirects: 0 };
  const elements = options.elements || {};
  const selectors = Object.assign(
    { '.playlist-tag-chip': [], '.btn-rename-tag': [], '.btn-delete-tag': [] },
    options.selectors || {});

  let ready = null;
  global.window = {
    location: { href: '', reload() { calls.reloads += 1; } },
    //< the same stub shape tests/test_wrapped_page.js uses: the real helper's
    //  navigation is pinned in tests/test_ajax_status.js, so what these tests
    //  have to prove is that this page DELEGATES a 401 to it rather than
    //  reading the body as if it described the user's tags
    AjaxStatus: options.noAjaxStatus ? undefined : {
      redirectIfUnauthorized(response) {
        if (!response || response.status !== 401) return false;
        calls.loginRedirects += 1;
        return true;
      },
    },
  };
  global.document = {
    addEventListener(type, fn) { if (type === 'DOMContentLoaded') ready = fn; },
    getElementById(id) { return elements[id] || null; },
    querySelectorAll(selector) { return selectors[selector] || []; },
    querySelector(selector) {
      return selector === 'input[name="csrf_token"]'
        ? (options.csrf === null ? null : { value: options.csrf || 'tok-123' })
        : null;
    },
  };
  global.fetch = function (url, init) {
    calls.fetched.push({ url, init });
    return options.respond ? options.respond(url, calls.fetched.length) : Promise.reject(new Error('no stub'));
  };
  global.prompt = function (message, initial) { calls.prompts.push(message); return options.promptAnswer !== undefined ? options.promptAnswer : initial; };
  global.confirm = function (message) { calls.confirms.push(message); return !!options.confirmAnswer; };
  global.alert = function (message) { calls.alerts.push(message); };
  global.console = { error(...args) { calls.errors.push(args); }, log: realConsole.log };

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  ready();                //< the whole file is inside a DOMContentLoaded handler
  return calls;
}

function jsonOnce(body) {
  return () => Promise.resolve({ json: () => Promise.resolve(body) });
}

// What every endpoint on this page answers once the session has expired:
// @requiresUser(api=True) sends 401 with a JSON body. The body is the trap -
// it PARSES, so a handler that goes straight to .json() reads a real payload
// whose keys are all absent.
function unauthorizedOnce() {
  return () => Promise.resolve({
    status: 401, ok: false,
    json: () => Promise.resolve({ error: 'Not logged in' }),
  });
}

const tick = () => new Promise(resolve => setImmediate(resolve));

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// ------------------------------------------------------------ tag selection

function previewSetup(options) {
  const chipA = makeChip('jazz');
  const chipB = makeChip('lofi');
  const previewCount = makeControl();
  const btnDownload = makeControl();
  const matchMode = makeControl('any');
  const sortBy = makeControl('plays');
  const exportFormat = makeControl('m3u');
  const calls = loadPlaylists(Object.assign({
    selectors: { '.playlist-tag-chip': [chipA, chipB] },
    //< the id really is btnDownloadPlaylist; keying this map on the local name
    //  instead left the script with a null button, and every assertion about
    //  `disabled` then passed against the stub's own untouched initial value
    elements: { previewCount, btnDownloadPlaylist: btnDownload, matchMode, sortBy, exportFormat },
  }, options || {}));
  return { calls, chipA, chipB, previewCount, btnDownload, matchMode, sortBy, exportFormat };
}

run('selecting a tag asks how many tracks match', async () => {
  const dom = previewSetup({ respond: jsonOnce({ track_count: 12 }) });

  dom.chipA.click();
  await tick();

  assert.strictEqual(dom.calls.fetched.length, 1);
  assert.ok(dom.calls.fetched[0].url.includes('tags=jazz'), dom.calls.fetched[0].url);
  assert.ok(dom.calls.fetched[0].url.includes('match=any'));
  assert.strictEqual(dom.previewCount.textContent, '12 tracks match selection');
  assert.strictEqual(dom.btnDownload.disabled, false);
});

run('one match is reported in the singular', async () => {
  const dom = previewSetup({ respond: jsonOnce({ track_count: 1 }) });

  dom.chipA.click();
  await tick();

  assert.strictEqual(dom.previewCount.textContent, '1 track match selection');
});

run('a selection matching nothing leaves Download disabled', async () => {
  const dom = previewSetup({ respond: jsonOnce({ track_count: 0 }) });

  dom.chipA.click();
  await tick();

  assert.strictEqual(dom.btnDownload.disabled, true);
});

run('deselecting the last tag asks the server nothing at all', async () => {
  const dom = previewSetup({ respond: jsonOnce({ track_count: 12 }) });
  dom.chipA.click();
  await tick();
  const asked = dom.calls.fetched.length;

  dom.chipA.click();          //< toggles it back off
  await tick();

  assert.strictEqual(dom.calls.fetched.length, asked, 'an empty selection is answered locally');
  assert.strictEqual(dom.previewCount.textContent, '0 tracks match selection');
  assert.strictEqual(dom.btnDownload.disabled, true);
});

run('both selected tags travel together', async () => {
  const dom = previewSetup({ respond: jsonOnce({ track_count: 30 }) });

  dom.chipA.click();
  dom.chipB.click();
  await tick();

  const last = dom.calls.fetched[dom.calls.fetched.length - 1].url;
  assert.ok(last.includes(encodeURIComponent('jazz,lofi')), last);
});

// ------------------------------------------------- the out-of-order guard

run('a stale preview cannot overwrite a newer one', async () => {
  const first = deferred();
  const second = deferred();
  const dom = previewSetup({ respond: (url, n) => (n === 1 ? first.promise : second.promise) });

  dom.chipA.click();                                   //< request 1
  dom.chipB.click();                                   //< request 2, supersedes it
  second.resolve({ json: () => Promise.resolve({ track_count: 30 }) });
  await tick();
  first.resolve({ json: () => Promise.resolve({ track_count: 12 }) });
  await tick();

  assert.strictEqual(dom.previewCount.textContent, '30 tracks match selection',
                     'the older response arrived last and must be discarded');
});

run('a stale preview cannot re-enable Download after the selection was cleared', async () => {
  const first = deferred();
  const dom = previewSetup({ respond: () => first.promise });

  dom.chipA.click();                                   //< request 1: will say 12 tracks
  dom.chipA.click();                                   //< cleared to nothing; Download off
  assert.strictEqual(dom.btnDownload.disabled, true);
  first.resolve({ json: () => Promise.resolve({ track_count: 12 }) });
  await tick();

  assert.strictEqual(dom.btnDownload.disabled, true,
                     'exporting here would download tracks for a selection nobody has');
});

run('a failed preview says the count is unknown rather than lying', async () => {
  const dom = previewSetup({ respond: () => Promise.reject(new Error('offline')) });

  dom.chipA.click();
  await tick();

  assert.strictEqual(dom.previewCount.textContent, "Couldn't check how many tracks match.");
  assert.strictEqual(dom.calls.errors.length, 1);
});

run('an expired session sends the user to log in rather than claiming zero matches', async () => {
  const dom = previewSetup({ respond: unauthorizedOnce() });

  dom.chipA.click();
  await tick(); await tick();

  assert.strictEqual(dom.calls.loginRedirects, 1);
  //< the whole point: `data.track_count || 0` on a 401 body reads 0, and the
  //  line below is then a statement about the user's library that is false.
  //  Nothing may be written over the count while the browser is leaving.
  assert.strictEqual(dom.previewCount.textContent, '');
  assert.notStrictEqual(dom.previewCount.textContent, '0 tracks match selection');
});

// ---------------------------------------------------------------- download

run('Download builds an export URL carrying every control', async () => {
  const dom = previewSetup({ respond: jsonOnce({ track_count: 5 }) });
  dom.chipA.click();
  await tick();

  dom.btnDownload.handlers.click();

  const url = new URL(global.window.location.href, 'http://localhost');
  assert.strictEqual(url.pathname, '/playlist/export');
  assert.strictEqual(url.searchParams.get('tags'), 'jazz');
  assert.strictEqual(url.searchParams.get('match'), 'any');
  assert.strictEqual(url.searchParams.get('sort'), 'plays');
  assert.strictEqual(url.searchParams.get('format'), 'm3u');
});

run('Download with nothing selected navigates nowhere', () => {
  const dom = previewSetup({});

  dom.btnDownload.handlers.click();

  assert.strictEqual(global.window.location.href, '');
});

// ------------------------------------------------------------ rename a tag

function renameSetup(options) {
  const btn = makeChip('jazz');
  const calls = loadPlaylists(Object.assign({ selectors: { '.btn-rename-tag': [btn] } }, options || {}));
  return { calls, btn };
}

run('renaming sends the new name with the CSRF token', async () => {
  const { calls, btn } = renameSetup({ promptAnswer: 'jazzy', respond: jsonOnce({ success: true }) });

  btn.click();
  await tick(); await tick();

  assert.strictEqual(calls.fetched[0].url, '/api/tags/rename');
  assert.strictEqual(calls.fetched[0].init.headers['X-CSRFToken'], 'tok-123');
  assert.deepStrictEqual(JSON.parse(calls.fetched[0].init.body), { old_tag: 'jazz', new_tag: 'jazzy' });
  assert.strictEqual(calls.reloads, 1);
});

run('a cancelled rename prompt sends nothing', () => {
  const { calls, btn } = renameSetup({ promptAnswer: null });

  btn.click();

  assert.deepStrictEqual(calls.fetched, []);
});

run('renaming a tag to itself sends nothing', () => {
  const { calls, btn } = renameSetup({ promptAnswer: 'jazz' });

  btn.click();

  assert.deepStrictEqual(calls.fetched, []);
});

run('a whitespace-only name sends nothing', () => {
  const { calls, btn } = renameSetup({ promptAnswer: '   ' });

  btn.click();

  assert.deepStrictEqual(calls.fetched, []);
});

run('the new name is trimmed before it is sent', async () => {
  const { calls, btn } = renameSetup({ promptAnswer: '  jazzy  ', respond: jsonOnce({ success: true }) });

  btn.click();
  await tick(); await tick();

  assert.strictEqual(JSON.parse(calls.fetched[0].init.body).new_tag, 'jazzy');
});

run('a refused rename says so instead of no-opping silently', async () => {
  const { calls, btn } = renameSetup({
    promptAnswer: 'jazzy', respond: jsonOnce({ success: false, error: 'That tag already exists.' }),
  });

  btn.click();
  await tick(); await tick();

  assert.deepStrictEqual(calls.alerts, ['That tag already exists.']);
  assert.strictEqual(calls.reloads, 0, 'nothing changed, so nothing to reload');
});

run('an expired session sends the user to log in rather than alerting the 401 body', async () => {
  const { calls, btn } = renameSetup({ promptAnswer: 'jazzy', respond: unauthorizedOnce() });

  btn.click();
  await tick(); await tick();

  assert.strictEqual(calls.loginRedirects, 1);
  //< "Not logged in" in an alert() is the route's error field read as if it
  //  were a rejected rename: it leaves the user on a dead page with no way
  //  back. tags.js redirects for the same endpoint; so does this.
  assert.deepStrictEqual(calls.alerts, []);
  assert.strictEqual(calls.reloads, 0);
});

run('a rename that never reaches the server still tells the user', async () => {
  const { calls, btn } = renameSetup({ promptAnswer: 'jazzy', respond: () => Promise.reject(new Error('offline')) });

  btn.click();
  await tick(); await tick();

  assert.deepStrictEqual(calls.alerts, ["Couldn't rename that tag. Please try again."]);
});

// ------------------------------------------------------------ delete a tag

function deleteSetup(options) {
  const btn = makeChip('lo fi');   //< a space, so the URL encoding is exercised
  const calls = loadPlaylists(Object.assign({ selectors: { '.btn-delete-tag': [btn] } }, options || {}));
  return { calls, btn };
}

run('deleting asks first, and says what it removes the tag from', () => {
  const { calls, btn } = deleteSetup({ confirmAnswer: false });

  btn.click();

  assert.strictEqual(calls.confirms.length, 1);
  assert.ok(calls.confirms[0].includes('all items'), calls.confirms[0]);
  assert.deepStrictEqual(calls.fetched, [], 'declining must not delete anything');
});

run('a confirmed delete encodes the tag into the path', async () => {
  const { calls, btn } = deleteSetup({ confirmAnswer: true, respond: jsonOnce({ success: true }) });

  btn.click();
  await tick(); await tick();

  assert.strictEqual(calls.fetched[0].url, '/api/tags/lo%20fi');
  assert.strictEqual(calls.fetched[0].init.method, 'DELETE');
  assert.strictEqual(calls.fetched[0].init.headers['X-CSRFToken'], 'tok-123');
  assert.strictEqual(calls.reloads, 1);
});

run('a refused delete says so instead of no-opping silently', async () => {
  const { calls, btn } = deleteSetup({
    confirmAnswer: true, respond: jsonOnce({ success: false, error: 'Session expired.' }),
  });

  btn.click();
  await tick(); await tick();

  assert.deepStrictEqual(calls.alerts, ['Session expired.']);
  assert.strictEqual(calls.reloads, 0);
});

run('an expired session sends the user to log in rather than alerting a failed delete', async () => {
  const { calls, btn } = deleteSetup({ confirmAnswer: true, respond: unauthorizedOnce() });

  btn.click();
  await tick(); await tick();

  assert.strictEqual(calls.loginRedirects, 1);
  assert.deepStrictEqual(calls.alerts, []);
  assert.strictEqual(calls.reloads, 0);
});

run('a page with no CSRF input sends an empty token rather than crashing', async () => {
  const btn = makeChip('jazz');
  const calls = loadPlaylists({
    csrf: null, confirmAnswer: true,
    selectors: { '.btn-delete-tag': [btn] }, respond: jsonOnce({ success: true }),
  });

  btn.click();
  await tick(); await tick();

  assert.strictEqual(calls.fetched[0].init.headers['X-CSRFToken'], '');
});

(async () => {
  for (const { name, fn } of results) {
    try {
      await fn();
      realConsole.log(`ok - ${name}`);
    } catch (err) {
      realConsole.error(`FAIL - ${name}`);
      realConsole.error(err);
      process.exit(1);
    }
  }
  realConsole.log(`all ${results.length} playlists tests passed`);
})();
