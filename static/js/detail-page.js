/* Two-phase load for the song/artist/album detail pages. The initial GET is a
 * shell - hero, toolbar, tag panel - and everything below it (the entity card,
 * the charts, the songs list, the play log) is fetched here via ?ajax=page and
 * swapped into #detailBody, the same shape /charts, /genres, /history and the
 * Top pages already use. The pages had grown a play-log query, two bucketed
 * chart aggregates, a songs aggregate, a Last.fm biography fetch and a skip
 * summary, all in front of the first paint.
 *
 * Three AJAX modes share these routes and this one owns only the whole body;
 * the Trend-buckets select (?ajax=true, detail-chart.js) and the play log's
 * sort/page controls (?ajax=list, detail-history.js) keep their own narrower
 * refetches OF that body. The select is disabled in the shell and enabled here
 * so a bucket change can't be issued against a chart that isn't on the page
 * yet - and can't then be overwritten by the body payload already in flight.
 *
 * The URL is never touched: this load is what the current URL already means.
 * Loaded last of the detail scripts, since it calls the init functions the
 * others define. */

// --- pure helpers -----------------------------------------------------------
// DOM-free and exported so the URL contract is unit-testable in plain node,
// like the sibling modules' logic (see tests/test_detail_page.js).

//< must match routes/charts.py's DETAIL_BODY_AJAX
var DETAIL_BODY_AJAX = 'page';

// The deferred body's URL for the page currently on screen. Every other
// parameter rides along untouched: ?page=, ?sort=, ?view= and ?groupBy= are
// all part of what the visitor asked for, and a shared link carries them.
function detailBodyUrl(pathname, search) {
  var params = new URLSearchParams(search);
  params.set('ajax', DETAIL_BODY_AJAX);
  return pathname + '?' + params.toString();
}

// window.__chartData for one item's page. showSkips is what turns the play
// history chart's second (skip-count) series on, and these pages are the only
// ones that set it: a skip is a per-item behaviour signal here, and a track
// whose plays are ALL skips has to render at all. The aggregate pages leave it
// off - see renderTimeSeriesChart for why it does not survive contact with
// buckets holding real listening volume. heatmap is absent on artist/album,
// whose payload has no such chart; renderAllCharts skips a canvas that isn't
// there, so it stays undefined rather than becoming an empty series.
function detailChartData(data) {
  return { timeSeries: data.timeSeries, heatmap: data.heatmap, showSkips: true };
}

if (typeof window !== 'undefined') (function () {
  function target() { return document.getElementById('detailBody'); }

  // Cached covers can finish loading before layout.html's delegated handler
  // ever sees them, so mark those explicitly (same as top-list.js does after
  // its own swaps) or they stay at opacity 0.
  function fadeInCovers(el) {
    el.querySelectorAll('img.track-cover').forEach(function (img) {
      if (img.complete) img.classList.add('loaded');
      else img.addEventListener('load', function () { img.classList.add('loaded'); });
    });
  }

  function applyDetailBody(el, data) {
    el.innerHTML = data.bodyHtml;
    // The canvases only exist now, so charts.js was told to skip its initial
    // render (window.__deferInitialChartRender in the shell) and is driven
    // from here instead.
    window.__chartData = detailChartData(data);
    if (window.renderAllCharts) window.renderAllCharts();
    //< both bind to elements that arrived with the body above
    if (window.initDetailHistory) window.initDetailHistory();
    if (window.initPlayEmbed) window.initPlayEmbed();
    fadeInCovers(el);
    var select = document.getElementById('groupBy');
    if (select) select.disabled = false;
  }

  function loadDetailBody() {
    var el = target();
    if (!el) return;

    fetch(detailBodyUrl(window.location.pathname, window.location.search), {
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    })
      .then(function (resp) {
        //< an expired session: go to the login page instead of parsing its
        //  HTML as JSON and dead-ending on a Retry that can never succeed
        if (window.AjaxStatus && window.AjaxStatus.redirectIfUnauthorized(resp)) {
          throw new Error(window.AjaxStatus.UNAUTHORIZED_ERROR);
        }
        if (!resp.ok) throw new Error('detail body fetch failed: ' + resp.status);
        return resp.json();
      })
      .then(function (data) {
        applyDetailBody(el, data);
      })
      .catch(function (err) {
        //< navigating to /login - not a load failure to report
        if (window.AjaxStatus && window.AjaxStatus.isUnauthorizedError(err)) return;
        console.error(err);
        //< the skeleton would otherwise pulse forever with no way out
        if (window.AjaxStatus) window.AjaxStatus.renderInto(el, loadDetailBody);
      });
  }

  // No abort/supersede bookkeeping here, unlike the filter loaders this
  // mirrors: nothing on the shell re-fires this load, so there is only ever
  // one in flight.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadDetailBody);
  } else {
    loadDetailBody();
  }
})();

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { detailBodyUrl, detailChartData, DETAIL_BODY_AJAX };
}
