// Plain-node tests for the now-playing poll's response ordering in
// static/js/dashboard-page.js. Two in-flight polls can resolve out of order (a
// response only has to outlive the 15s interval), and the handlers used to
// apply whichever landed LAST: stale data over newer, a late failure counted
// against a healthy feed - and, worst, a slow success resolving after the 401
// branch had stopped both timers un-dimmed the card and cleared the Stale
// pill on a poller that will never poll again. Same sequence-number fix, for
// the same reason, as profile-page.js/admin-page.js (an AbortController
// REJECTS the superseded promise, which here would count toward the stale
// threshold).
//
// Unlike tests/test_dashboard_page.js (which requires the module with an
// empty DOM so the poll IIFE bails), this file pre-fills the DOM stub so the
// IIFE wires up, captures poll() through a VisibilityPoll stub, and drives
// fetch by hand. Run with: node tests/test_dashboard_poll.js
const assert = require('assert');

const MODULE_PATH = require.resolve('../static/js/dashboard-page.js');

function makeClassList() {
  const names = new Set();
  return {
    names,
    add(n) { names.add(n); },
    remove(n) { names.delete(n); },
    toggle(n, on) { if (on) names.add(n); else names.delete(n); },
    contains(n) { return names.has(n); },
  };
}

function makeEl(id) {
  return {
    id,
    textContent: '',
    style: {},
    classList: makeClassList(),
    setAttribute() {},
    getAttribute() { return null; },
    removeAttribute() {},
    appendChild() {},
  };
}

/* Load a FRESH copy of the module (its seq counter, stop flag and `playing`
 * anchor are module state) against a DOM stub carrying the now-playing card,
 * a VisibilityPoll stub that captures the callbacks instead of scheduling
 * them, and a fetch stub whose settlement the test controls. */
function freshPage() {
  const elements = {};
  for (const id of ['nowPlayingCard', 'nowPlayingPanel', 'nowPlayingName',
                    'nowPlayingArtists', 'nowPlayingState', 'nowPlayingCover',
                    'nowPlayingCoverLink']) {
    elements[id] = makeEl(id);
  }

  const handles = [];
  global.window = {
    VisibilityPoll: {
      start(fn, _intervalMs) {
        const handle = { fn, stopped: 0, stop() { this.stopped += 1; } };
        handles.push(handle);
        return handle;
      },
    },
  };

  const pendingFetches = [];
  global.fetch = () => new Promise((resolve, reject) => {
    pendingFetches.push({ resolve, reject });
  });

  global.document = {
    body: { addEventListener() {} },
    addEventListener() {},
    getElementById(id) { return elements[id] || null; },
    querySelector() { return null; },
    createElement(tag) { return Object.assign(makeEl(''), { tag }); },
    createTextNode(text) { return { text }; },
  };

  delete require.cache[MODULE_PATH];
  require(MODULE_PATH);

  assert.strictEqual(handles.length, 2, 'the poll and the progress tick must both be scheduled');
  return {
    elements,
    poll: handles[0].fn,          //< scheduled first (see the IIFE's last lines)
    pollHandle: handles[0],
    tickHandle: handles[1],
    pendingFetches,
  };
}

function okResponse(payload) {
  return { status: 200, ok: true, json: () => Promise.resolve(payload) };
}

function nowPlaying(name) {
  return { nowPlaying: { name, isPaused: false, positionMs: 0, durationMs: 1000 },
           friends: [], friendsMoreCount: 0 };
}

//< drains the whole microtask chain (.then -> json() -> .then), not just one hop
function settle() { return new Promise((resolve) => setImmediate(resolve)); }

const tests = [];
function run(name, fn) { tests.push([name, fn]); }

run('a superseded response does not overwrite the newer one', async () => {
  const page = freshPage();

  page.poll();   //< A - will resolve LAST despite being sent first
  page.poll();   //< B
  page.pendingFetches[1].resolve(okResponse(nowPlaying('Newer')));
  await settle();
  assert.strictEqual(page.elements.nowPlayingName.textContent, 'Newer');

  page.pendingFetches[0].resolve(okResponse(nowPlaying('Older')));
  await settle();

  assert.strictEqual(page.elements.nowPlayingName.textContent, 'Newer',
                     'the stale response must not win over the one that superseded it');
});

run('a slow success cannot un-freeze the card after the 401 stop', async () => {
  const page = freshPage();
  const card = page.elements.nowPlayingCard;

  page.poll();   //< A - still in flight when the session expires
  page.poll();   //< B - meets the expired session
  page.pendingFetches[1].resolve({ status: 401, ok: false, json: () => Promise.resolve({}) });
  await settle();
  assert.strictEqual(page.pollHandle.stopped, 1);
  assert.strictEqual(page.tickHandle.stopped, 1);
  assert.ok(card.classList.contains('is-stale'), 'the 401 freezes the card');
  assert.strictEqual(page.elements.nowPlayingState.textContent, 'Stale');

  page.pendingFetches[0].resolve(okResponse(nowPlaying('Late')));
  await settle();

  assert.ok(card.classList.contains('is-stale'),
            'nothing will ever poll again, so the late success must not present the card as live');
  assert.notStrictEqual(page.elements.nowPlayingName.textContent, 'Late',
                        'the late payload must not render either');
});

run('superseded failures do not count toward the stale threshold', async () => {
  const page = freshPage();
  const card = page.elements.nowPlayingCard;

  page.poll();   //< A, B, C - three requests the network will drop, slowly
  page.poll();
  page.poll();
  page.poll();   //< D - the live one, and it succeeds
  page.pendingFetches[3].resolve(okResponse(nowPlaying('Healthy')));
  await settle();
  assert.ok(!card.classList.contains('is-stale'));

  for (const i of [0, 1, 2]) page.pendingFetches[i].reject(new Error('dropped'));
  await settle();

  assert.ok(!card.classList.contains('is-stale'),
            'three STALE failures against a healthy feed must not mark it out of date');
});

(async () => {
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      console.error(`FAIL - ${name}`);
      console.error(err);
      process.exit(1);
    }
  }
  console.log('All dashboard-poll tests done.');
})();
