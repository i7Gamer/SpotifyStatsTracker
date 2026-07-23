/* The song/artist/album detail pages' "Trend buckets" select: re-fetches just
 * the play-history time series via the route's ?ajax=true branch and redraws
 * that one chart in place - no full page reload. Mirrors charts-page.js's
 * abort-superseded-loads pattern; the inline onchange="updateDetailGroupByFilter()"
 * handler in the three detail templates depends on the global defined here.
 * Loaded after charts.js (window.renderTimeSeriesChart, window.__chartData). */
(function () {
  var DETAIL_FADE_MS = 200;

  //< the in-flight fetch ({controller, wrap}) - a newer bucket change aborts it
  //  so a slow older response can't land after (and clobber) the newer one
  var activeLoad = null;

  // Pure URL builders (unit-tested in tests/test_detail_chart.js). Kept free of
  // window/document so they can run under plain node.
  function detailDataUrl(pathname, search) {
    var params = new URLSearchParams(search);
    params.set('ajax', 'true');
    return pathname + '?' + params.toString();
  }

  function detailPageUrl(pathname, search, groupBy) {
    var params = new URLSearchParams(search);
    if (groupBy) {
      params.set('groupBy', groupBy);
    } else {
      params.delete('groupBy');   //< Auto: let the server derive from the item's play span
    }
    params.delete('ajax');
    var query = params.toString();
    return pathname + (query ? '?' + query : '');
  }

  function timeSeriesWrap() {
    var canvas = document.getElementById('timeSeriesChart');
    return canvas ? canvas.parentElement : null;
  }

  function loadDetailTimeSeries() {
    if (activeLoad) {
      activeLoad.controller.abort();
      if (activeLoad.wrap) {
        activeLoad.wrap.classList.remove('loading-fade');
      }
    }
    var controller = new AbortController();
    var wrap = timeSeriesWrap();
    activeLoad = { controller: controller, wrap: wrap };
    if (wrap) {
      wrap.classList.add('loading-fade');
    }

    var delay = new Promise(function (resolve) { setTimeout(resolve, DETAIL_FADE_MS); });
    var fetched = fetch(detailDataUrl(window.location.pathname, window.location.search), {
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      signal: controller.signal
    }).then(function (resp) { return resp.ok ? resp.json() : null; });

    Promise.all([fetched, delay])
      .then(function (results) {
        //< a response that settled before its abort can still reach here; never
        //  swap stale data in over a newer load's
        if (!activeLoad || activeLoad.controller !== controller) {
          return;
        }
        if (results[0]) {
          window.__chartData.timeSeries = results[0].timeSeries;
          if (window.renderTimeSeriesChart) {
            window.renderTimeSeriesChart();
          }
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
          if (wrap) {
            wrap.classList.remove('loading-fade');
          }
        }
      });
  }

  function updateDetailGroupByFilter() {
    var groupBy = document.getElementById('groupBy').value;
    window.history.pushState({}, '',
      detailPageUrl(window.location.pathname, window.location.search, groupBy));
    // The admin "Refresh Last.fm Data" form redirects back with its hidden
    // groupBy - keep it matching the visible choice instead of the value the
    // page originally rendered with.
    document.querySelectorAll('form input[type="hidden"][name="groupBy"]').forEach(function (input) {
      input.value = groupBy;
    });
    loadDetailTimeSeries();
  }

  if (typeof window !== 'undefined') {
    // The inline onchange="updateDetailGroupByFilter()" in the three detail
    // templates depends on this global.
    window.updateDetailGroupByFilter = updateDetailGroupByFilter;

    window.addEventListener('popstate', function () {
      var params = new URLSearchParams(window.location.search);
      var select = document.getElementById('groupBy');
      if (select) {
        select.value = params.get('groupBy') || '';
      }
      loadDetailTimeSeries();
    });
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { detailDataUrl, detailPageUrl };
  }
})();
