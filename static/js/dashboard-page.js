// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// The dashboard: what is left of its browser logic once htmx owns the
// request/swap layer (see templates/tracks.html for the attributes), plus the
// two things htmx could not take - the now-playing poll and the
// listening-calendar tooltip.
//
// Extracted from tracks.html so it can be linted - it was the single largest
// unlinted file in the project. USERNAME stays behind as a data island because
// the poll builds cover-image URLs from it.
//
// Three of this file's four fetches went to htmx, and the mapping is worth
// keeping written down:
//
//   loadDashboardSummary + AbortController -> hx-get + hx-sync="...:replace"
//   replaceDashboardUrl                    -> hx-replace-url="true"
//   updateDashboardFilters' set/delete     -> the form's own inputs, serialized
//     surgery on the query string             by htmx, plus `disabled` on the
//                                             custom dates when not custom
//   updateDashboardDateFilter's range      -> HtmxFilters.rangeProblem, vetoing
//     check + error styling                   the request in htmx:configRequest
//   the loading-fade bookkeeping           -> .htmx-fade-target + hx-indicator
//   the 401 -> /login branch               -> HX-Redirect, sent by the server
//   the popstate handler                   -> nothing, and deliberately: every
//     URL update here REPLACES, so this page never puts an entry on the history
//     stack for itself. It only ever ran on a cross-document Back, which
//     reloads the page server-side anyway - and DASHBOARD_DEFAULT_WINDOW, the
//     fallback it needed to re-select the right option, went with it.
//   the Discover + trends card fetches     -> hx-trigger="load" on each card,
//     and the DOM they built by hand          answering with markup
//
// The FOURTH fetch, the now-playing poll below, deliberately stayed. htmx can
// issue a request every 15s, but not do either of the things this poll exists
// for: it renders DATA into a dozen elements with per-link logic (an internal
// /song/<id> link only when the track has actually been played before, a
// Spotify link otherwise, plain text with no id at all), and it must STOP on a
// 401 rather than navigate - a background poll yanking the page to /login
// mid-read is the exact bug its 401 branch was added to fix, while every htmx
// request in the app is answered with HX-Redirect, which navigates. Stopping a
// poll client-side needs hx-on::, and that compiles a JS expression with the
// Function constructor, which this page's CSP denies.

//< the form htmx watches, in tracks.html
var DASHBOARD_FORM_ID = 'dashboardFilters';
//< the swap target
var DASHBOARD_SUMMARY_ID = 'dashboardSummary';

var byId = function (id) { return document.getElementById(id); };

// Called from the Time Period select's onchange. Runs before htmx's listener
// (an inline on*= handler fires at the target; htmx's is on the form and fires
// as the event bubbles), so the disabled flags are already right by the time
// the request is serialized.
//
// `disabled`, not merely hidden: a disabled control is not serialized, which is
// what keeps a stale custom range out of the request - and so out of the URL -
// after switching back to a named interval.
window.updateDashboardInterval = function () { HtmxFilters.syncCustomRange('dashboardCustomDates'); };

// The one place a request gets vetoed, and what stops "custom" firing one the
// moment it is selected: a range with no dates yet is RANGE_INCOMPLETE, which
// is exactly what the old handler's early return covered. Shared with every
// other filter page - /history, /charts, /genres, /compare and the Top lists
// render the same control set, so the logic lives once in
// static/js/htmx-filters.js and tests/test_custom_date_controls.py keeps it
// that way.
document.body.addEventListener('htmx:configRequest', function (evt) {
  if (!evt.detail.elt || evt.detail.elt.id !== DASHBOARD_FORM_ID) return;
  var problem = HtmxFilters.rangeProblemFromDom();
  HtmxFilters.showRangeError(problem);
  if (problem !== HtmxFilters.RANGE_OK) {
    evt.preventDefault();
    return;
  }
  HtmxFilters.pruneEmptyParams(evt.detail.parameters);
});

// A genuine failure replaces the stuck/stale cards with an inline error +
// Retry, so the numbers are never silently stale under a URL that says
// otherwise. Scoped to the summary swap: the two deferred cards below fail
// independently and keep their own placeholders rather than blanking this.
var reportDashboardFailure = function (evt) {
  var target = byId(DASHBOARD_SUMMARY_ID);
  if (!target || !window.AjaxStatus || !evt.detail || evt.detail.target !== target) return;
  window.AjaxStatus.renderInto(target, function () {
    htmx.ajax('GET', window.location.pathname + window.location.search,
              { target: '#' + DASHBOARD_SUMMARY_ID, swap: 'innerHTML' });
  });
};
document.body.addEventListener('htmx:responseError', reportDashboardFailure);
document.body.addEventListener('htmx:sendError', reportDashboardFailure);

