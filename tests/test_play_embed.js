// Plain-node unit test for the "Play now" embedded-player state machine
// (static/js/play-embed.js). No test framework/dependency - run with:
//   node tests/test_play_embed.js
const assert = require('assert');
const {
  nextPlayEmbedState, embedHeightFor, EMBED_HEIGHT_PX, initPlayEmbed, SCRIPT_FAILED_NOTICE,
} = require('../static/js/play-embed.js');

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

// --- the API script failing to load ------------------------------------------
// Without a script-error path the phase stayed 'loading' forever: the button
// kept toggling an empty player box and no click could ever recover.

run('a failed script load returns to idle so the next click can retry', () => {
  const afterClick = nextPlayEmbedState({ phase: 'idle', visible: false }, 'click');
  assert.strictEqual(afterClick.phase, 'loading');

  const afterError = nextPlayEmbedState(afterClick, 'script-error');

  assert.strictEqual(afterError.phase, 'idle');
  assert.strictEqual(afterError.visible, false);
  assert.strictEqual(afterError.action, 'script-failed');
});

run('a failed load restores the Play label rather than leaving Hide', () => {
  const afterError = nextPlayEmbedState({ phase: 'loading', visible: true }, 'script-error');

  assert.strictEqual(afterError.label, 'Play now');
});

run('clicking again after a failure requests the script once more', () => {
  const afterError = nextPlayEmbedState({ phase: 'loading', visible: true }, 'script-error');

  const retry = nextPlayEmbedState(afterError, 'click');

  assert.strictEqual(retry.action, 'load-script');
});

run('a script error once the player is ready changes nothing', () => {
  const ready = { phase: 'ready', visible: true };

  const after = nextPlayEmbedState(ready, 'script-error');

  assert.strictEqual(after.phase, 'ready');
  assert.strictEqual(after.action, 'none');
});

// --- what that failure actually renders --------------------------------------
// The notice used to build an "Open in Spotify" anchor of its own, because the
// hero card had none: the Play now button REPLACED the card's anchor. The card
// carries both now (templates/_track_card.html), so a second link a few pixels
// under the first is noise - the notice points at the one already there. Which
// nodes get built is wiring, not state, so this drives initPlayEmbed against a
// hand-rolled page (same approach as tests/test_back_button.js, no jsdom).

const SPOTIFY_URL = 'https://open.spotify.com/track/abc';

function fakeElement(tagName) {
  const element = {
    tagName,
    className: '',
    textContent: '',
    hidden: false,
    offsetHeight: 0,
    children: [],
    listeners: {},
    attributes: {},
    style: { setProperty() {} },
    classList: {
      names: [],
      add(name) { element.classList.names.push(name); },
      remove(name) { element.classList.names = element.classList.names.filter((n) => n !== name); },
      contains(name) { return element.classList.names.includes(name); },
    },
    appendChild(child) { element.children.push(child); return child; },
    addEventListener(event, handler) { element.listeners[event] = handler; },
    setAttribute(name, value) { element.attributes[name] = value; },
  };
  return element;
}

/** A page whose first Play-now click gets no API script back. */
function failedScriptLoad() {
  const button = fakeElement('button');
  button.dataset = { spotifyUrl: SPOTIFY_URL, embedType: 'track' };
  button.textContent = 'Play now';
  const container = fakeElement('section');
  const slot = fakeElement('div');
  const created = [];
  const body = fakeElement('body');

  global.window = {};
  global.document = {
    body,
    querySelector(selector) { return selector === '.play-now-button' ? button : null; },
    getElementById(id) {
      if (id === 'play-embed') { return container; }
      if (id === 'play-embed-slot') { return slot; }
      return null;
    },
    createElement(tagName) {
      const element = fakeElement(tagName);
      created.push(element);
      return element;
    },
  };

  initPlayEmbed();
  button.listeners.click();              //< requests the API script...
  body.children[0].listeners.error();    //< ...which never arrives

  return { button, container, slot, created };
}

run('the failure notice replaces the slot and says what happened', () => {
  const { slot } = failedScriptLoad();

  assert.strictEqual(slot.children.length, 1);
  assert.strictEqual(slot.children[0].tagName, 'p');
  assert.strictEqual(slot.children[0].className, 'dashboard-card-empty');
  assert.strictEqual(slot.children[0].textContent, SCRIPT_FAILED_NOTICE);
});

run('the notice sends the visitor to the pill on the card, not to a link of its own', () => {
  const { slot, created } = failedScriptLoad();

  assert.ok(SCRIPT_FAILED_NOTICE.includes('Open in Spotify'));
  assert.deepStrictEqual(created.filter((element) => element.tagName === 'a'), []);
  assert.strictEqual(slot.children[0].children.length, 0);
});

run('the failure still reveals the container and frees the button to retry', () => {
  const { button, container } = failedScriptLoad();

  assert.strictEqual(container.hidden, false);
  assert.ok(container.classList.contains('is-visible'));
  assert.strictEqual(button.textContent, 'Play now');
  assert.strictEqual(button.attributes['aria-expanded'], 'true');
});

console.log('All play-embed tests passed.');
