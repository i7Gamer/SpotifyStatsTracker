// Plain-node unit tests for the two pure decisions in static/js/dashboard-page.js.
// Uses a tiny hand-rolled DOM stub (no jsdom) - just enough for the module's
// top-level statements to run. Run with: node tests/test_dashboard_page.js
//
// Both of these were previously buried in page IIFEs and asserted, if at all,
// by grepping the source. That is how the calendar tooltip ended up with a test
// that survived deleting the listening time from it: the assertion looked for
// `getAttribute('data-time')`, which is still there when the value is read and
// then dropped on the floor.
const assert = require('assert');

global.window = {};
global.document = {
  body: { addEventListener() {} },
  addEventListener() {},
  getElementById() { return null; },     //< no now-playing card, no friends strip
  querySelector() { return null; },      //< no streak calendar
  createElement() { return { classList: { add() {} }, appendChild() {} }; },
};

const { calendarTooltipLabel, friendsStripSignature } = require('../static/js/dashboard-page.js');

function run(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { console.error(`FAIL - ${name}`); throw err; }
}

// --- the streak calendar's tooltip -------------------------------------------

run('a day reports its plays and how long they lasted', () => {
  assert.strictEqual(calendarTooltipLabel('7', '1h 12m'), '7 plays · 1h 12m');
});

run('one play is singular', () => {
  assert.strictEqual(calendarTooltipLabel('1', '4m'), '1 play · 4m');
  assert.strictEqual(calendarTooltipLabel(1, '4m'), '1 play · 4m');
});

run('a day with no time attribute reports only its plays', () => {
  // A quiet day carries no data-time at all, so getAttribute answers null -
  // "0 plays · 0s" would say the same thing twice.
  assert.strictEqual(calendarTooltipLabel('0', null), '0 plays');
  assert.strictEqual(calendarTooltipLabel('3', ''), '3 plays');
});

// --- the friends strip's 15s poll --------------------------------------------

const KEVIN = { username: 'kevin', displayName: 'Kevin', name: 'Nightcall',
                artistsText: 'Kavinsky', imageId: 'img1', compareUrl: '/compare?with=kevin' };

run('the same strip twice is the same signature', () => {
  assert.strictEqual(friendsStripSignature([KEVIN], 0), friendsStripSignature([KEVIN], 0));
});

run('a friend changing track changes it', () => {
  const later = Object.assign({}, KEVIN, { name: 'Rampage' });
  assert.notStrictEqual(friendsStripSignature([KEVIN], 0), friendsStripSignature([later], 0));
});

run('every field the chip renders is part of it', () => {
  for (const field of ['username', 'displayName', 'name', 'artistsText', 'imageId', 'compareUrl']) {
    const changed = Object.assign({}, KEVIN, { [field]: 'something else' });
    assert.notStrictEqual(friendsStripSignature([KEVIN], 0),
                          friendsStripSignature([changed], 0), `${field} is not in the signature`);
  }
});

run('the overflow count is part of it', () => {
  assert.notStrictEqual(friendsStripSignature([KEVIN], 0), friendsStripSignature([KEVIN], 3));
});

run('who is listening, and in what order, is part of it', () => {
  const alex = Object.assign({}, KEVIN, { username: 'alex', compareUrl: '/compare?with=alex' });
  assert.notStrictEqual(friendsStripSignature([KEVIN, alex], 0),
                        friendsStripSignature([alex, KEVIN], 0));
  assert.notStrictEqual(friendsStripSignature([KEVIN], 0), friendsStripSignature([KEVIN, alex], 0));
});

run('two friends cannot collide by concatenation', () => {
  // A separator that could appear in a track name would make "A" + "B" and
  // "AB" + "" the same strip.
  const a = Object.assign({}, KEVIN, { name: 'One', artistsText: 'Two' });
  const b = Object.assign({}, KEVIN, { name: 'One Two', artistsText: '' });
  assert.notStrictEqual(friendsStripSignature([a], 0), friendsStripSignature([b], 0));
});

run('nothing playing is a stable signature', () => {
  assert.strictEqual(friendsStripSignature([], 0), friendsStripSignature([], 0));
});

console.log('all dashboard-page tests passed');
