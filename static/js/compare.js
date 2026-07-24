// The Compare page's client logic, extracted from the inline <script> in
// templates/compare.html so it is a static, cacheable asset instead of
// template-embedded JS.
//
// Server-rendered comparisonTrend + the default limit arrive via the JSON
// <script type="application/json" id="compare-bootstrap"> data island in
// compare.html; everything below is verbatim from the former inline script.
// This file is loaded BEFORE charts.js, which reads window.__chartData set here.
//
// Top-level names stay global on purpose: the inline
// onchange="updateCompare*Filter()" handlers in compare.html depend on them.
const compareBootstrap = JSON.parse(document.getElementById('compare-bootstrap').textContent);
window.__chartData = window.__chartData || {};
// The shell renders before any data query runs (see compare.py) - charts.js
// would otherwise draw the mirror chart immediately off empty
// window.__chartData. loadCompareData's initial fetch below sets
// window.__chartData.comparisonTrend and calls renderComparisonMirror
// itself once the real data lands.
window.__deferInitialChartRender = true;

// ---- AJAX filter updates: interval/date/user switches swap the dynamic
// regions in place (same fade-and-swap pattern as the Wrapped page)
// instead of a full page reload. ----
const COMPARE_FADE_MS = 200;

// The six individual my/their Top lists are the ONLY regions whose
// content reads ?sortBy= (see app.py's compare route comment) - the
// stats table, chart, taste match, similarities, genres and Top Common
// lists render byte-identical under any sortBy, so a sort change fades
// and swaps just these and leaves the rest untouched. One map drives
// BOTH the fade targets and the innerHTML swap, so a list can't end up
// faded-but-not-swapped (or vice versa).
const SORT_BY_LIST_SWAPS = {
  myTopSongsList: 'myTopSongsHtml',
  theirTopSongsList: 'theirTopSongsHtml',
  myTopArtistsList: 'myTopArtistsHtml',
  theirTopArtistsList: 'theirTopArtistsHtml',
  myTopAlbumsList: 'myTopAlbumsHtml',
  theirTopAlbumsList: 'theirTopAlbumsHtml',
};

function compareSwapTargets(sortByOnly = false) {
  const sortByLists = Object.keys(SORT_BY_LIST_SWAPS).map(id => document.getElementById(id));
  if (sortByOnly) {
    return sortByLists.filter(Boolean);
  }
  return [
    document.getElementById('compareStatsTable'),
    document.querySelector('.chart-canvas-wrap'),
    document.getElementById('compareSimilarities'),
    document.getElementById('compareGenres'),
    document.getElementById('sharedArtistsList'),
    document.getElementById('sharedSongsList'),
    document.getElementById('sharedAlbumsList'),
    ...sortByLists,
    ...document.querySelectorAll('[data-category] .track-list'),
  ].filter(Boolean);
}

//< the in-flight filter fetch ({controller, targets}) - a newer filter
//  change aborts it so a slow older response can't land after (and
//  clobber) the newer one's swap
let activeCompareLoad = null;

function loadCompareData({ sortByOnly = false, initial = false } = {}) {
  if (activeCompareLoad) {
    activeCompareLoad.controller.abort();
    //< the aborted load skips its own cleanup (see the finally guard) -
    //  un-fade its targets here so sections this load doesn't also fade
    //  don't stay stuck mid-fade
    activeCompareLoad.targets.forEach(t => t.classList.remove('loading-fade'));
  }
  const controller = new AbortController();
  //< the initial load has "Loading…" placeholders, not stale content - no
  //  fade-out needed, only the fade-in once real content swaps in
  const targets = initial ? [] : compareSwapTargets(sortByOnly);
  activeCompareLoad = { controller, targets };
  targets.forEach(t => t.classList.add('loading-fade'));

  const params = new URLSearchParams(window.location.search);
  params.set('ajax', 'true');
  if (sortByOnly) {
    //< the server then skips the shared/taste/genre/trend work the six
    //  lists don't need - see the compare route's scope=sortable branch
    params.set('scope', 'sortable');
  }
  const delay = new Promise(resolve => setTimeout(resolve, COMPARE_FADE_MS));
  const fetched = fetch(window.location.pathname + '?' + params.toString(), { signal: controller.signal })
    .then(response => response.json());

  Promise.all([fetched, delay])
    .then(([data]) => {
      //< a response that resolved before its abort can still reach here
      //  (abort() can't recall a settled promise, and the fade delay may
      //  be what it was waiting on) - never swap stale data in over a
      //  newer load's
      if (!activeCompareLoad || activeCompareLoad.controller !== controller) {
        return;
      }
      Object.entries(SORT_BY_LIST_SWAPS).forEach(([id, dataKey]) => {
        document.getElementById(id).innerHTML = data[dataKey];
      });

      if (!sortByOnly) {
        document.getElementById('compareStatsTable').innerHTML = data.statsTableHtml;
        document.getElementById('compareSimilarities').innerHTML = data.similaritiesHtml;
        document.getElementById('compareGenres').innerHTML = data.genresHtml;
        document.getElementById('sharedArtistsList').innerHTML = data.sharedArtistsHtml;
        document.getElementById('sharedSongsList').innerHTML = data.sharedSongsHtml;
        document.getElementById('sharedAlbumsList').innerHTML = data.sharedAlbumsHtml;

        document.querySelectorAll('.js-with-username').forEach(el => el.textContent = data.withUsername);

        const tasteMatchEl = document.getElementById('tasteMatch');
        if (tasteMatchEl) {
          tasteMatchEl.style.display = data.tasteMatch === null ? 'none' : '';
          if (data.tasteMatch !== null) {
            tasteMatchEl.querySelector('.js-taste-match').textContent = data.tasteMatch + '%';
          }
        }

        window.__chartData.comparisonTrend = data.comparisonTrend;
        if (window.renderComparisonMirror) {
          window.renderComparisonMirror();
        }
      }

      // smooth cover-art fade-ins for the freshly injected cards
      document.querySelectorAll('img.track-cover').forEach(img => {
        if (img.complete) {
          img.classList.add('loaded');
        } else {
          img.addEventListener('load', () => img.classList.add('loaded'));
        }
      });

      if (window.AjaxStatus) window.AjaxStatus.clearBanner();
    })
    .catch(err => {
      //< an abort is the expected fate of a superseded load, not an error
      if (err.name !== 'AbortError') {
        console.error(err);
        //< genuine failure (not superseded): the shell has many swap targets,
        //  so surface a page-level banner with Retry
        if ((!activeCompareLoad || activeCompareLoad.controller === controller) && window.AjaxStatus) {
          window.AjaxStatus.showBanner(() => { loadCompareData(); });
        }
      }
    })
    .finally(() => {
      //< only the still-current load cleans up - a superseded one's fades
      //  were already cleared by its successor when it aborted it
      if (activeCompareLoad && activeCompareLoad.controller === controller) {
        activeCompareLoad = null;
        targets.forEach(t => t.classList.remove('loading-fade'));
      }
    });
}

