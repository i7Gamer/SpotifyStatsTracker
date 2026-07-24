// The Wrapped page's client logic, extracted from the inline <script> in
// templates/wrapped.html so it is a static, cacheable asset instead of ~500
// lines of template-embedded JS.
//
// Server-rendered config + the time-series data arrive via the JSON
// <script type="application/json" id="wrapped-bootstrap"> data island in
// wrapped.html; everything below is verbatim from the former inline script.
// This file is loaded BEFORE charts.js, which reads window.__chartData set here.
//
// Top-level names stay global on purpose: the inline
// onchange="updateWrappedFilters()" handlers in wrapped.html depend on it.
const wrappedBootstrap = JSON.parse(document.getElementById('wrapped-bootstrap').textContent);
window.__chartData = { timeSeries: wrappedBootstrap.timeSeries };
const WRAPPED_FETCH_URL = wrappedBootstrap.fetchUrl;
const IS_PUBLIC_VIEW = wrappedBootstrap.isPublicView;
const SHARE_OWNER_NAME = wrappedBootstrap.shareOwnerName;
let currentYear = String(wrappedBootstrap.year);
let currentGroupBy = String(wrappedBootstrap.groupBy);
let currentLimit = String(wrappedBootstrap.limit);
let currentSortBy = String(wrappedBootstrap.sortBy);

const filterButtons = document.querySelectorAll('.stats-filter-button');
const categoryDivs = document.querySelectorAll('[data-category]');

filterButtons.forEach(button => {
  button.addEventListener('click', () => {
    const filter = button.dataset.filter;

    filterButtons.forEach(btn => btn.classList.remove('active'));
    button.classList.add('active');

    categoryDivs.forEach(div => {
      if (filter === 'all' || div.dataset.category === filter) {
        div.classList.add('visible');
      } else {
        div.classList.remove('visible');
      }
    });
  });
});

const allButton = document.querySelector('[data-filter="all"]');
if (allButton) {
  allButton.click();
}

// The Export Summary Card button and Share modal only exist in the DOM
// when not publicView - their handlers below use ?. so registering them
// here unconditionally is a safe no-op on the public page.


// The in-flight filter fetch ({controller, targets}) - a newer filter
// change aborts it so a slow older response can't land after (and
// clobber) the newer one's swap. Same race, same fix as Compare's
// loadCompareData (see templates/compare.html).
let activeWrappedLoad = null;

function setWrappedFade(targets, add) {
  targets.forEach(t => {
    if (!t) return;
    if (t instanceof NodeList) {
      t.forEach(el => el.classList[add ? 'add' : 'remove']('wrapped-loading-fade'));
    } else {
      t.classList[add ? 'add' : 'remove']('wrapped-loading-fade');
    }
  });
}

