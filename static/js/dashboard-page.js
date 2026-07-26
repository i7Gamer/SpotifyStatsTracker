// The dashboard: the Time Period filter's AJAX card refresh, the now-playing
// poll (which also carries the friends-listening strip), the Discover card's
// deferred load, the listening-calendar tooltip, and the trends fetch.
//
// Extracted from tracks.html so it can be linted - it was the single largest
// unlinted file in the project. The three server values it needs stay behind
// as a data island declared before this loads; USERNAME was declared twice,
// once inside each of two IIFEs, and is now declared once at the top with the
// same value, which those IIFEs read identically.
// Dashboard filter only drives the stats cards' Time Period now - search
// and the paginated list moved to /history, so no q/page handling here.
// A change fetches just the four cards via ?ajax=true and fades them in
// in place (same pattern as compare.html/genres.html) instead of a full
// page reload - the live cards above (streak, on this day, discover,
// calendar) and next-milestones are unfiltered and stay untouched.
var DASHBOARD_FADE_MS = 200;
var activeDashboardLoad = null;

function loadDashboardSummary() {
  var target = document.getElementById('dashboardSummary');
  if (activeDashboardLoad) {
    activeDashboardLoad.controller.abort();
    target.classList.remove('loading-fade');
  }
  var controller = new AbortController();
  activeDashboardLoad = { controller: controller };
  target.classList.add('loading-fade');

  var params = new URLSearchParams(window.location.search);
  params.set('ajax', 'true');
  var delay = new Promise(function (resolve) { setTimeout(resolve, DASHBOARD_FADE_MS); });
  var fetched = fetch(window.location.pathname + '?' + params.toString(), { signal: controller.signal })
    .then(function (response) { return response.json(); });

  Promise.all([fetched, delay])
    .then(function (results) {
      var data = results[0];
      //< a response that resolved before its abort can still reach here -
      //  never swap stale data in over a newer load's
      if (!activeDashboardLoad || activeDashboardLoad.controller !== controller) {
        return;
      }
      target.innerHTML = data.summaryHtml;
    })
    .catch(function (err) {
      if (err.name !== 'AbortError') {
        console.error(err);
        //< genuine failure (not superseded): replace the stuck/stale cards
        //  with an inline error + Retry so the numbers aren't silently stale
        if ((!activeDashboardLoad || activeDashboardLoad.controller === controller) && window.AjaxStatus) {
          window.AjaxStatus.renderInto(target, function () { loadDashboardSummary(); });
        }
      }
    })
    .finally(function () {
      if (activeDashboardLoad && activeDashboardLoad.controller === controller) {
        activeDashboardLoad = null;
        target.classList.remove('loading-fade');
      }
    });
}

// Update the URL in place (replaceState, not push) so a filter change stays
// shareable/refreshable without stacking a history entry - Back then returns
// to the previous page instead of stepping back through past filters.
function replaceDashboardUrl(mutate) {
  var params = new URLSearchParams(window.location.search);
  mutate(params);
  params.delete('ajax');
  var query = params.toString();
  window.history.replaceState({}, '', window.location.pathname + (query ? '?' + query : ''));
}

function updateDashboardInterval() {
  const interval = document.getElementById('interval').value;
  const customDates = document.getElementById('dashboardCustomDates');

  if (interval === 'custom') {
    customDates.style.display = 'flex';
    return;
  }

  customDates.style.display = 'none';
  updateDashboardFilters();
}

function updateDashboardDateFilter() {
  const startDateElem = document.getElementById('startDate');
  const endDateElem = document.getElementById('endDate');
  const errorElem = document.getElementById('dateError');
  const startDate = startDateElem.value;
  const endDate = endDateElem.value;

  errorElem.style.display = 'none';
  startDateElem.style.borderColor = '';
  endDateElem.style.borderColor = '';

  if (startDate && endDate) {
    if (new Date(startDate) > new Date(endDate)) {
      errorElem.textContent = 'Start date cannot be after end date.';
      errorElem.style.display = 'block';
      startDateElem.style.borderColor = 'var(--accent)';
      endDateElem.style.borderColor = 'var(--accent)';
      return;
    }
    updateDashboardFilters(true);
  }
}

