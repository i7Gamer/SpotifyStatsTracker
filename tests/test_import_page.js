// Plain-node unit test for the import page (static/js/import-page.js).
// Run with: node tests/test_import_page.js
//
// The confirm() here stands in front of "deletes ALL recorded plays in the
// years these files cover - including live-tracked and manually added ones".
// That is the single most destructive action in the app, its guard is three
// lines of browser JavaScript, and nothing executed them. This file's own
// header says the lint gate was added because a ReferenceError in this class of
// code shipped once; no-undef would catch that spelling, but not a guard that
// asks and then submits anyway.
//
// setTimeout is stubbed rather than left to node: the poller re-arms itself, so
// a real timer would keep the process alive and hang the suite.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'import-page.js');
// Captured before any test replaces global.console. The stub below swallows
// console.error so a test can assert on it - and would otherwise swallow this
// runner's own failure report too, turning a red test into a silent exit 1.
const realConsole = console;

function makeElement(extra) {
  return Object.assign({ value: null, textContent: '', checked: false, files: [] }, extra || {});
}

function makeForm() {
  const handlers = {};
  return { handlers, addEventListener(type, fn) { handlers[type] = fn; } };
}

function loadImportPage(options) {
  options = options || {};
  const calls = { timeouts: [], fetched: [], errors: [], confirms: [] };
  const elements = options.elements || {};

  global.window = {
    IMPORT_PROGRESS_URL: '/import-progress',
    IMPORT_IS_RUNNING: !!options.running,
  };
  global.document = { getElementById(id) { return elements[id] || null; } };
  global.setTimeout = function (fn, ms) { calls.timeouts.push({ fn, ms }); return calls.timeouts.length; };
  global.confirm = function (message) { calls.confirms.push(message); return !!options.confirmAnswer; };
  global.console = { error(...args) { calls.errors.push(args); }, log: realConsole.log };
  global.fetch = function (url) {
    calls.fetched.push(url);
    const responder = options.respond;
    return responder ? responder() : Promise.reject(new Error('no stub'));
  };

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  return calls;
}

function progressResponse(body, ok, status) {
  const failed = ok === false;
  return () => Promise.resolve({
    ok: !failed,
    //< the poller tells an expired session (401 - the only non-2xx
    //  /import-progress deliberately answers, see routes/system.py) apart from
    //  a transient failure, so the stub has to carry a status
    status: status === undefined ? (failed ? 500 : 200) : status,
    json: () => Promise.resolve(body),
  });
}

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// ------------------------------------------------- the destructive-overwrite guard

function submitScenario(options) {
  const form = makeForm();
  const overwrite = makeElement({ checked: options.overwriteTicked });
  const bar = makeElement();
  const message = makeElement();
  const files = makeElement({ files: new Array(options.fileCount === undefined ? 1 : options.fileCount) });
  const calls = loadImportPage({
    confirmAnswer: options.confirmAnswer,
    elements: {
      'import-form': form, 'overwrite-range': overwrite,
      'progress-bar': bar, 'progress-message': message, history_file: files,
    },
  });
  const evt = { prevented: 0, preventDefault() { this.prevented += 1; } };
  form.handlers.submit(evt);
  return { evt, calls, bar, message };
}

run('declining the overwrite warning stops the submit', () => {
  const { evt, calls } = submitScenario({ overwriteTicked: true, confirmAnswer: false });

  assert.strictEqual(evt.prevented, 1);
  assert.strictEqual(calls.confirms.length, 1);
  assert.ok(calls.confirms[0].includes('cannot be undone'), calls.confirms[0]);
});

run('a declined overwrite does not pretend an import started', () => {
  const { message, bar } = submitScenario({ overwriteTicked: true, confirmAnswer: false });

  assert.strictEqual(message.textContent, '', 'no "Starting import..." for an import that was cancelled');
  assert.strictEqual(bar.value, null, 'and the bar is not reset either');
});

run('accepting the overwrite warning lets the submit through', () => {
  const { evt, calls } = submitScenario({ overwriteTicked: true, confirmAnswer: true });

  assert.strictEqual(evt.prevented, 0);
  assert.strictEqual(calls.confirms.length, 1);
});

run('an append import is never interrupted by the warning', () => {
  const { evt, calls } = submitScenario({ overwriteTicked: false, confirmAnswer: false });

  assert.deepStrictEqual(calls.confirms, [], 'nothing is deleted, so nothing to warn about');
  assert.strictEqual(evt.prevented, 0);
});

run('a started import resets the bar and counts the files', () => {
  const { message, bar } = submitScenario({ overwriteTicked: false, fileCount: 4 });

  assert.strictEqual(message.textContent, 'Starting import of 4 files...');
  assert.strictEqual(bar.value, 0);
});

run('a single file says so without the count', () => {
  const { message } = submitScenario({ overwriteTicked: false, fileCount: 1 });

  assert.strictEqual(message.textContent, 'Starting import...');
});

// ------------------------------------------------------------- the poller

function pollScenario(body, ok, status) {
  const bar = makeElement();
  const message = makeElement();
  const calls = loadImportPage({
    running: true,
    respond: progressResponse(body, ok, status),
    elements: { 'progress-bar': bar, 'progress-message': message },
  });
  return { calls, bar, message };
}