function loadWrappedData(year, groupBy, limit, sortBy, updateType = 'all') {
  const targets = [];
  if (updateType === 'all') {
    const summaryGrid = document.querySelector('.track-summary-grid');
    if (summaryGrid) targets.push(summaryGrid);
    document.querySelectorAll('.track-summary-grid-3').forEach(el => targets.push(el));
    const chartWrap = document.querySelector('.chart-canvas-wrap');
    if (chartWrap) targets.push(chartWrap);
    document.querySelectorAll('[data-category]').forEach(el => targets.push(el));
  } else if (updateType === 'chart') {
    const chartWrap = document.querySelector('.chart-canvas-wrap');
    if (chartWrap) targets.push(chartWrap);
  } else if (updateType === 'lists') {
    document.querySelectorAll('[data-category]').forEach(el => targets.push(el));
  }

  if (activeWrappedLoad) {
    activeWrappedLoad.controller.abort();
    //< the aborted load skips its own finally cleanup - un-fade its
    //  targets here so a wider ('all') load superseded by a narrower
    //  ('chart'/'lists') one doesn't leave the extra sections stuck
    //  mid-fade forever
    setWrappedFade(activeWrappedLoad.targets, false);
  }
  const controller = new AbortController();
  activeWrappedLoad = { controller, targets };

  setWrappedFade(targets, true);

  const hiddenYear = document.querySelector('form.filter-section input[name="year"]');
  if (hiddenYear) hiddenYear.value = year;

  const delayPromise = new Promise(resolve => setTimeout(resolve, 200));
  // Build with URLSearchParams so each value is encoded - a crafted value (e.g.
  // a year of "2024&limit=999" from a hand-edited URL restored on Back) can't
  // inject or desync other params.
  const fetchParams = new URLSearchParams({
    year: year, groupBy: groupBy, limit: limit, sortBy: sortBy,
    ajax: 'true', type: updateType,
  });
  const fetchPromise = fetch(`${WRAPPED_FETCH_URL}?${fetchParams.toString()}`, { signal: controller.signal })
    .then(response => response.json());

  Promise.all([fetchPromise, delayPromise])
    .then(([data]) => {
      //< a response that resolved before its abort can still reach here
      //  (abort() can't recall a settled promise, and the fade delay
      //  may be what it was waiting on) - never swap stale data in
      //  over a newer load's
      if (!activeWrappedLoad || activeWrappedLoad.controller !== controller) {
        return;
      }
      if (data.error) {
        console.error(data.error);
        return;
      }

      const heroTitle = document.querySelector('.hero h1');
      if (heroTitle) heroTitle.textContent = IS_PUBLIC_VIEW ? `${SHARE_OWNER_NAME}'s ${year} Wrapped` : `Your ${year} Wrapped`;
      const heroSubtitle = document.querySelector('.hero p');
      if (heroSubtitle) heroSubtitle.textContent = `A look back at what ${IS_PUBLIC_VIEW ? SHARE_OWNER_NAME : 'you'} listened to in ${year}.`;

      // 1. Update general stats if returned
      if (data.totalPlays !== undefined) {
        const playsVal = document.querySelector('.track-summary-grid .track-summary-card:nth-child(1) .summary-value');
        if (playsVal) playsVal.textContent = data.totalPlays;
        const timeVal = document.querySelector('.track-summary-grid .track-summary-card:nth-child(2) .summary-value');
        if (timeVal) timeVal.textContent = data.totalTime;

        const grid3_list = document.querySelectorAll('.track-summary-grid-3');
        if (grid3_list.length >= 2) {
          const firstGrid = grid3_list[0];
          const secondGrid = grid3_list[1];

          const streakVal = firstGrid.querySelector('.track-summary-card:nth-child(1) .summary-value');
          if (streakVal) streakVal.textContent = data.longestStreak + ' days';

          const peakVal = firstGrid.querySelector('.track-summary-card:nth-child(2) .summary-value');
          if (peakVal) peakVal.textContent = data.peakDay;

          const peakSub = firstGrid.querySelector('.track-summary-card:nth-child(2) .summary-subtitle');
          if (peakSub) peakSub.textContent = data.peakPlays + ' plays';

          const uniqueSongs = firstGrid.querySelector('.track-summary-card:nth-child(3) .summary-value');
          if (uniqueSongs) uniqueSongs.textContent = data.uniqueSongsCount;

          const uniqueArtists = secondGrid.querySelector('.track-summary-card:nth-child(1) .summary-value');
          if (uniqueArtists) uniqueArtists.textContent = data.uniqueArtistsCount;

          const discSongs = secondGrid.querySelector('.track-summary-card:nth-child(2) .summary-value');
          if (discSongs) discSongs.textContent = data.discoveredSongsCount;

          const discArtists = secondGrid.querySelector('.track-summary-card:nth-child(3) .summary-value');
          if (discArtists) discArtists.textContent = data.discoveredArtistsCount;
        }
      }

      // Swap the live-computed genre card (year-scoped, so a year change
      // must replace it wholesale).
      const genresCard = document.getElementById('wrappedGenresCard');
      if (genresCard && data.topGenresHtml !== undefined) {
        genresCard.innerHTML = data.topGenresHtml;
      }

      // The share modal's panel is year-scoped too (create-form action
      // URL, revoke form's hidden year field, and which link - if any -
      // counts as "current" all depend on it) - re-render it on every
      // year switch, same reasoning as the genre card above.
      const sharePanelBody = document.getElementById('shareLinkPanelBody');
      if (sharePanelBody && data.sharePanelHtml !== undefined) {
        sharePanelBody.innerHTML = data.sharePanelHtml;
      }

      // 2. Update export button datasets
      const exportBtn = document.getElementById('exportWrappedBtn');
      if (exportBtn) {
        exportBtn.dataset.year = year;
        if (data.totalPlays !== undefined) {
          exportBtn.dataset.plays = data.totalPlays;
          exportBtn.dataset.time = data.totalTime;
          exportBtn.dataset.songs = data.uniqueSongsCount;
          exportBtn.dataset.artists = data.uniqueArtistsCount;
          exportBtn.dataset.streak = data.longestStreak;
          exportBtn.dataset.peakday = data.peakDay;
          exportBtn.dataset.peakplays = data.peakPlays;
          exportBtn.dataset.discoveredsongs = data.discoveredSongsCount;
          exportBtn.dataset.discoveredartists = data.discoveredArtistsCount;
          exportBtn.dataset.topsong = data.topSongText || 'N/A';
          exportBtn.dataset.topartist = data.topArtistText || 'N/A';
          exportBtn.dataset.topalbum = data.topAlbumText || 'N/A';
        }
      }

      // 3. Update lists if returned
      if (data.topSongsHtml !== undefined) {
        const topSongsList = document.querySelector('[data-category="top-songs"] .track-list');
        if (topSongsList) topSongsList.innerHTML = data.topSongsHtml;
        const topArtistsList = document.querySelector('[data-category="top-artists"] .track-list');
        if (topArtistsList) topArtistsList.innerHTML = data.topArtistsHtml;
        const topAlbumsList = document.querySelector('[data-category="top-albums"] .track-list');
        if (topAlbumsList) topAlbumsList.innerHTML = data.topAlbumsHtml;

        const discSongsDiv = document.querySelector('[data-category="discoveries-songs"]');
        if (discSongsDiv) {
          const list = discSongsDiv.querySelector('.track-list');
          if (list) list.innerHTML = data.discoveredSongsHtml;
          const hasItems = list && list.querySelector('.track-card') !== null;
          discSongsDiv.style.display = hasItems ? '' : 'none';
          const btn = document.querySelector('.stats-filter-button[data-filter="discoveries-songs"]');
          if (btn) btn.style.display = hasItems ? '' : 'none';
        }
        const discArtistsDiv = document.querySelector('[data-category="discoveries-artists"]');
        if (discArtistsDiv) {
          const list = discArtistsDiv.querySelector('.track-list');
          if (list) list.innerHTML = data.discoveredArtistsHtml;
          const hasItems = list && list.querySelector('.track-card') !== null;
          discArtistsDiv.style.display = hasItems ? '' : 'none';
          const btn = document.querySelector('.stats-filter-button[data-filter="discoveries-artists"]');
          if (btn) btn.style.display = hasItems ? '' : 'none';
        }
        const discAlbumsDiv = document.querySelector('[data-category="discoveries-albums"]');
        if (discAlbumsDiv) {
          const list = discAlbumsDiv.querySelector('.track-list');
          if (list) list.innerHTML = data.discoveredAlbumsHtml;
          const hasItems = list && list.querySelector('.track-card') !== null;
          discAlbumsDiv.style.display = hasItems ? '' : 'none';
          const btn = document.querySelector('.stats-filter-button[data-filter="discoveries-albums"]');
          if (btn) btn.style.display = hasItems ? '' : 'none';
        }

        // Fallback focus: if the currently active filter button becomes hidden, select "All Stats"
        const activeFilterBtn = document.querySelector('.stats-filter-button.active');
        if (activeFilterBtn && activeFilterBtn.style.display === 'none') {
          const allBtn = document.querySelector('.stats-filter-button[data-filter="all"]');
          if (allBtn) allBtn.click();
        }
      }

      // 4. Update chart if returned
      if (data.timeSeries !== undefined) {
        const chartTitle = document.querySelector('.chart-section h2');
        if (chartTitle) chartTitle.textContent = `Listening Over ${year}`;

        window.__chartData = window.__chartData || {};
        window.__chartData.timeSeries = data.timeSeries;
        window.__chartData.interval = groupBy;
        renderTimeSeriesChart();
      }

      // Trigger smooth cover art fade-ins for newly injected track elements
      document.querySelectorAll('img.track-cover').forEach(img => {
        if (img.complete) {
          img.classList.add('loaded');
        } else {
          img.addEventListener('load', function() {
            img.classList.add('loaded');
          });
        }
      });
    })
    .catch(err => {
      //< an abort is the expected fate of a superseded load, not an error
      if (err.name !== 'AbortError') {
        console.error(err);
      }
    })
    .finally(() => {
      //< only the still-current load cleans up - a superseded one's
      //  fades were already cleared by its successor when it aborted it
      if (activeWrappedLoad && activeWrappedLoad.controller === controller) {
        activeWrappedLoad = null;
        setWrappedFade(targets, false);
      }
    });
}