// Now Playing: poll the listener's cached connect state (no Spotify
// calls server-side) and show the now-playing sub-panel only while
// something is playing. The listening streak below it stays put.
(function () {
  var NOW_PLAYING_POLL_MS = 15000;
  var card = document.getElementById('nowPlayingCard');
  var panel = document.getElementById('nowPlayingPanel');
  // Friends' current tracks ride along on this same poll (see
  // routes/system.py) so the strip costs no extra requests. Looked up here,
  // beside the now-playing elements, because the guard below has to know
  // about BOTH: the two halves are independently absent (the strip isn't
  // rendered without shares or with the admin switch off), so neither may
  // gate the other's polling.
  var friendsRow = document.getElementById('friendsListening');
  var friendsChips = document.getElementById('friendsListeningChips');
  var friendsMore = document.getElementById('friendsListeningMore');
  if (!card && !friendsRow) return;   //< nothing on this page to poll for

  function render(np) {
    if (!card) return;   //< now-playing card absent; the strip may still poll
    if (!np || !np.name) {
      card.style.display = 'none';
      if (panel) panel.classList.remove('has-now-playing');
      return;
    }
    var nameEl = document.getElementById('nowPlayingName');
    nameEl.textContent = np.name;

    // Link the title (and cover) to our own /song/<id> page when the user
    // has actually played this track before; otherwise fall back to Spotify
    // (a track playing for the first time has no completed play logged yet,
    // so the internal detail page would have nothing to show). No id at all
    // (local files / podcasts / transitional states) -> plain text.
    function applyTrackLink(el) {
      if (!el) return;
      if (np.trackId && np.trackPlayed) {
        el.href = '/song/' + encodeURIComponent(np.trackId);
        el.removeAttribute('target');
        el.removeAttribute('rel');
      } else if (np.trackId) {
        el.href = 'https://open.spotify.com/track/' + encodeURIComponent(np.trackId);
        el.target = '_blank';
        el.rel = 'noreferrer noopener';
      } else {
        el.removeAttribute('href');
        el.removeAttribute('target');
        el.removeAttribute('rel');
      }
    }
    applyTrackLink(nameEl);
    applyTrackLink(document.getElementById('nowPlayingCoverLink'));

    // Artist names: link each to our /artist/<id> page when the user has
    // played that artist before, else to their Spotify page (first play).
    // The first-listen fallback carries no artist ids, so keep plain text.
    var artistsEl = document.getElementById('nowPlayingArtists');
    if (np.artists && np.artists.length) {
      artistsEl.textContent = '';
      np.artists.forEach(function (a, i) {
        if (i) artistsEl.appendChild(document.createTextNode(', '));
        var el;
        if (a.id && a.played) {
          el = document.createElement('a');
          el.href = '/artist/' + encodeURIComponent(a.id);
        } else if (a.id) {
          el = document.createElement('a');
          el.href = 'https://open.spotify.com/artist/' + encodeURIComponent(a.id);
          el.target = '_blank';
          el.rel = 'noreferrer noopener';
        } else {
          el = document.createElement('span');
        }
        el.className = 'now-playing-artist-link';
        el.textContent = a.name;
        artistsEl.appendChild(el);
      });
    } else {
      artistsEl.textContent = np.artistsText || '';
    }

    var stateEl = document.getElementById('nowPlayingState');
    stateEl.textContent = np.isPaused ? 'Paused' : 'Playing';
    stateEl.classList.toggle('paused', !!np.isPaused);
    var cover = document.getElementById('nowPlayingCover');
    var coverLink = document.getElementById('nowPlayingCoverLink');
    if (np.imageId) {
      var src = '/img/' + encodeURIComponent(USERNAME) + '/tracks/' + encodeURIComponent(np.imageId) + '.jpeg';
      if (cover.getAttribute('src') !== src) cover.src = src;
      cover.style.display = '';
      if (coverLink) coverLink.style.display = '';
    } else {
      cover.style.display = 'none';
      if (coverLink) coverLink.style.display = 'none';
    }
    var bar = document.getElementById('nowPlayingBar');
    if (np.durationMs > 0) {
      bar.parentElement.style.display = '';
      bar.style.width = Math.min(100, (np.positionMs / np.durationMs) * 100) + '%';
    } else {
      bar.parentElement.style.display = 'none';
    }
    card.style.display = '';
    if (panel) panel.classList.add('has-now-playing');
  }

  function renderFriends(friends, moreCount) {
    if (!friendsRow) return;   //< admin switch off: the row isn't rendered
    friends = friends || [];
    if (!friends.length) {
      friendsRow.style.display = 'none';
      return;
    }
    friendsChips.textContent = '';
    friends.forEach(function (friend) {
      var chip = document.createElement('div');
      chip.className = 'friends-listening-chip';

      var cover = document.createElement('img');
      cover.className = 'friends-listening-cover';
      cover.alt = '';
      //< catalog images are shared files; the <username> segment is only an
      //  authorization check, so they resolve under the VIEWER's own name
      cover.src = friend.imageId
        ? '/img/' + encodeURIComponent(USERNAME) + '/tracks/' + encodeURIComponent(friend.imageId) + '.jpeg'
        : window.PLACEHOLDER_IMG;
      cover.onerror = function () { this.onerror = null; this.src = window.PLACEHOLDER_IMG; };
      chip.appendChild(cover);

      var meta = document.createElement('div');
      meta.className = 'friends-listening-meta';
      var title = document.createElement('div');
      title.className = 'friends-listening-track';
      //< textContent throughout: names are other users' data
      title.textContent = (friend.displayName || friend.username) + ' · ' + (friend.name || '');
      var artist = document.createElement('div');
      artist.className = 'friends-listening-artist';
      artist.textContent = friend.artistsText || '';
      meta.appendChild(title);
      meta.appendChild(artist);
      chip.appendChild(meta);
      friendsChips.appendChild(chip);
    });

    if (moreCount > 0) {
      friendsMore.textContent = '+' + moreCount + ' more';
      friendsMore.style.display = '';
    } else {
      friendsMore.style.display = 'none';
    }
    friendsRow.style.display = '';
  }

  var pollTimer = null;

  function poll() {
    fetch('/api/now-playing')
      .then(function (resp) {
        // An expired session used to fall into the `null` branch below and be
        // treated as a transient blip, so the card and the friends strip sat
        // frozen on stale content for as long as the tab stayed open, polling
        // every 15s forever and never redirecting. A background poll shouldn't
        // yank the page away mid-read, so it stops rather than navigates - the
        // next click on anything goes through the normal login redirect.
        if (resp.status === 401) {
          if (pollTimer !== null) {
            clearInterval(pollTimer);
            pollTimer = null;
          }
          return null;
        }
        return resp.ok ? resp.json() : null;
      })
      .then(function (data) {
        if (!data) return;
        render(data.nowPlaying);
        renderFriends(data.friends, data.friendsMoreCount);
      })
      .catch(function () { /* transient network error - keep last state */ });
  }

  poll();
  pollTimer = setInterval(poll, NOW_PLAYING_POLL_MS);
})();

