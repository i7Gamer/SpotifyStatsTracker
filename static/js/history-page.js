// The /history page: time-filter controls, the debounced search box and the
// two-phase AJAX list load.
//
// Extracted from history.html so it can be linted. The one Jinja value it
// needs (the user's default time window) stays behind in the template as a
// data island declared BEFORE this file loads - the same split compare.html
// and wrapped.html already use.
// Filter behavior for the history list - the interval/custom-range/search
// reload logic the dashboard used to own, now that the list lives here.
// Every change fetches the list + pagination strip via ?ajax=true and
// fades the result into #historyResults in place (same pattern as
// compare.html/genres.html), instead of a full page reload.
var HISTORY_FADE_MS = 200;
var activeHistoryLoad = null;

function loadHistoryResults(opts) {
  opts = opts || {};
  var initial = !!opts.initial;
  var target = document.getElementById('historyResults');
  if (activeHistoryLoad) {
    activeHistoryLoad.controller.abort();
    target.classList.remove('loading-fade');
  }
  var controller = new AbortController();
  activeHistoryLoad = { controller: controller };
  //< the initial load has a bare "Loading…" placeholder, not stale
  //  content - no fade-out needed, only the fade-in once real content lands
  if (!initial) target.classList.add('loading-fade');

  var params = new URLSearchParams(window.location.search);
  params.set('ajax', 'true');
  var delay = new Promise(function (resolve) { setTimeout(resolve, initial ? 0 : HISTORY_FADE_MS); });
  var fetched = fetch(window.location.pathname + '?' + params.toString(), { signal: controller.signal })
    .then(function (response) {
      return window.AjaxStatus.readJsonOrThrow(response, 'history');
    });

  Promise.all([fetched, delay])
    .then(function (results) {
      var data = results[0];
      //< a response that resolved before its abort can still reach here -
      //  never swap stale data in over a newer load's
      if (!activeHistoryLoad || activeHistoryLoad.controller !== controller) {
        return;
      }
      target.innerHTML = data.resultsHtml;

      // smooth cover-art fade-ins for the freshly injected cards
      document.querySelectorAll('#historyResults img.track-cover').forEach(function (img) {
        if (img.complete) {
          img.classList.add('loaded');
        } else {
          img.addEventListener('load', function () { img.classList.add('loaded'); });
        }
      });
    })
    .catch(function (err) {
      //< navigating to /login - not a load failure to report
      if (window.AjaxStatus && window.AjaxStatus.isUnauthorizedError(err)) return;
      if (err.name !== 'AbortError') {
        console.error(err);
        //< genuine failure (not superseded): show an inline error + Retry
        //  in place of the stuck "Loading…" / stale list
        if ((!activeHistoryLoad || activeHistoryLoad.controller === controller) && window.AjaxStatus) {
          window.AjaxStatus.renderInto(target, function () { loadHistoryResults(); });
        }
      }
    })
    .finally(function () {
      if (activeHistoryLoad && activeHistoryLoad.controller === controller) {
        activeHistoryLoad = null;
        target.classList.remove('loading-fade');
      }
    });
}

// Pure helper (unit-tested in tests/test_history_page.js): which query params
// a tag filter change sets vs deletes. Mirrors applyTopListTagParam in
// static/js/top-list.js - the Top pages' tag dropdown this one was copied from.
function applyHistoryTagParam(params, tag) {
  if (tag) { params.set('tag', tag); } else { params.delete('tag'); }
  params.delete('page');
  return params;
}

// Update the URL in place (replaceState, not push) so a filter/sort/page
// change stays shareable/refreshable without stacking a history entry - Back
// then returns to the previous page instead of stepping back through it.
function replaceHistoryUrl(mutate) {
  var params = new URLSearchParams(window.location.search);
  mutate(params);
  params.delete('ajax');
  var query = params.toString();
  window.history.replaceState({}, '', window.location.pathname + (query ? '?' + query : ''));
}

function updateHistoryInterval() {
  const interval = document.getElementById('interval').value;
  const customDates = document.getElementById('historyCustomDates');

  if (interval === 'custom') {
    customDates.style.display = 'flex';
    return;
  }

  customDates.style.display = 'none';
  updateHistoryFilters();
}

function updateHistoryDateFilter() {
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
    updateHistoryFilters(true);
  }
}