function updateWrappedFilters() {
  const yearBadge = document.querySelector('.wrapped-year-badge.active');
  const year = yearBadge ? new URL(yearBadge.href).searchParams.get('year') : new URLSearchParams(window.location.search).get('year') || String(wrappedBootstrap.year);
  const groupBy = document.getElementById('groupBy').value;
  const limit = document.getElementById('limit').value;
  const sortBy = document.getElementById('sortBy').value;

  let updateType = 'all';
  if (year === currentYear) {
    if (groupBy !== currentGroupBy && limit === currentLimit && sortBy === currentSortBy) {
      updateType = 'chart';
    } else if ((limit !== currentLimit || sortBy !== currentSortBy) && groupBy === currentGroupBy) {
      updateType = 'lists';
    }
  }

  currentYear = year;
  currentGroupBy = groupBy;
  currentLimit = limit;
  currentSortBy = sortBy;

  const newParams = new URLSearchParams();
  newParams.set('year', year);
  if (groupBy) newParams.set('groupBy', groupBy);   //< Auto: server derives from the year span
  newParams.set('limit', limit);
  newParams.set('sortBy', sortBy);
  // replaceState, not push: keep the URL shareable without stacking a history
  // entry, so Back returns to the previous page rather than past filter states.
  window.history.replaceState({}, '', window.location.pathname + '?' + newParams.toString());

  loadWrappedData(year, groupBy, limit, sortBy, updateType);
}

