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
  //< `tag` so the chip-link tests can tell an <a> from the <span> a link-less
  //  field falls back to
  createElement(tag) { return { tag, classList: { add() {} }, appendChild() {} }; },
};

const { calendarTooltipLabel, friendsStripSignature, friendsChipAnchor,
        friendsChipLink, progressPercent, pollIsStale } = require('../static/js/dashboard-page.js');

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
                artistsText: 'Kavinsky', imageId: 'img1', compareUrl: '/compare?with=kevin',
                trackUrl: '/song/t1',
                artists: [{ name: 'Kavinsky', url: '/artist/art1' }] };

run('the same strip twice is the same signature', () => {
  assert.strictEqual(friendsStripSignature([KEVIN], 0), friendsStripSignature([KEVIN], 0));
});

run('a friend changing track changes it', () => {
  const later = Object.assign({}, KEVIN, { name: 'Rampage' });
  assert.notStrictEqual(friendsStripSignature([KEVIN], 0), friendsStripSignature([later], 0));
});

run('every field the chip renders is part of it', () => {
  for (const field of ['username', 'displayName', 'name', 'artistsText', 'imageId',
                       'compareUrl', 'trackUrl']) {
    const changed = Object.assign({}, KEVIN, { [field]: 'something else' });
    assert.notStrictEqual(friendsStripSignature([KEVIN], 0),
                          friendsStripSignature([changed], 0), `${field} is not in the signature`);
  }
});

run('an artist link that moves is part of it', () => {
  // The same artist can go from Spotify to our own /artist page the moment the
  // viewer's first play of them lands - same name, different href.
  const played = Object.assign({}, KEVIN, {
    artists: [{ name: 'Kavinsky', url: 'https://open.spotify.com/artist/art1' }],
  });
  assert.notStrictEqual(friendsStripSignature([KEVIN], 0), friendsStripSignature([played], 0));
});

run('a credited artist appearing or vanishing is part of it', () => {
  const feat = Object.assign({}, KEVIN, {
    artists: KEVIN.artists.concat([{ name: 'Lovefoxxx', url: '/artist/art2' }]),
  });
  assert.notStrictEqual(friendsStripSignature([KEVIN], 0), friendsStripSignature([feat], 0));
});

run('a chip with no artists does not serialize like one with them', () => {
  // The artists are flattened into the same field list as everything else, so
  // this is the collision that shape has to survive.
  const bare = Object.assign({}, KEVIN, { artists: [] });
  const named = Object.assign({}, KEVIN, { artists: [{ name: '', url: '' }] });
  assert.notStrictEqual(friendsStripSignature([bare], 0), friendsStripSignature([named], 0));
});

run('a payload with no artists key at all is still stable', () => {
  const legacy = Object.assign({}, KEVIN, { artists: undefined });
  assert.strictEqual(friendsStripSignature([legacy], 0), friendsStripSignature([legacy], 0));
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

// --- a chip's three links -----------------------------------------------------
// A chip names a person, a track and some artists, and each goes somewhere
// different. Which of our own pages vs. Spotify is the server's call
// (routes/system.py::_friendChip); what is decided HERE is only whether the
// link leaves the app, which is read straight off the URL.

run('an app-relative link stays in this tab', () => {
  const el = friendsChipLink('Nightcall', '/song/t1', 'friends-listening-link');

  assert.strictEqual(el.tag, 'a');
  assert.strictEqual(el.href, '/song/t1');
  assert.strictEqual(el.textContent, 'Nightcall');
  assert.strictEqual(el.target, undefined);
  assert.strictEqual(el.rel, undefined);
});

run('a Spotify link opens in a new tab, unable to reach back', () => {
  const el = friendsChipLink('Nightcall', 'https://open.spotify.com/track/t1',
                             'friends-listening-link');

  assert.strictEqual(el.target, '_blank');
  assert.strictEqual(el.rel, 'noreferrer noopener');
});

run('nothing to link to is text, not an empty anchor', () => {
  // An <a href=""> is a link back to the current page, which is worse than no
  // link: the strip would look clickable and reload the dashboard.
  const el = friendsChipLink('Some Local File', '', 'friends-listening-link');

  assert.strictEqual(el.tag, 'span');
  assert.strictEqual(el.href, undefined);
  assert.strictEqual(el.textContent, 'Some Local File');
});

run('a missing name renders empty rather than "undefined"', () => {
  assert.strictEqual(friendsChipLink(undefined, '/song/t1', 'x').textContent, '');
});

run('the cover wrapper is the same link with nothing written into it', () => {
  // The cover art links to the track like its title does, so it shares the
  // anchor builder - but it holds an <img>, and a stray '' textContent would
  // be a text node beside it.
  const el = friendsChipAnchor('/song/t1', 'friends-listening-cover-link');

  assert.strictEqual(el.tag, 'a');
  assert.strictEqual(el.href, '/song/t1');
  assert.strictEqual(el.textContent, undefined);
});

run('a cover with no track to link to wraps nothing clickable', () => {
  assert.strictEqual(friendsChipAnchor('', 'friends-listening-cover-link').tag, 'span');
});

// --- the progress bar between polls ------------------------------------------
// The payload carries the position as of the moment the poll landed, so with
// nothing else the bar stepped once every 15 seconds. This advances it from
// there, which is the same arithmetic the server does against the connect
// state's own timestamp (Database/workers/listener.py::getNowPlaying).

const FOUR_MINUTES_MS = 240000;

run('the bar advances with the time since the poll landed', () => {
  assert.strictEqual(progressPercent(120000, FOUR_MINUTES_MS, 30000, false), 62.5);
});

run('a paused track stays exactly where it stopped', () => {
  assert.strictEqual(progressPercent(120000, FOUR_MINUTES_MS, 30000, true), 50);
});

run('the bar never runs past the end of the track', () => {
  // A track that ended between polls: the server drops it on the next one, and
  // until then a bar past 100% would render outside its own track.
  assert.strictEqual(progressPercent(230000, FOUR_MINUTES_MS, 60000, false), 100);
});

run('a track with no duration reports no bar at all', () => {
  // Podcasts and local files arrive without one; the caller hides the track.
  assert.strictEqual(progressPercent(1000, 0, 0, false), null);
  assert.strictEqual(progressPercent(1000, undefined, 0, false), null);
});

run('a clock that jumped backwards does not rewind the bar', () => {
  // Wall-clock time is not monotonic - an NTP correction or a laptop waking up
  // can hand back a negative elapsed.
  assert.strictEqual(progressPercent(120000, FOUR_MINUTES_MS, -5000, false), 50);
});

// --- the poll's own health ---------------------------------------------------

run('a single missed poll is not worth flagging', () => {
  // A 15s poll drops one request on any flaky connection; saying "stale" there
  // would cry wolf often enough that nobody reads it.
  assert.strictEqual(pollIsStale(0), false);
  assert.strictEqual(pollIsStale(1), false);
  assert.strictEqual(pollIsStale(2), false);
});

run('three in a row means what is on screen may be minutes old', () => {
  assert.strictEqual(pollIsStale(3), true);
  assert.strictEqual(pollIsStale(9), true);
});

console.log('all dashboard-page tests passed');
