/* Two-phase AJAX loader + filter handlers for the Top Songs/Artists/Albums
 * pages (templates/top_*.html + _page_card.html). The shell renders the filter
 * controls and an empty #topListResults placeholder; this fetches the stat
 * header + list + pagination via ?ajax=true on first paint and on every
 * filter/sort/tag/page change, swapping it in place instead of a full reload
 * (the same pattern history.html / charts-page.js use). The filter functions
 * were inline in _page_card.html and did window.location full navigations. */

// --- pure helpers -----------------------------------------------------------
// Kept at module scope, DOM-free and exported, so the branchy parts (which
// query params a filter change sets vs deletes, and whether a settled response
// still belongs to the newest load) are unit-testable in plain node like the
// sibling modules' logic is. The DOM wiring below reads the controls and hands
// their values here.

// True when a response that just settled still belongs to the newest load. A
// fetch aborted mid-flight can still resolve, so without this a superseded
// response could swap stale rows in over fresher ones.
function isCurrentTopListLoad(activeLoad, controller) {
  return !!activeLoad && activeLoad.controller === controller;
}

// `state`: {interval, sortBy, searchQuery, startDate, endDate, forceCustom}
function applyTopListFilterParams(params, state) {
  var useCustom = state.interval === 'custom' || state.forceCustom;
  if (state.searchQuery && state.searchQuery.trim()) {
    params.set('q', state.searchQuery);
  } else {
    params.delete('q');
  }
  params.set('sortBy', state.sortBy);
  if (useCustom) {
    params.set('interval', 'custom');
    params.set('startDate', state.startDate);
    params.set('endDate', state.endDate);
  } else if (state.interval) {
    params.set('interval', state.interval);
    params.delete('startDate');
    params.delete('endDate');
  } else {
    params.delete('interval');   //< empty value = All Time
    params.delete('startDate');
    params.delete('endDate');
  }
  params.delete('page');         //< any filter change returns to page 1
  return params;
}

function applyTopListTagParam(params, tag) {
  if (tag) { params.set('tag', tag); } else { params.delete('tag'); }
  params.delete('page');
  return params;
}

function applyTopListFullPlaysParam(params, fullPlaysOnly) {
  params.set('fullOnly', fullPlaysOnly ? '1' : '0');
  params.delete('page');
  return params;
}