// The two deferred cards fail independently of the summary and of each other,
// and htmx swaps nothing on a non-2xx - so without these both would sit on a
// placeholder claiming work is still in progress. They fail DIFFERENTLY, which
// is why this is two handlers and not one:
//
//   Discover goes blank. Its three states are locked / empty / a list, and the
//     first two are statements about the user's own library that a server error
//     is no evidence for - the old fetch()'s catch made the same call.
//   Trends gets the shared inline error + Retry, which is what its own catch
//     did: three "Loading listening trends…" placeholders say nothing useful on
//     their own and would otherwise stay up forever.
document.body.addEventListener('htmx:responseError', function (evt) {
  if (!evt.detail) return;
  var card = document.getElementById('discoverCard');
  if (card && evt.detail.target === card) {
    card.replaceChildren();
    return;
  }
  var trends = document.getElementById('dashboardTrendsContainer');
  if (trends && evt.detail.target === trends && window.AjaxStatus) {
    window.AjaxStatus.renderInto(trends, function () {
      htmx.ajax('GET', '/api/dashboard-trends',
                { target: '#dashboardTrendsContainer', swap: 'innerHTML' });
    });
  }
});

// Listening calendar: a cursor-following overlay on hover (like the charts
// page tooltips), replacing the native `title` hint so the day's play count
// shows instantly instead of after the browser's slow title-hover delay.
// Reuses the global #chartTooltip element + .chart-tooltip styling and is
// delegated off the grid so all ~370 day cells share a single handler.
(function () {
  var grid = document.querySelector('.streak-calendar-grid');
  if (!grid) return;

  function ensureTooltip() {
    var tip = document.getElementById('chartTooltip');
    if (!tip) {
      tip = document.createElement('div');
      tip.id = 'chartTooltip';
      tip.className = 'chart-tooltip';
      document.body.appendChild(tip);
    }
    return tip;
  }

  function hideTooltip() {
    var tip = document.getElementById('chartTooltip');
    if (tip) tip.style.display = 'none';
  }

  // Build the date from its parts so the label doesn't slip a day in
  // timezones behind UTC (new Date("2026-07-20") parses as UTC midnight).
  function formatDay(iso) {
    var p = iso.split('-');
    var d = new Date(+p[0], (+p[1]) - 1, +p[2]);
    return d.toLocaleDateString(undefined,
      { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
  }

  //< bound once at load: the calendar is unfiltered, so it is never swapped
  grid.addEventListener('mousemove', function (evt) {
    // Future cells carry no data-date, so they (and the gaps) get no tooltip.
    var cell = evt.target.closest('.streak-calendar-day[data-date]');
    if (!cell) { hideTooltip(); return; }
    var count = cell.getAttribute('data-count');
    var tip = ensureTooltip();
    // Built as nodes rather than an innerHTML concat: both values come off
    // data-* attributes, and reinterpreting DOM text as HTML is the one
    // step that would turn a future non-numeric count into markup.
    var day = document.createElement('strong');
    day.textContent = formatDay(cell.getAttribute('data-date'));
    tip.replaceChildren(day, document.createElement('br'),
      document.createTextNode(count + ' play' + (count === '1' ? '' : 's')));
    tip.style.left = (evt.clientX + 14) + 'px';
    tip.style.top = (evt.clientY + 14) + 'px';
    tip.style.display = 'block';
  });
  grid.addEventListener('mouseleave', hideTooltip);
})();
