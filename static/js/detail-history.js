/* The song/artist/album detail pages' play-history list: the Top Songs /
 * History tab toggle (artist/album only - same click-to-toggle-visibility
 * pattern as wrapped.js's [data-category].visible) and in-place AJAX
 * navigation for the list's sort toggle + pagination links, via the routes'
 * ?ajax=list branch (mirrors history.html's loadHistoryResults: abort
 * superseded loads, loading-fade, swap resultsHtml). All tab content is
 * server-rendered on every full load, so tab clicks only flip classes and
 * keep ?view= in sync via replaceState (see activateView). All in-page URL
 * updates replaceState rather than push, so Back returns to the previous page
 * instead of stepping back through sort/page/tab states. */
(function () {
  var DETAIL_HISTORY_FADE_MS = 200;

  var filterButtons = document.querySelectorAll('.stats-filter-button');
  var categoryDivs = document.querySelectorAll('[data-category]');

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

  filterButtons.forEach(function (button) {
    button.addEventListener('click', function () { activateView(button.dataset.filter, true); });
  });

  var container = document.getElementById('detailHistoryResults');

  //< the in-flight fetch ({controller}) - a newer sort/page change aborts it
  //  so a slow older response can't land after (and clobber) the newer one
  var activeLoad = null;

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
    }).then(function (resp) { return resp.ok ? resp.json() : null; });

    Promise.all([fetched, delay])
      .then(function (results) {
        //< a response that settled before its abort can still reach here;
        //  never swap stale data in over a newer load's
        if (!activeLoad || activeLoad.controller !== controller) {
          return;
        }
        if (results[0]) {
          container.innerHTML = results[0].resultsHtml;
        }
      })
      .catch(function (err) {
        if (err.name !== 'AbortError') {
          console.error(err);
        }
      })
      .finally(function () {
        if (activeLoad && activeLoad.controller === controller) {
          activeLoad = null;
          container.classList.remove('loading-fade');
        }
      });
  }

  if (container) {
    // Delegated click listener covering pagination links, sort toggle, and skips toggle.
    container.addEventListener('click', function (evt) {
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

        showMoreBtn.disabled = true;
        showMoreBtn.textContent = 'Loading...';

        var params = new URLSearchParams(window.location.search);
        params.set('ajax', 'list');
        params.set('offset', offset);

        fetch(window.location.pathname + '?' + params.toString(), {
          headers: { 'X-Requested-With': 'XMLHttpRequest' }
        })
          .then(function (resp) { return resp.ok ? resp.json() : null; })
          .then(function (data) {
            if (!data || !data.resultsHtml) {
              showMoreBtn.disabled = false;
              showMoreBtn.textContent = 'Show More Plays (50)';
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
              showMoreBtn.disabled = false;
              showMoreBtn.dataset.offset = data.nextOffset;
              showMoreBtn.textContent = 'Show More Plays (50)';
            } else if (actionsDiv) {
              actionsDiv.remove();
            }
          })
          .catch(function (err) {
            console.error(err);
            showMoreBtn.disabled = false;
            showMoreBtn.textContent = 'Show More Plays (50)';
          });
      }
    });

    // _pagination.html's jump-to-page input calls the shared
    // handleJumpToPageKeydown (layout.html), which defers to this hook when
    // present instead of navigating.
    window.__paginationAjaxHandler = function (page) {
      var params = new URLSearchParams(window.location.search);
      params.set('page', page);
      window.history.replaceState({}, '', window.location.pathname + '?' + params.toString());
      loadDetailHistory();
    };
  }

  window.addEventListener('popstate', function () {
    if (filterButtons.length) {
      var params = new URLSearchParams(window.location.search);
      activateView(params.get('view') === 'history' ? 'history' : 'top-songs', false);
    }
    loadDetailHistory();
  });
})();