// Update the URL in place (replaceState, not push) so a filter change stays
// shareable/refreshable without stacking a history entry - Back then returns
// to the page the user came from instead of stepping back through past filters.
function replaceCompareUrl(mutate) {
  const params = new URLSearchParams(window.location.search);
  mutate(params);
  params.delete('ajax');
  const query = params.toString();
  window.history.replaceState({}, '', window.location.pathname + (query ? '?' + query : ''));
}

function updateCompareIntervalFilter() {
  const interval = document.getElementById('interval').value;
  const customDates = document.getElementById('compareCustomDates');

  if (interval === 'custom') {
    customDates.style.display = 'flex';
    return;
  }

  customDates.style.display = 'none';
  replaceCompareUrl(params => {
    params.set('interval', interval);
    params.delete('startDate');
    params.delete('endDate');
  });
  loadCompareData();
}

function updateCompareDateFilter() {
  const startDate = document.getElementById('startDate').value;
  const endDate = document.getElementById('endDate').value;
  if (!startDate || !endDate) {
    return;
  }

  replaceCompareUrl(params => {
    params.set('interval', 'custom');
    params.set('startDate', startDate);
    params.set('endDate', endDate);
  });
  loadCompareData();
}

function updateCompareLimitFilter() {
  const limit = document.getElementById('limit').value;
  replaceCompareUrl(params => params.set('limit', limit));
  loadCompareData();
}

function updateCompareGroupByFilter() {
  const groupBy = document.getElementById('groupBy').value;
  replaceCompareUrl(params => {
    if (groupBy) {
      params.set('groupBy', groupBy);
    } else {
      params.delete('groupBy');   //< Auto: let the server pick from the range span
    }
  });
  loadCompareData();
}

function updateCompareSortByFilter() {
  const sortBy = document.getElementById('sortBy').value;
  replaceCompareUrl(params => params.set('sortBy', sortBy));
  loadCompareData({ sortByOnly: true });
}

document.querySelectorAll('.wrapped-year-badge').forEach(badge => {
  badge.addEventListener('click', function (e) {
    e.preventDefault();
    const withUser = new URL(this.href).searchParams.get('with');
    document.querySelectorAll('.wrapped-year-badge').forEach(b => b.classList.remove('active'));
    this.classList.add('active');
    replaceCompareUrl(params => params.set('with', withUser));
    loadCompareData();
  });
});

window.addEventListener('popstate', function () {
  const params = new URLSearchParams(window.location.search);
  const interval = params.get('interval') || '';
  document.getElementById('interval').value = (params.get('startDate') && params.get('endDate')) ? 'custom' : interval;
  document.getElementById('startDate').value = params.get('startDate') || '';
  document.getElementById('endDate').value = params.get('endDate') || '';
  document.getElementById('compareCustomDates').style.display =
    (params.get('startDate') && params.get('endDate')) ? 'flex' : 'none';
  document.getElementById('limit').value = params.get('limit') || String(compareBootstrap.limitDefault);
  document.getElementById('groupBy').value = params.get('groupBy') || '';
  document.getElementById('sortBy').value = params.get('sortBy') || 'plays';
  const withUser = params.get('with');
  document.querySelectorAll('.wrapped-year-badge').forEach(badge => {
    const badgeUser = new URL(badge.href).searchParams.get('with');
    badge.classList.toggle('active', withUser !== null && badgeUser === withUser);
  });
  loadCompareData();
});

// The shell (see compare.py) renders no data - fetch it now, the same way
// every filter change already does, just fired once more up front.
loadCompareData({ initial: true });

// ---- Category filter badges - same show/hide + staggered fade-in pattern
// as the Wrapped page ([data-category].visible in style.css). ----
const compareFilterButtons = document.querySelectorAll('.stats-filter-button');
const compareCategoryDivs = document.querySelectorAll('[data-category]');

compareFilterButtons.forEach(button => {
  button.addEventListener('click', () => {
    const filter = button.dataset.filter;

    compareFilterButtons.forEach(btn => btn.classList.remove('active'));
    button.classList.add('active');

    compareCategoryDivs.forEach(div => {
      if (filter === 'all' || div.dataset.category === filter) {
        div.classList.add('visible');
      } else {
        div.classList.remove('visible');
      }
    });
  });
});

const compareAllButton = document.querySelector('.stats-filter-button[data-filter="all"]');
if (compareAllButton) {
  compareAllButton.click();
}
