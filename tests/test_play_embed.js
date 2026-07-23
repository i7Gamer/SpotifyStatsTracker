// Plain-node unit test for the "Play now" embedded-player state machine
// (static/js/play-embed.js). No test framework/dependency - run with:
//   node tests/test_play_embed.js
const assert = require('assert');
const { nextPlayEmbedState, embedHeightFor, EMBED_HEIGHT_PX } = require('../static/js/play-embed.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

const IDLE = { phase: 'idle', visible: false };

run('first click loads the script and reveals the player', () => {
  assert.deepStrictEqual(nextPlayEmbedState(IDLE, 'click'), {
    phase: 'loading', visible: true, action: 'load-script', label: 'Hide player',
  });
});

run('clicking again while the script is still loading just hides (no second load)', () => {
  const shown = { phase: 'loading', visible: true };
  assert.deepStrictEqual(nextPlayEmbedState(shown, 'click'), {
    phase: 'loading', visible: false, action: 'none', label: 'Play now',
  });
});

run('re-showing while still loading stays a no-op (script requested once)', () => {
  const hiddenDuringLoad = { phase: 'loading', visible: false };
  assert.deepStrictEqual(nextPlayEmbedState(hiddenDuringLoad, 'click'), {
    phase: 'loading', visible: true, action: 'none', label: 'Hide player',
  });
});

run('api ready while visible creates the controller and autoplays', () => {
  const loadingVisible = { phase: 'loading', visible: true };
  assert.deepStrictEqual(nextPlayEmbedState(loadingVisible, 'api-ready'), {
    phase: 'ready', visible: true, action: 'create-and-play', label: 'Hide player',
  });
});

run('api ready while hidden creates the controller but does not autoplay', () => {
  const loadingHidden = { phase: 'loading', visible: false };
  assert.deepStrictEqual(nextPlayEmbedState(loadingHidden, 'api-ready'), {
    phase: 'ready', visible: false, action: 'create', label: 'Play now',
  });
});

run('clicking a visible ready player hides and pauses it', () => {
  const readyVisible = { phase: 'ready', visible: true };
  assert.deepStrictEqual(nextPlayEmbedState(readyVisible, 'click'), {
    phase: 'ready', visible: false, action: 'pause', label: 'Play now',
  });
});

run('clicking a hidden ready player shows and resumes playback', () => {
  const readyHidden = { phase: 'ready', visible: false };
  assert.deepStrictEqual(nextPlayEmbedState(readyHidden, 'click'), {
    phase: 'ready', visible: true, action: 'play', label: 'Hide player',
  });
});

run('a stray api-ready in idle is ignored', () => {
  assert.deepStrictEqual(nextPlayEmbedState(IDLE, 'api-ready'), {
    phase: 'idle', visible: false, action: 'none', label: 'Play now',
  });
});

run('a stray api-ready when already ready is ignored and preserves visibility', () => {
  const readyVisible = { phase: 'ready', visible: true };
  assert.deepStrictEqual(nextPlayEmbedState(readyVisible, 'api-ready'), {
    phase: 'ready', visible: true, action: 'none', label: 'Hide player',
  });
});

run('embedHeightFor returns the per-entity heights', () => {
  assert.strictEqual(embedHeightFor('track'), EMBED_HEIGHT_PX.track);
  assert.strictEqual(embedHeightFor('artist'), EMBED_HEIGHT_PX.artist);
  assert.strictEqual(embedHeightFor('album'), EMBED_HEIGHT_PX.album);
});

run('embedHeightFor falls back to the track height for unknown types', () => {
  assert.strictEqual(embedHeightFor('playlist'), EMBED_HEIGHT_PX.track);
  assert.strictEqual(embedHeightFor(undefined), EMBED_HEIGHT_PX.track);
});

console.log('All play-embed tests passed.');