function updateHistoryFilters(forceCustom = false) {
  const interval = document.getElementById('interval').value;
  const searchInput = document.getElementById('historySearch');
  const searchQuery = searchInput ? searchInput.value : '';

  let startDate, endDate;
  if (interval === 'custom' || forceCustom) {
    startDate = document.getElementById('startDate').value;
    endDate = document.getElementById('endDate').value;

    if (!startDate || !endDate) {
      return;
    }
  }

  replaceHistoryUrl(function (params) {
    if (searchQuery.trim()) {
      params.set('q', searchQuery);
    } else {
      params.delete('q');
    }

    if (interval === 'custom' || forceCustom) {
      params.set('interval', 'custom');
      params.set('startDate', startDate);
      params.set('endDate', endDate);
    } else {
      params.set('interval', interval);
      params.delete('startDate');
      params.delete('endDate');
    }

    params.delete('page');
  });

  loadHistoryResults();
}

// The Date sort toggle: flips newest-first (default) <-> oldest-first,
// resets to page 1 (page N of one order isn't page N of the other), and
// refreshes the list in place like every other filter change.
function updateHistorySort() {
  var params = new URLSearchParams(window.location.search);
  var next = params.get('sort') === 'oldest' ? 'newest' : 'oldest';
  replaceHistoryUrl(function (p) {
    if (next === 'oldest') { p.set('sort', 'oldest'); } else { p.delete('sort'); }
    p.delete('page');
  });
  updateHistorySortLabel(next);
  loadHistoryResults();
}

function updateHistorySortLabel(sort) {
  document.getElementById('historySort').textContent = sort === 'oldest' ? 'Date ↑' : 'Date ↓';
}

// The Tag filter (only rendered when the user has tagged something - see
// history.html's {% if tags_enabled and user_tags %} guard), same
// replace-and-reload pattern as every other filter here.
function updateHistoryTagFilter() {
  var tagFilter = document.getElementById('tagFilter');
  if (!tagFilter) return;
  replaceHistoryUrl(function (params) { applyHistoryTagParam(params, tagFilter.value); });
  loadHistoryResults();
}

// Pagination: Previous/Next + page-number links, and the "Go to page"
// input (_pagination.html) - all navigate within #historyResults via
// ajax instead of a full reload.
function goToHistoryPage(page) {
  replaceHistoryUrl(function (params) { params.set('page', page); });
  loadHistoryResults();
}

// _pagination.html's jump-to-page input calls the shared
// handleJumpToPageKeydown (layout.html), which defers to this hook when
// present instead of navigating - see layout.html for the fallback.
//
// Everything below only runs in a browser (guarded on `document`, like
// static/js/top-list.js's init) so this module can still be `require()`d from
// a plain-node unit test (tests/test_history_page.js) for its pure helpers.
if (typeof document !== 'undefined') {
  window.__paginationAjaxHandler = goToHistoryPage;

  // The results container persists across ajax swaps (only its innerHTML is
  // replaced), so this delegated listener survives every refresh.
  document.getElementById('historyResults').addEventListener('click', function (evt) {
    const link = evt.target.closest('.pagination-controls a');
    if (!link) return;
    // Let modified clicks (new tab, etc.) behave normally.
    if (evt.metaKey || evt.ctrlKey || evt.shiftKey || evt.altKey) return;
    evt.preventDefault();
    const page = new URL(link.href).searchParams.get('page') || '1';
    goToHistoryPage(page);
  });

  // Back/forward: reconcile the filter controls with the URL, then re-fetch.
  window.addEventListener('popstate', function () {
    const params = new URLSearchParams(window.location.search);
    const hasCustom = params.get('startDate') && params.get('endDate');
    const interval = hasCustom ? 'custom' : (params.get('interval') || HISTORY_DEFAULT_WINDOW);
    document.getElementById('interval').value = interval;
    document.getElementById('startDate').value = params.get('startDate') || '';
    document.getElementById('endDate').value = params.get('endDate') || '';
    document.getElementById('historyCustomDates').style.display = hasCustom ? 'flex' : 'none';
    const searchInput = document.getElementById('historySearch');
    if (searchInput) searchInput.value = params.get('q') || '';
    const tagFilter = document.getElementById('tagFilter');
    if (tagFilter) tagFilter.value = params.get('tag') || '';
    updateHistorySortLabel(params.get('sort') === 'oldest' ? 'oldest' : 'newest');
    loadHistoryResults();
  });

  loadHistoryResults({ initial: true });
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { applyHistoryTagParam };
}