document.querySelectorAll('.wrapped-year-badge').forEach(badge => {
  badge.addEventListener('click', function(e) {
    e.preventDefault();
    const url = new URL(this.href);
    const year = url.searchParams.get('year');
    const groupBy = document.getElementById('groupBy').value;
    const limit = document.getElementById('limit').value;
    const sortBy = document.getElementById('sortBy').value;

    let updateType = 'all';
    if (year === currentYear) {
      if (groupBy !== currentGroupBy && limit === currentLimit && sortBy === currentSortBy) {
        updateType = 'chart';
      } else if ((limit !== currentLimit || sortBy !== currentSortBy) && groupBy === currentGroupBy) {
        updateType = 'lists';
      }
    }

    currentYear = year;
    currentGroupBy = groupBy;
    currentLimit = limit;
    currentSortBy = sortBy;

    const newParams = new URLSearchParams();
    newParams.set('year', year);
    if (groupBy) newParams.set('groupBy', groupBy);   //< Auto: server derives from the year span
    newParams.set('limit', limit);
    newParams.set('sortBy', sortBy);
    // replaceState, not push: keep the URL shareable without stacking a history
    // entry, so Back returns to the previous page rather than past filter states.
    window.history.replaceState({}, '', window.location.pathname + '?' + newParams.toString());

    document.querySelectorAll('.wrapped-year-badge').forEach(b => b.classList.remove('active'));
    this.classList.add('active');

    loadWrappedData(year, groupBy, limit, sortBy, updateType);
  });
});

