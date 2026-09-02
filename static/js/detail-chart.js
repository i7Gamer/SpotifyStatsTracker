// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* The song/artist/album detail pages' "Trend buckets" select: re-fetches just
 * the play-history time series via the route's ?ajax=true branch and redraws
 * that one chart in place - no full page reload. Mirrors charts-page.js's
 * abort-superseded-loads pattern; the inline onchange="updateDetailGroupByFilter()"
 * handler in the three detail templates depends on the global defined here.
 * Loaded after charts.js (window.renderTimeSeriesChart, window.__chartData). */
(function () {
  var DETAIL_FADE_MS = 200;
  var HTTP_NOT_FOUND = 404;

  //< the play log (detail-history.js) reports through the same banner slot;
  //  naming this loader keeps one's success from clearing the other's failure
  var BANNER_OWNER = 'detail-chart';

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
    }).then(function (resp) {
      // A 404 here is the route saying the entity no longer resolves (an
      // overwrite import or a merge removed it after the page loaded) and
      // WHERE to go instead - the JSON twin of the HX-Redirect the deferred
      // body gets (routes/charts.py's _missingEntityResponse). Peeled off
      // ahead of the shared helper, which throws on every non-2xx before
      // reading a body: left to it, the banner's Retry failed identically
      // forever while a reload of the same URL redirected to the top list.
      if (resp.status === HTTP_NOT_FOUND) {
        return resp.json().then(function (body) {
          if (body && body.redirectUrl) {
            window.location.href = body.redirectUrl;
            //< the "we're leaving the page" sentinel the catch already skips;
            //  the redirect is not about the session, only the outcome is shared
            throw new Error(window.AjaxStatus.UNAUTHORIZED_ERROR);
          }
          //< a 404 with nowhere to go is the plain failure it always was
          return window.AjaxStatus.readJsonOrThrow(resp, 'detail chart');
        });
      }
      //< the Group-by select has already moved, so a swallowed non-2xx would
      //  leave the PREVIOUS series on screen labelled as the new one
      return window.AjaxStatus.readJsonOrThrow(resp, 'detail chart');
    });

    Promise.all([fetched, delay])
      .then(function (results) {
        //< a response that settled before its abort can still reach here; never
        //  swap stale data in over a newer load's
        if (!activeLoad || activeLoad.controller !== controller) {
          return;
        }
        window.__chartData.timeSeries = results[0].timeSeries;
        if (window.renderTimeSeriesChart) {
          window.renderTimeSeriesChart();
        }
        if (window.AjaxStatus) window.AjaxStatus.clearBanner(BANNER_OWNER);
      })
      .catch(function (err) {
        //< navigating to /login - not a load failure to report
        if (window.AjaxStatus && window.AjaxStatus.isUnauthorizedError(err)) return;
        if (err.name !== 'AbortError') {
          console.error(err);
          //< the canvas can't hold a message, so surface a banner with Retry
          if ((!activeLoad || activeLoad.controller === controller) && window.AjaxStatus) {
            window.AjaxStatus.showBanner(function () { loadDetailTimeSeries(); }, undefined, BANNER_OWNER);
          }
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
    // replaceState, not push: keep the URL shareable without stacking a history
    // entry, so Back returns to the previous page rather than past bucket states.
    window.history.replaceState({}, '',
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