function updateDashboardFilters(forceCustom = false) {
  const interval = document.getElementById('interval').value;

  if (interval === 'custom' || forceCustom) {
    const startDate = document.getElementById('startDate').value;
    const endDate = document.getElementById('endDate').value;

    if (!startDate || !endDate) {
      return;
    }

    replaceDashboardUrl(function (params) {
      params.set('interval', 'custom');
      params.set('startDate', startDate);
      params.set('endDate', endDate);
    });
  } else {
    replaceDashboardUrl(function (params) {
      params.set('interval', interval);
      params.delete('startDate');
      params.delete('endDate');
    });
  }

  loadDashboardSummary();
}

// Back/forward: reconcile the filter controls with the URL, then re-fetch.
window.addEventListener('popstate', function () {
  const params = new URLSearchParams(window.location.search);
  const hasCustom = params.get('startDate') && params.get('endDate');
  const interval = hasCustom ? 'custom' : (params.get('interval') || DASHBOARD_DEFAULT_WINDOW);
  document.getElementById('interval').value = interval;
  document.getElementById('startDate').value = params.get('startDate') || '';
  document.getElementById('endDate').value = params.get('endDate') || '';
  document.getElementById('dashboardCustomDates').style.display = hasCustom ? 'flex' : 'none';
  loadDashboardSummary();
});

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
      title.textContent = friend.username + ' · ' + (friend.name || '');
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

// Discover: fetched once after first paint instead of computed inline -
// see routes/charts.py's dashboardDiscover for why (full-history queries
// that were blocking the dashboard's initial render).
(function () {
  var loadingEl = document.getElementById('discoverLoading');
  if (!loadingEl) return;
  var listEl = document.getElementById('discoverList');
  var emptyEl = document.getElementById('discoverEmpty');
  var lockedEl = document.getElementById('discoverLocked');

  function artistRow(artist) {
    var row = document.createElement('a');
    row.className = 'discover-row';
    row.href = '/artist/' + encodeURIComponent(artist.id);

    var img = document.createElement('img');
    img.className = 'discover-cover';
    img.loading = 'lazy';
    img.alt = '';
    img.src = '/img/' + encodeURIComponent(USERNAME) + '/artists/' + encodeURIComponent(artist.imageId) + '.jpeg';
    img.onerror = function () { img.onerror = null; img.src = window.PLACEHOLDER_IMG; };
    row.appendChild(img);

    var meta = document.createElement('span');
    meta.className = 'discover-meta';
    var name = document.createElement('span');
    name.className = 'discover-name';
    name.textContent = artist.name;
    meta.appendChild(name);
    if (artist.matchedGenres && artist.matchedGenres.length) {
      var genre = document.createElement('span');
      genre.className = 'discover-genre';
      genre.textContent = artist.matchedGenres[0];
      meta.appendChild(genre);
    }
    row.appendChild(meta);
    return row;
  }

  fetch('/api/dashboard-discover')
    .then(function (resp) { return resp.ok ? resp.json() : null; })
    .then(function (data) {
      loadingEl.style.display = 'none';
      if (!data || !data.unlocked) {
        lockedEl.style.display = '';
        return;
      }
      if (!data.recommendations || !data.recommendations.length) {
        emptyEl.style.display = '';
        return;
      }
      data.recommendations.forEach(function (artist) { listEl.appendChild(artistRow(artist)); });
      listEl.style.display = '';
    })
    .catch(function () {
      // Transient network error - leave the card blank rather than show
      // a locked/empty message that isn't actually true.
      loadingEl.style.display = 'none';
    });
})();

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

document.addEventListener('DOMContentLoaded', function() {
  fetch('/api/dashboard-trends')
    .then(function(res) { return res.json(); })
    .then(function(data) {
      if (data && data.trendsHtml) {
        var container = document.getElementById('dashboardTrendsContainer');
        if (container) container.innerHTML = data.trendsHtml;
      }
    })
    .catch(function(err) {
      console.error('Error fetching dashboard trends:', err);
    });
});
