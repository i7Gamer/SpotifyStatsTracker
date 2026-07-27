// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// The song/artist/album detail pages' "Play now" button: reveals an embedded
// Spotify player between the hero card and the charts and starts playback via
// the official Spotify iFrame API. The API script is loaded lazily on the first
// click, so visitors who never press Play pull nothing from Spotify.
//
// The button carries data-spotify-url (the entity's open.spotify.com URL) and
// data-embed-type (track|artist|album); markup lives in templates/_track_card.html
// and the #play-embed container in the three detail templates.

const PLAY_LABEL = 'Play now';
const HIDE_LABEL = 'Hide player';
const SPOTIFY_IFRAME_API_SRC = 'https://open.spotify.com/embed/iframe-api/v1';

// Shown in the player's place when the API script never loads. It names the
// card's own "Open in Spotify" pill rather than repeating it as a link here -
// see showScriptFailedFallback below.
const SCRIPT_FAILED_NOTICE = "Spotify's player couldn't be loaded. "
  + 'Use the Open in Spotify link above to listen there.';

// Spotify's standard embed heights: a compact track card vs the taller
// artist/album card that shows a tracklist.
const EMBED_HEIGHT_PX = { track: 152, artist: 352, album: 352 };

function embedHeightFor(type) {
  return EMBED_HEIGHT_PX[type] || EMBED_HEIGHT_PX.track;
}

// Pure decision function (unit-tested in tests/test_play_embed.js). Given the
// current {phase, visible} and an event ('click' | 'api-ready'), return the next
// state plus the side effect the wiring should run and the button's label.
//   phase: 'idle'   - nothing loaded yet
//          'loading' - API script requested, controller not created
//          'ready'   - controller created, can play/pause
//   action: 'load-script' | 'create' | 'create-and-play' | 'play' | 'pause' | 'none'
function nextPlayEmbedState(state, event) {
  let phase = state.phase;
  let visible = state.visible;
  let action = 'none';

  if (event === 'click') {
    if (phase === 'idle') {
      phase = 'loading';
      visible = true;
      action = 'load-script';
    } else if (phase === 'loading') {
      // The script is still in flight - toggle intent only; api-ready will
      // honor the latest visibility. Never request the script twice.
      visible = !visible;
    } else {
      visible = !visible;
      action = visible ? 'play' : 'pause';
    }
  } else if (event === 'api-ready') {
    if (phase === 'loading') {
      phase = 'ready';
      action = visible ? 'create-and-play' : 'create';
    }
  } else if (event === 'script-error') {
    // The API script never arrived (offline, blocked by an extension or a
    // strict DNS/adblock rule). Without this the phase stayed 'loading'
    // forever, so the button just toggled an empty box and no click could
    // ever recover - back to idle instead, and the next click retries.
    if (phase === 'loading') {
      phase = 'idle';
      visible = false;
      action = 'script-failed';
    }
  }

  return { phase, visible, action, label: visible ? HIDE_LABEL : PLAY_LABEL };
}

