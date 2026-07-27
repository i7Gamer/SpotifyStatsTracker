// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* The song/artist/album detail pages' play-history list: the Top Songs /
 * History tab toggle (artist/album only - same click-to-toggle-visibility
 * pattern as wrapped.js's [data-category].visible) and in-place AJAX
 * navigation for the list's sort toggle + pagination links, via the routes'
 * ?ajax=list branch (mirrors history.html's loadHistoryResults: abort
 * superseded loads, loading-fade, swap resultsHtml). All tab content is
 * server-rendered on every full load, so tab clicks only flip classes and
 * keep ?view= in sync via replaceState (see activateView). All in-page URL
 * updates replaceState rather than push, so Back returns to the previous page
 * instead of stepping back through sort/page/tab states.
 *
 * The markup this drives arrives with the page's deferred body, so the wiring
 * lives in initDetailHistory() and detail-page.js calls it once the body is
 * swapped in - at script-load time none of these elements exist yet. */
(function () {
  var DETAIL_HISTORY_FADE_MS = 200;
  var SHOW_MORE_BATCH_SIZE = 50;
  var SHOW_MORE_LABEL = 'Show More Plays (' + SHOW_MORE_BATCH_SIZE + ')';

  function formatShowMoreLabel(batchSize) {
    var size = parseInt(batchSize, 10);
    if (isNaN(size) || size <= 0) {
      size = SHOW_MORE_BATCH_SIZE;
    }
    return 'Show More Plays (' + size + ')';
  }

  //< re-resolved by initDetailHistory on every body swap; empty until then
  var filterButtons = [];
  var categoryDivs = [];

  function activateView(view, pushUrl) {
    filterButtons.forEach(function (btn) { btn.classList.toggle('active', btn.dataset.filter === view); });
    categoryDivs.forEach(function (div) { div.classList.toggle('visible', div.dataset.category === view); });
    if (pushUrl) {
      var params = new URLSearchParams(window.location.search);
      if (view === 'top-songs') { params.delete('view'); } else { params.set('view', view); }
      var query = params.toString();
      window.history.replaceState({}, '', window.location.pathname + (query ? '?' + query : ''));
    }
  }

  var container = null;

  //< the in-flight fetch ({controller}) - a newer sort/page change aborts it
  //  so a slow older response can't land after (and clobber) the newer one
  var activeLoad = null;

  //< bumped every time the list is re-rendered from scratch. A "Show more"
  //  request in flight when a sort/filter change swaps the list would
  //  otherwise append its (now stale) rows into the fresh list and strip the
  //  new list's own Show-more control.
  var listGeneration = 0;

  function loadDetailHistory() {
    if (!container) {
      return;
    }
    if (activeLoad) {
      activeLoad.controller.abort();
      container.classList.remove('loading-fade');
    }
    var controller = new AbortController();
    activeLoad = { controller: controller };
    container.classList.add('loading-fade');

    var params = new URLSearchParams(window.location.search);
    params.set('ajax', 'list');
    var delay = new Promise(function (resolve) { setTimeout(resolve, DETAIL_HISTORY_FADE_MS); });
    var fetched = fetch(window.location.pathname + '?' + params.toString(), {
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      signal: controller.signal
    }).then(function (resp) {
      //< onContainerClick replaceStates the URL BEFORE fetching, so a swallowed
      //  non-2xx would leave the old list under a URL claiming the new sort
      return window.AjaxStatus.readJsonOrThrow(resp, 'detail history');
    });

    Promise.all([fetched, delay])
      .then(function (results) {
        //< a response that settled before its abort can still reach here;
        //  never swap stale data in over a newer load's
        if (!activeLoad || activeLoad.controller !== controller) {
          return;
        }
        container.innerHTML = results[0].resultsHtml;
        listGeneration += 1;
        if (window.AjaxStatus) window.AjaxStatus.clearBanner();
      })
      .catch(function (err) {
        //< navigating to /login - not a load failure to report
        if (window.AjaxStatus && window.AjaxStatus.isUnauthorizedError(err)) return;
        if (err.name !== 'AbortError') {
          console.error(err);
          //< a banner, not renderInto: the list still holds the previous page,
          //  which is worth keeping on screen behind the error
          if ((!activeLoad || activeLoad.controller === controller) && window.AjaxStatus) {
            window.AjaxStatus.showBanner(function () { loadDetailHistory(); });
          }
        }
      })
      .finally(function () {
        if (activeLoad && activeLoad.controller === controller) {
          activeLoad = null;
          container.classList.remove('loading-fade');
        }
      });
  }

  // Delegated click handler covering pagination links, sort toggle, skips
  // toggle and "Show more" - bound to the results container, which survives
  // every in-place swap of its own innerHTML.
  function onContainerClick(evt) {
      var link = evt.target.closest('.pagination-controls a, a.sort-toggle, a.skips-toggle');
      if (link) {
        if (evt.metaKey || evt.ctrlKey || evt.shiftKey || evt.altKey) return;
        evt.preventDefault();
        var url = new URL(link.href);
        window.history.replaceState({}, '', url.pathname + url.search);
        loadDetailHistory();
        return;
      }

      var showMoreBtn = evt.target.closest('#showMorePlaysBtn, .show-more-btn');
      if (showMoreBtn) {
        evt.preventDefault();
        var offset = showMoreBtn.dataset.offset;
        if (!offset) return;

        var currentBatchSize = showMoreBtn.dataset.nextBatchSize || SHOW_MORE_BATCH_SIZE;
        var generation = listGeneration;   //< the list these rows belong to

        showMoreBtn.disabled = true;
        showMoreBtn.textContent = 'Loading...';

        var params = new URLSearchParams(window.location.search);
        params.set('ajax', 'list');
        params.set('offset', offset);

        fetch(window.location.pathname + '?' + params.toString(), {
          headers: { 'X-Requested-With': 'XMLHttpRequest' }
        })
          .then(function (resp) {
            //< a swallowed non-2xx just re-enabled the button, so the click was
            //  lost with nothing said about why no rows arrived
            return window.AjaxStatus.readJsonOrThrow(resp, 'show more');
          })
          .then(function (data) {
            //< the list was re-rendered while this was in flight: these rows
            //  belong to a filter/sort that is no longer on screen
            if (generation !== listGeneration) return;
            if (!data || !data.resultsHtml) {
              showMoreBtn.disabled = false;
              showMoreBtn.textContent = formatShowMoreLabel(currentBatchSize);
              return;
            }

            var tempDiv = document.createElement('div');
            tempDiv.innerHTML = data.resultsHtml;

            var newItems = tempDiv.querySelectorAll('#timelineItems > *');
            var targetList = container.querySelector('#timelineItems');

            if (targetList && newItems.length) {
              newItems.forEach(function (child) {
                targetList.appendChild(child);
              });
            }

            var actionsDiv = container.querySelector('.timeline-actions');
            if (data.hasMore && data.nextOffset) {
              var nextSize = data.nextBatchSize || currentBatchSize;
              showMoreBtn.disabled = false;
              showMoreBtn.dataset.offset = data.nextOffset;
              showMoreBtn.dataset.nextBatchSize = nextSize;
              showMoreBtn.textContent = formatShowMoreLabel(nextSize);
            } else if (actionsDiv) {
              actionsDiv.remove();
            }
          })
          .catch(function (err) {
            if (window.AjaxStatus && window.AjaxStatus.isUnauthorizedError(err)) return;
            console.error(err);
            if (generation !== listGeneration) return;
            showMoreBtn.disabled = false;
            showMoreBtn.textContent = formatShowMoreLabel(currentBatchSize);
            //< the re-enabled button alone reads as "there was nothing more to
            //  load"; say that the request failed
            if (window.AjaxStatus) window.AjaxStatus.showBanner(function () { showMoreBtn.click(); });
          });
      }
  }

  // Called by detail-page.js after each body swap. Every element referenced
  // here is replaced wholesale by that swap, so re-resolving them is also what
  // keeps the listeners from stacking - the nodes they were bound to are gone.
  function initDetailHistory() {
    filterButtons = document.querySelectorAll('.stats-filter-button');
    categoryDivs = document.querySelectorAll('[data-category]');
    filterButtons.forEach(function (button) {
      button.addEventListener('click', function () { activateView(button.dataset.filter, true); });
    });

    container = document.getElementById('detailHistoryResults');
    if (container) {
      container.addEventListener('click', onContainerClick);
    }
  }

  window.addEventListener('popstate', function () {
    if (filterButtons.length) {
      var params = new URLSearchParams(window.location.search);
      activateView(params.get('view') === 'history' ? 'history' : 'top-songs', false);
    }
    loadDetailHistory();
  });

  if (typeof window !== 'undefined') {
    window.initDetailHistory = initDetailHistory;
  }
})();

