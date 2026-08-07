// Plain-node unit tests for static/js/visibility-poll.js - the poll lifecycle
// both of this app's background polls share.
// Run with: node tests/test_visibility_poll.js
//
// What makes this worth its own file: the two things it gets wrong are both
// invisible in a browser until a bill or a bug arrives. Failing to STOP means a
// tab left open overnight keeps asking (browsers throttle background timers,
// they don't stop them). Failing to stay stopped after a caller's stop() means
// the 401 branches - the ones that exist so an expired session doesn't hammer
// the server every 10s forever - come back to life at the next tab switch.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'visibility-poll.js');

// Loads the module fresh against a new stub. Fresh because the module cache
// would otherwise replay the first load's side effects onto the old globals.
function load(options) {
  options = options || {};
  const calls = { intervals: [], cleared: [], listeners: {}, ran: 0 };
  global.window = {};
  global.document = {
    hidden: !!options.hidden,
    addEventListener(type, fn) { calls.listeners[type] = fn; },
  };
  global.setInterval = function (fn, ms) { calls.intervals.push({ fn, ms }); return calls.intervals.length; };
  global.clearInterval = function (id) { calls.cleared.push(id); };

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);

  calls.start = function (intervalMs) {
    return global.window.VisibilityPoll.start(function () { calls.ran += 1; }, intervalMs || 1000);
  };
  //< what the browser does on a tab switch: flip the flag, then fire the event
  calls.setHidden = function (hidden) {
    global.document.hidden = hidden;
    calls.listeners.visibilitychange();
  };
  return calls;
}

function run(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { console.error(`FAIL - ${name}`); throw err; }
}

run('a visible tab polls at once and then on the interval', () => {
  const poll = load();

  poll.start(15000);

  assert.strictEqual(poll.ran, 1, 'the first request goes out immediately, not one interval later');
  assert.strictEqual(poll.intervals.length, 1);
  assert.strictEqual(poll.intervals[0].ms, 15000);
});

run('a page that loads in a background tab does neither', () => {
  // Opening a link in a new tab: nobody is looking at it yet, so it costs
  // nothing until they switch to it.
  const poll = load({ hidden: true });

  poll.start();

  assert.strictEqual(poll.ran, 0);
  assert.strictEqual(poll.intervals.length, 0);
});

run('hiding the tab stops the timer', () => {
  const poll = load();
  poll.start();

  poll.setHidden(true);

  assert.strictEqual(poll.cleared.length, 1);
});

run('coming back polls immediately rather than waiting out the interval', () => {
  // The other half of the saving: without this, a returning tab shows whatever
  // was playing when it was hidden for up to a full interval.
  const poll = load();
  poll.start();
  poll.setHidden(true);

  poll.setHidden(false);

  assert.strictEqual(poll.ran, 2);
  assert.strictEqual(poll.intervals.length, 2, 'and the interval is armed again');
});

run('a visibility event that changes nothing does not stack a second timer', () => {
  const poll = load();
  poll.start();

  poll.setHidden(false);
  poll.setHidden(false);

  assert.strictEqual(poll.intervals.length, 1, 'two timers means two requests per interval, forever');
  assert.strictEqual(poll.ran, 1);
});

run('stopping is permanent - a tab switch does not resurrect it', () => {
  // This is what the 401 branches call. Restarting one would put the app back
  // to hitting the server on an expired session for the life of the tab.
  const poll = load();
  const handle = poll.start();

  handle.stop();
  poll.setHidden(true);
  poll.setHidden(false);

  assert.strictEqual(poll.ran, 1, 'no request after stop()');
  assert.strictEqual(poll.intervals.length, 1, 'and no new timer');
});

run('stopping a poll that is already hidden still stops it for good', () => {
  const poll = load();
  const handle = poll.start();
  poll.setHidden(true);

  handle.stop();
  poll.setHidden(false);

  assert.strictEqual(poll.ran, 1);
});

run('the callback is what the interval runs, not a wrapper that drops it', () => {
  const poll = load();
  poll.start();

  poll.intervals[0].fn();

  assert.strictEqual(poll.ran, 2);
});

run('two polls on one page keep their own timers', () => {
  // The dashboard runs three (now-playing, the progress tick, the status pill).
  const poll = load();
  const first = poll.start(1000);
  poll.start(2000);

  first.stop();

  assert.strictEqual(poll.intervals.length, 2);
  assert.strictEqual(poll.cleared.length, 1, 'stopping one must not stop the other');
});

console.log('all visibility-poll tests passed');