// setTimeout is stubbed into a list rather than run, so a retry only happens if
// this drives it. Bounded by `guard` so a poller that never gives up fails the
// test instead of hanging the suite.
async function drainRetries(calls, guard) {
  let fired = 0;
  while (calls.timeouts.length && fired < (guard || 10)) {
    calls.timeouts.shift().fn();
    fired += 1;
    await new Promise(resolve => setImmediate(resolve));
  }
  return fired;
}

run('a page with no import running asks for no progress at all', () => {
  const calls = loadImportPage({ running: false, elements: {} });

  assert.deepStrictEqual(calls.fetched, []);
});

run('a running import shows its percentage and polls again quickly', async () => {
  const { calls, bar, message } = pollScenario({ status: 'running', percentage: 42, message: 'Parsing' });
  await new Promise(resolve => setImmediate(resolve));

  assert.deepStrictEqual(calls.fetched, ['/import-progress']);
  assert.strictEqual(bar.value, 42);
  assert.ok(message.textContent.includes('42%'), message.textContent);
  assert.deepStrictEqual(calls.timeouts.map(t => t.ms), [1200]);
});

run('a finished import stops polling', async () => {
  const { calls, message } = pollScenario({ status: 'complete', percentage: 100, message: 'Done' });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(message.textContent, 'Import complete: Done');
  assert.deepStrictEqual(calls.timeouts, [], 'a completed import must not keep hitting the server');
});

run('a failed import stops polling and says it failed', async () => {
  const { calls, message } = pollScenario({ status: 'failed', percentage: 12, message: 'Bad file' });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(message.textContent, 'Import failed: Bad file');
  assert.deepStrictEqual(calls.timeouts, []);
});

run('a queued import backs off to the slower poll', async () => {
  const { calls } = pollScenario({ status: 'queued', percentage: 0, message: '' });
  await new Promise(resolve => setImmediate(resolve));

  assert.deepStrictEqual(calls.timeouts.map(t => t.ms), [5000],
                         'it may not have started yet, so this must not spin at the running cadence');
});

run('a progress request that errors is logged, not left to reject unhandled', async () => {
  const bar = makeElement();
  const calls = loadImportPage({
    running: true,
    respond: () => Promise.reject(new Error('offline')),
    elements: { 'progress-bar': bar, 'progress-message': makeElement() },
  });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(calls.errors.length, 1);
  //< it used to assert no retry here. A dropped connection mid-import froze the
  //  bar for the rest of the page's life, and the import it was reporting on
  //  carried on regardless - the poll has to come back.
  assert.deepStrictEqual(calls.timeouts.map(t => t.ms), [5000]);
});

run('a non-OK progress response is dropped rather than rendered as zero', async () => {
  const { calls, bar } = pollScenario({ status: 'running', percentage: 99 }, false);
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(bar.value, null, 'a 500 must not repaint the bar');
  //< same deliberate change as above: dropping the BODY is right, dropping the
  //  poll with it is not - the import is still running
  assert.deepStrictEqual(calls.timeouts.map(t => t.ms), [5000]);
});

run('an expired session ends the poll instead of retrying', async () => {
  const { calls } = pollScenario({}, false, 401);
  await new Promise(resolve => setImmediate(resolve));

  assert.deepStrictEqual(calls.timeouts, [],
    'the session is gone, so every retry is a guaranteed 401 - stop, as the other polls do');
});

run('a failure that never clears gives up rather than polling forever', async () => {
  const { calls, message } = pollScenario({}, false, 500);
  await new Promise(resolve => setImmediate(resolve));
  await drainRetries(calls);

  assert.deepStrictEqual(calls.timeouts, [], 'a dead endpoint must not be polled for the life of the tab');
  assert.ok(message.textContent.includes('Lost contact'),
    'and the user is told, rather than left reading a frozen bar: ' + message.textContent);
});

run('a recovered poll forgets the earlier failures', async () => {
  const bar = makeElement();
  const message = makeElement();
  // Three failures, a success, three more: the cap counts CONSECUTIVE failures,
  // so a flaky connection over a long import must not accumulate toward it.
  // Against a cumulative counter this gives up on the sixth attempt.
  const outcomes = [false, false, false, true, false, false, false];
  let attempt = 0;
  const calls = loadImportPage({
    running: true,
    respond: () => {
      const ok = outcomes[attempt] === undefined ? true : outcomes[attempt];
      attempt += 1;
      return ok
        ? Promise.resolve({ ok: true, status: 200,
                            json: () => Promise.resolve({ status: 'running', percentage: 7, message: 'Parsing' }) })
        : Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) });
    },
    elements: { 'progress-bar': bar, 'progress-message': message },
  });
  await new Promise(resolve => setImmediate(resolve));
  await drainRetries(calls, 12);

  assert.strictEqual(bar.value, 7, 'the poll recovered and kept reporting');
  assert.ok(!message.textContent.includes('Lost contact'),
    'it must not give up on an import it is still successfully following: ' + message.textContent);
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
  realConsole.log(`all ${results.length} import-page tests passed`);
})();
