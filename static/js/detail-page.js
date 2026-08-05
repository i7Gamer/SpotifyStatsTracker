// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* What is left of the song/artist/album detail pages' deferred-body load once
 * htmx owns the request/swap layer (see templates/song_detail.html for the
 * attributes, shared in shape by all three shells).
 *
 * This file used to be the loader: build the ?ajax=page URL, fetch it, branch
 * on 401 and on 404, parse the JSON, write bodyHtml into #detailBody. htmx does
 * the request and the swap declaratively, and the two error branches moved to
 * the server, where they are now one response header each:
 *
 *   detailBodyUrl + fetch + DOMContentLoaded -> hx-get + hx-trigger="load"
 *   the 401 -> /login branch                 -> HX-Redirect (app.py's
 *                                               unauthenticatedResponse)
 *   the 404 -> redirectUrl branch            -> HX-Redirect (_missingEntityResponse)
 *
 * That second one is why this file also leaves EXPECTED_PAGE_LOADERS *and*
 * HAND_ROLLED_EXCEPTIONS in tests/test_ajax_loader_error_handling.py: it was
 * the documented exception there precisely because reading a 404's body meant
 * it could not use the shared payload-reading helper. There is no body to read
 * any more. (Named obliquely on purpose - that gate matches on source text, and
 * a comment naming the helper would satisfy it without any code doing so.)
 *
 * What could not move is below, and it is all of one kind: the swap puts new
 * elements on the page, and something has to point the non-htmx machinery at
 * them. Charts are the main one - htmx swaps markup, and a <canvas> is not
 * markup, so the render is a htmx:afterSwap listener we write. */

// The chart series for one item's page, as window.__chartData expects them.
// DOM-free and exported so the contract is unit-testable in plain node (see
// tests/test_detail_page.js).
//
// showSkips is what turns the play-history chart's second (skip-count) series
// on, and these pages are the only ones that set it: a skip is a per-item
// behaviour signal here, and a track whose plays are ALL skips has to render at
// all. The aggregate pages leave it off - see renderTimeSeriesChart for why it
// does not survive contact with buckets holding real listening volume. It stays
// a client-side flag rather than riding in the island, because it is about how
// charts.js draws rather than about what the server measured.
//
// heatmap is absent on artist/album, whose island has no such key;
// renderAllCharts skips a canvas that isn't there, so it stays undefined rather
// than becoming an empty series.
function detailChartData(data) {
  return { timeSeries: data.timeSeries, heatmap: data.heatmap, showSkips: true };
}

if (typeof window !== 'undefined') (function () {
  //< the swap target, and the data island that arrives inside it
  var DETAIL_BODY_ID = 'detailBody';
  var CHART_DATA_ID = 'detailChartData';

  // The canvases only exist once the body is on the page, so charts.js was told
  // to skip its initial render (window.__deferInitialChartRender in the shell)
  // and is driven from here instead.
  function applyDetailBody(el) {
    var island = el.querySelector('#' + CHART_DATA_ID);
    if (island) {
      window.__chartData = detailChartData(JSON.parse(island.textContent));
      if (window.renderAllCharts) window.renderAllCharts();
    }
    //< both bind to elements that arrived with the body above
    if (window.initDetailHistory) window.initDetailHistory();
    if (window.initPlayEmbed) window.initPlayEmbed();
    //< cover-art fade-ins are handled once for the whole app in
    //  static/js/chrome-common.js. This file used to re-mark them because that
    //  sweep only ran at DOMContentLoaded - it now re-runs on htmx:afterSwap,
    //  which is what the workaround was standing in for.
    //< disabled in the shell so a bucket change can't be issued against a chart
    //  that isn't on the page yet - and can't then be overwritten by the body
    //  swap already in flight
    var select = document.getElementById('groupBy');
    if (select) select.disabled = false;
  }

  document.body.addEventListener('htmx:afterSwap', function (evt) {
    if (evt.target.id !== DETAIL_BODY_ID) return;
    applyDetailBody(evt.target);
  });

  // A genuine failure gets the shared inline error + Retry rather than a
  // skeleton that pulses forever with no way out. Neither an expired session
  // nor a removed entity arrives here any more: the server answers both with
  // HX-Redirect, so the browser navigates instead of this reporting a load
  // failure for something that is not one.
  var reportDetailBodyFailure = function (evt) {
    var target = document.getElementById(DETAIL_BODY_ID);
    if (!target || !window.AjaxStatus) return;
    //< the swap target of the request that failed, not the element that fired
    //  it. The play log swaps live INSIDE #detailBody and report themselves
    //  (see detail-history.js), so an element-containment check would let a
    //  failed sort change blank the whole body.
    if (!evt.detail || evt.detail.target !== target) return;
    window.AjaxStatus.renderInto(target, function () {
      htmx.ajax('GET', window.location.pathname + window.location.search,
                { target: '#' + DETAIL_BODY_ID, swap: 'innerHTML' });
    });
  };
  document.body.addEventListener('htmx:responseError', reportDetailBodyFailure);
  document.body.addEventListener('htmx:sendError', reportDetailBodyFailure);
})();

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { detailChartData };
}
