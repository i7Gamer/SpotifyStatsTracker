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
    } else if (action === 'create') {
      createController(IFrameAPI, false);
    } else if (action === 'create-and-play') {
      createController(IFrameAPI, true);
    } else if (action === 'play') {
      safePlay();
    } else if (action === 'pause' && controller) {
      controller.pause();
    }

    container.hidden = !state.visible;
    button.textContent = next.label;
    button.setAttribute('aria-expanded', String(state.visible));
  }

  button.addEventListener('click', () => dispatch('click'));
}

if (typeof document !== 'undefined') {
  initPlayEmbed();
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { nextPlayEmbedState, embedHeightFor, EMBED_HEIGHT_PX, SPOTIFY_IFRAME_API_SRC };
}