function initPlayEmbed() {
  let state = { phase: 'idle', visible: false };
  let controller = null;

  const button = document.querySelector('.play-now-button');
  const container = document.getElementById('play-embed');
  const slot = document.getElementById('play-embed-slot');
  if (!button || !container || !slot) {
    return;
  }

  function loadScript() {
    // The API calls this global once its script finishes; wire it before
    // injecting the loader so we never miss the callback.
    window.onSpotifyIframeApiReady = (IFrameAPI) => dispatch('api-ready', IFrameAPI);
    const script = document.createElement('script');
    script.src = SPOTIFY_IFRAME_API_SRC;
    script.async = true;
    script.addEventListener('error', () => dispatch('script-error'));
    document.body.appendChild(script);
  }

  function createController(IFrameAPI, autoplay) {
    IFrameAPI.createController(
      slot,
      {
        url: button.dataset.spotifyUrl,
        width: '100%',
        height: embedHeightFor(button.dataset.embedType),
      },
      (embedController) => {
        controller = embedController;
        if (autoplay) {
          safePlay();
        }
      },
    );
  }

  function showScriptFailedFallback() {
    // window.open used to run here, and browsers block it: this fires from the
    // injected script's async error event, not from the click, so it is not a
    // user gesture. With open.spotify.com blocked by an extension or a DNS rule
    // the button therefore did NOTHING visible at all.
    //
    // So the failure is reported in the container the player would have filled.
    // It used to carry its own "Open in Spotify" anchor as the way through,
    // because back then the hero card had none - the Play now button replaced
    // it. The card shows both now, so the notice points at that pill instead of
    // duplicating it inches below itself. The state machine has already returned
    // to 'idle', so a later click still retries the script (a transient network
    // failure recovers) - this only makes sure the visitor is told when it does
    // not.
    slot.textContent = '';
    const notice = document.createElement('p');
    notice.className = 'dashboard-card-empty';
    notice.textContent = SCRIPT_FAILED_NOTICE;
    slot.appendChild(notice);

    container.hidden = false;
    container.classList.add('is-visible');
    button.textContent = PLAY_LABEL;   //< a later click retries the script
    button.setAttribute('aria-expanded', 'true');
  }

  function safePlay() {
    if (!controller) {
      return;
    }
    // Autoplay may be blocked by the browser even off a user gesture (Spotify
    // documents this); the player stays visible for a manual click inside it.
    try {
      controller.play();
    } catch (err) {
      /* autoplay refused - leave the visible player for the user to start */
    }
  }

  function dispatch(event, IFrameAPI) {
    const next = nextPlayEmbedState(state, event);
    const action = next.action;
    state = { phase: next.phase, visible: next.visible };

    if (action === 'load-script') {
      loadScript();
    } else if (action === 'script-failed') {
      showScriptFailedFallback();
      return;   //< the fallback owns the container and the label from here
    } else if (action === 'create') {
      createController(IFrameAPI, false);
    } else if (action === 'create-and-play') {
      createController(IFrameAPI, true);
    } else if (action === 'play') {
      safePlay();
    } else if (action === 'pause' && controller) {
      controller.pause();
    }

    setContainerVisible(state.visible);
    button.textContent = next.label;
    button.setAttribute('aria-expanded', String(state.visible));
  }

  // Animates the reveal/hide via the .is-visible class (CSS transitions
  // max-height/opacity) instead of toggling `hidden`, which snaps instantly.
  // `hidden` is still applied once the hide transition finishes so the
  // collapsed player stays out of layout and the accessibility tree.
  function setContainerVisible(visible) {
    if (visible) {
      container.style.setProperty('--embed-max-height', `${embedHeightFor(button.dataset.embedType)}px`);
      container.hidden = false;
      // Force a reflow so the browser registers the collapsed state before
      // adding .is-visible - otherwise the two class changes coalesce and
      // there's nothing to transition from.
      void container.offsetHeight;
      container.classList.add('is-visible');
    } else {
      container.classList.remove('is-visible');
      container.addEventListener(
        'transitionend',
        (event) => {
          if (event.target === container && !container.classList.contains('is-visible')) {
            container.hidden = true;
          }
        },
        { once: true },
      );
    }
  }

  button.addEventListener('click', () => dispatch('click'));
}

if (typeof document !== 'undefined') {
  initPlayEmbed();
}

if (typeof module !== 'undefined' && module.exports) {
  // initPlayEmbed is exported for the DOM-stub tests only; in a browser the
  // guard above has already run it.
  module.exports = {
    nextPlayEmbedState, embedHeightFor, EMBED_HEIGHT_PX, SPOTIFY_IFRAME_API_SRC,
    initPlayEmbed, SCRIPT_FAILED_NOTICE,
  };
}