window.addEventListener('popstate', function() {
  const params = new URLSearchParams(window.location.search);
  const year = params.get('year') || String(wrappedBootstrap.year);
  const groupBy = params.get('groupBy') || '';   //< bare URL = Auto
  //< a bare URL (no ?limit) means the server default, not a hardcoded 50 that
  //  would desync the select from the data the server actually returns
  const limit = params.get('limit') || String(wrappedBootstrap.limitDefault);
  const sortBy = params.get('sortBy') || 'plays';

  document.querySelectorAll('.wrapped-year-badge').forEach(badge => {
    const badgeYear = new URL(badge.href).searchParams.get('year');
    if (badgeYear === year) {
      badge.classList.add('active');
    } else {
      badge.classList.remove('active');
    }
  });

  document.getElementById('groupBy').value = groupBy;
  document.getElementById('limit').value = limit;
  document.getElementById('sortBy').value = sortBy;

  let updateType = 'all';
  if (year === currentYear) {
    if (groupBy !== currentGroupBy && limit === currentLimit && sortBy === currentSortBy) {
      updateType = 'chart';
    } else if ((limit !== currentLimit || sortBy !== currentSortBy) && groupBy === currentGroupBy) {
      updateType = 'lists';
    }
  }

  currentYear = year;
  currentGroupBy = groupBy;
  currentLimit = limit;
  currentSortBy = sortBy;

  loadWrappedData(year, groupBy, limit, sortBy, updateType);
});