if (typeof window !== 'undefined') (function () {
  var FADE_MS = 200;
  var activeLoad = null;

  function target() { return document.getElementById('topListResults'); }

  function loadTopList(opts) {
    opts = opts || {};
    var initial = !!opts.initial;
    var el = target();
    if (!el) return;
    if (activeLoad) {
      activeLoad.controller.abort();
      el.classList.remove('loading-fade');
    }
    var controller = new AbortController();
    activeLoad = { controller: controller };
    if (!initial) el.classList.add('loading-fade');

    var params = new URLSearchParams(window.location.search);
    params.set('ajax', 'true');
    var delay = new Promise(function (resolve) { setTimeout(resolve, initial ? 0 : FADE_MS); });
    var fetched = fetch(window.location.pathname + '?' + params.toString(), { signal: controller.signal })
      .then(function (r) { return r.json(); });

    Promise.all([fetched, delay]).then(function (results) {
      var data = results[0];
      //< a response that resolved before its abort can still land here - never
      //  swap stale data in over a newer load
      if (!isCurrentTopListLoad(activeLoad, controller)) return;
      el.innerHTML = data.resultsHtml;
      el.querySelectorAll('img.track-cover').forEach(function (img) {
        if (img.complete) img.classList.add('loaded');
        else img.addEventListener('load', function () { img.classList.add('loaded'); });
      });
    }).catch(function (err) {
      if (err.name !== 'AbortError') {
        console.error(err);
        if ((!activeLoad || activeLoad.controller === controller) && window.AjaxStatus) {
          window.AjaxStatus.renderInto(el, function () { loadTopList(); });
        }
      }
    }).finally(function () {
      if (activeLoad && activeLoad.controller === controller) {
        activeLoad = null;
        el.classList.remove('loading-fade');
      }
    });
  }
  window.loadTopList = loadTopList;

  // Update the URL in place (replaceState, not push) so a filter/page change
  // stays shareable/refreshable without stacking history entries - Back then
  // leaves the page instead of stepping through each filter change.
  function replaceTopListUrl(mutate) {
    var params = new URLSearchParams(window.location.search);
    mutate(params);
    params.delete('ajax');
    var query = params.toString();
    window.history.replaceState({}, '', window.location.pathname + (query ? '?' + query : ''));
  }

  function currentSearch() {
    var el = document.getElementById('searchQuery');
    return el ? el.value : '';
  }

  function applyFilters(forceCustom) {
    var interval = document.getElementById('interval').value;
    var sortBy = document.getElementById('sortBy').value;
    var searchQuery = currentSearch();
    var startDate, endDate;
    if (interval === 'custom' || forceCustom) {
      startDate = document.getElementById('startDate').value;
      endDate = document.getElementById('endDate').value;
      if (!startDate || !endDate) return;
    }
    replaceTopListUrl(function (params) {
      applyTopListFilterParams(params, {
        interval: interval, sortBy: sortBy, searchQuery: searchQuery,
        startDate: startDate, endDate: endDate, forceCustom: !!forceCustom,
      });
    });
    loadTopList();
  }

  // --- handlers referenced by _page_card.html's controls ---
  window.updateIntervalFilter = function () {
    var interval = document.getElementById('interval').value;
    var customDates = document.getElementById('customDates');
    if (interval === 'custom') { customDates.style.display = 'flex'; return; }
    customDates.style.display = 'none';
    applyFilters();
  };

  window.updateDateFilter = function () {
    var startEl = document.getElementById('startDate');
    var endEl = document.getElementById('endDate');
    var errorEl = document.getElementById('dateError');
    var startDate = startEl.value, endDate = endEl.value;
    if (errorEl) errorEl.style.display = 'none';
    startEl.style.borderColor = '';
    endEl.style.borderColor = '';
    if (startDate && endDate && new Date(startDate) > new Date(endDate)) {
      if (errorEl) { errorEl.textContent = 'Start date cannot be after end date.'; errorEl.style.display = 'inline'; }
      startEl.style.borderColor = 'var(--accent)';
      endEl.style.borderColor = 'var(--accent)';
      return;
    }
    if (startDate && endDate) applyFilters(true);
  };

  window.updateSearchFilter = function () { applyFilters(); };
  window.updateFilters = function () { applyFilters(); };

  window.updateTagFilter = function () {
    var tagFilter = document.getElementById('tagFilter');
    replaceTopListUrl(function (params) { applyTopListTagParam(params, tagFilter.value); });
    loadTopList();
  };

  window.updateFullPlaysFilter = function () {
    var cb = document.getElementById('fullPlaysOnly');
    replaceTopListUrl(function (params) { applyTopListFullPlaysParam(params, cb.checked); });
    loadTopList();
  };

  // --- pagination (the results container persists across swaps, so this
  //     delegated listener survives every refresh) ---
  function goToTopListPage(page) {
    replaceTopListUrl(function (params) { params.set('page', page); });
    loadTopList();
  }
  window.__paginationAjaxHandler = goToTopListPage;

  function init() {
    var el = target();
    if (!el) return;
    el.addEventListener('click', function (evt) {
      var link = evt.target.closest('.pagination-controls a');
      if (!link) return;
      if (evt.metaKey || evt.ctrlKey || evt.shiftKey || evt.altKey) return;   //< let new-tab clicks pass
      evt.preventDefault();
      var page = new URL(link.href).searchParams.get('page') || '1';
      goToTopListPage(page);
    });
    loadTopList({ initial: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    isCurrentTopListLoad,
    applyTopListFilterParams,
    applyTopListTagParam,
    applyTopListFullPlaysParam,
  };
}