document.getElementById('exportWrappedBtn')?.addEventListener('click', function() {
  const btn = this;
  const canvas = document.createElement('canvas');
  canvas.width = 600;
  canvas.height = 900;
  const ctx = canvas.getContext('2d');

  const theme = document.documentElement.className || 'theme-rose';
  let gradStart = '#3c0b1f';
  let gradEnd = '#121212';
  let accentColor = '#FB717B';

  if (theme === 'theme-green') {
    gradStart = '#0b3c1d';
    gradEnd = '#121212';
    accentColor = '#1DB954';
  } else if (theme === 'theme-purple') {
    gradStart = '#2b1055';
    gradEnd = '#121212';
    accentColor = '#C77DFF';
  } else if (theme === 'theme-red') {
    gradStart = '#500505';
    gradEnd = '#121212';
    accentColor = '#FF4A4A';
  }

  const gradient = ctx.createLinearGradient(0, 0, 0, 900);
  gradient.addColorStop(0, gradStart);
  gradient.addColorStop(1, gradEnd);
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 600, 900);

  ctx.strokeStyle = accentColor + '44';
  ctx.lineWidth = 15;
  ctx.strokeRect(15, 15, 570, 870);

  ctx.fillStyle = '#ffffff';
  ctx.font = 'bold 36px sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText('SPOTIFY TRACKER', 300, 80);

  ctx.fillStyle = accentColor;
  ctx.font = 'bold 24px sans-serif';
  ctx.fillText(btn.dataset.year + ' WRAPPED', 300, 120);

  ctx.fillStyle = '#888888';
  ctx.font = '16px sans-serif';
  ctx.fillText('Listening Summary for @' + btn.dataset.user, 300, 150);

  const drawStat = (label, val, x, y) => {
    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 32px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(val, x, y);
    ctx.fillStyle = '#aaaaaa';
    ctx.font = '14px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(label, x, y + 25);
  };

  drawStat('Total Plays', btn.dataset.plays, 180, 220);
  drawStat('Total Time', btn.dataset.time, 420, 220);
  drawStat('Unique Songs', btn.dataset.songs, 180, 310);
  drawStat('Unique Artists', btn.dataset.artists, 420, 310);
  drawStat('Longest Streak', btn.dataset.streak + ' days', 300, 400);

  ctx.fillStyle = 'rgba(255,255,255,0.05)';
  ctx.fillRect(40, 440, 520, 380);
  ctx.strokeStyle = accentColor + '33';
  ctx.lineWidth = 2;
  ctx.strokeRect(40, 440, 520, 380);

  ctx.fillStyle = accentColor;
  ctx.font = 'bold 18px sans-serif';
  ctx.textAlign = 'left';
  ctx.fillText('TOP HIGHLIGHTS', 65, 475);

  const truncate = (str, len) => str.length > len ? str.substring(0, len) + '..' : str;

  const drawHighlight = (label, value, y) => {
    ctx.font = 'bold 17px sans-serif';
    ctx.textAlign = 'left';
    ctx.fillStyle = accentColor;
    ctx.fillText(label + ':', 65, y);

    ctx.font = '16px sans-serif';
    ctx.fillStyle = '#e0e0e0';
    ctx.fillText(truncate(value, 32), 65, y + 25);
  };

  drawHighlight('Top Song', btn.dataset.topsong, 505);
  drawHighlight('Top Artist', btn.dataset.topartist, 560);
  drawHighlight('Top Album', btn.dataset.topalbum, 615);
  drawHighlight('Peak Day', btn.dataset.peakday + ' (' + btn.dataset.peakplays + ' plays)', 670);
  drawHighlight('Discovered Songs', btn.dataset.discoveredsongs, 725);
  drawHighlight('Discovered Artists', btn.dataset.discoveredartists, 780);

  ctx.fillStyle = '#666666';
  ctx.font = 'italic 12px sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText('Generated via Spotify Stats Tracker', 300, 860);

  const dataURL = canvas.toDataURL('image/png');
  const link = document.createElement('a');
  link.download = btn.dataset.user + '_' + btn.dataset.year + '_wrapped_summary.png';
  link.href = dataURL;
  link.click();
});

document.getElementById('shareWrappedBtn')?.addEventListener('click', function() {
  const modal = document.getElementById('shareLinkModal');
  if (modal) modal.style.display = 'flex';
});

document.getElementById('shareLinkModal')?.addEventListener('click', function(e) {
  if (e.target === this) this.style.display = 'none';
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    const modal = document.getElementById('shareLinkModal');
    if (modal) modal.style.display = 'none';
  }
});

function showShareLinkError(message) {
  const panelBody = document.getElementById('shareLinkPanelBody');
  if (!panelBody) return;
  let errorEl = panelBody.querySelector('.share-link-error');
  if (!errorEl) {
    errorEl = document.createElement('p');
    errorEl.className = 'share-link-error';
    errorEl.style.cssText = 'color: #ff4a4a; font-size: 0.9rem; margin: 0 0 0.75rem;';
    panelBody.prepend(errorEl);
  }
  errorEl.textContent = message;
}

// Event-delegated: the create/revoke forms below are replaced wholesale
// (innerHTML) after each AJAX round-trip, so a directly bound listener
// would be lost on the very first submit.
document.getElementById('shareLinkModal')?.addEventListener('submit', function(e) {
  const form = e.target;
  if (!form.matches('.share-link-create-form, .share-link-revoke-form')) return;
  e.preventDefault();

  const panelBody = document.getElementById('shareLinkPanelBody');
  const url = new URL(form.action, window.location.href);
  url.searchParams.set('ajax', 'true');

  fetch(url, { method: 'POST', body: new FormData(form) })
    .then(response => response.json())
    .then(data => {
      if (data.html !== undefined) {
        panelBody.innerHTML = data.html;
      } else {
        showShareLinkError(data.error || 'Something went wrong. Please try again.');
      }
    })
    .catch(() => showShareLinkError('Something went wrong. Please try again.'));
});
