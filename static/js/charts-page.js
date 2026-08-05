// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* What is left of the /charts page's browser logic once htmx owns the
 * request/swap layer (see templates/charts.html for the attributes).
 *
 * This file used to be 230 lines. Most of it went the same way /history's did,
 * and the mapping is worth keeping written down:
 *
 *   loadChartsData + AbortController -> hx-get + hx-sync="#chartsCard:replace"
 *   replaceChartsUrl                 -> hx-replace-url="true"
 *   updateChartsFilters' set/delete  -> the form's own inputs, serialized by
 *     surgery on the query string       htmx, plus `disabled` on the custom
 *                                       dates when the range is not custom
 *   updateChartsDateFilter's range   -> HtmxFilters.rangeProblem, vetoing the
 *     check + error styling              request in htmx:configRequest
 *   fadeTargets + loading-fade       -> .htmx-fade-target + hx-indicator
 *   the 401 -> /login branch         -> HX-Redirect, sent by the server
 *   the popstate handler             -> nothing, and deliberately: every URL
 *     update here REPLACES, so this page never puts an entry on the history
 *     stack for itself. It only ever ran on a cross-document Back, which
 *     reloads the page server-side anyway. window.__chartsDefaultInterval, the
 *     fallback it needed to re-select the right option, went with it.
 *   applyChartsData's label writes,  -> rendered by the server, which is what
 *     section hiding, and the           decided all three in the first place
 *     genreSectionHtml injection
 *
 * What could not move is below. The charts themselves are the reason this file
 * still exists at all: they are drawn onto <canvas>, which htmx has no way to
 * swap, so the data comes with the markup as a JSON island and the redraw is a
 * htmx:afterSwap listener. */

//< the swap target, and the data island that arrives inside it
var CHARTS_TARGET_ID = 'chartsCard';
var CHARTS_DATA_ID = 'chartsData';

//< the form htmx watches, in charts.html
var CHARTS_FORM_ID = 'chartsFilters';

//< which ranges the route buckets by hour, and so where the Trend-buckets
//  choice does not apply (see chartsPage's isSingleDayView), lives in
//  HtmxFilters.hidesTrendBuckets - /genres applies the same rule

if (typeof document !== 'undefined') {
  var byId = function (id) { return document.getElementById(id); };

  // Called from the Time Period select's onchange. Runs before htmx's listener
  // (an inline on*= handler fires at the target; htmx's is on the form and
  // fires as the event bubbles), so the disabled flags are already right by the
  // time the request is serialized.
  //
  // `disabled`, not merely hidden: a disabled control is not serialized, which
  // is what keeps a stale custom range out of the request - and so out of the
  // URL - after switching back to a named interval. The bucket select is only
  // HIDDEN for a single-day range, because its value has to survive switching
  // back to a multi-day one.
  window.updateChartsIntervalFilter = function () {
    HtmxFilters.syncCustomRange('chartsCustomDates');
    //< a single-day view is bucketed by hour server-side, so the control is a
    //  no-op there. The rule is shared with /genres, which used to spell it
    //  differently (see HtmxFilters.hidesTrendBuckets).
    byId('groupByContainer').style.display =
      HtmxFilters.hidesTrendBuckets(byId('interval').value) ? 'none' : 'flex';
  };

  // The one place a request gets vetoed, and what stops "custom" firing a
  // request the moment it is selected: a range with no dates yet is
  // RANGE_INCOMPLETE, which is exactly what the old handler's early return
  // covered. Shared with /history and the Top lists, whose filter card is the
  // same control set (static/js/htmx-filters.js).
  document.body.addEventListener('htmx:configRequest', function (evt) {
    if (!evt.detail.elt || evt.detail.elt.id !== CHARTS_FORM_ID) return;
    var problem = HtmxFilters.rangeProblemFromDom();
    HtmxFilters.showRangeError(problem);
    if (problem !== HtmxFilters.RANGE_OK) {
      evt.preventDefault();
      return;
    }
    //< Auto ("") must not reach the URL, or the derived bucket would be pinned
    //  and auto mode frozen - the same thing params.delete('groupBy') did
    HtmxFilters.pruneEmptyParams(evt.detail.parameters);
  });

  // The charts. htmx swaps the card's markup; a <canvas> is not markup, so the
  // series travel with it as a JSON island (see _charts_results.html) and the
  // redraw happens here. This IS window.__chartData - charts.js reads it
  // directly, so there is no reshaping left to get wrong.
  document.body.addEventListener('htmx:afterSwap', function (evt) {
    if (evt.target.id !== CHARTS_TARGET_ID) return;
    var island = evt.target.querySelector('#' + CHARTS_DATA_ID);
    if (island) window.__chartData = JSON.parse(island.textContent);
    if (window.renderAllCharts) window.renderAllCharts();
  });

  // A genuine failure gets the shared inline error + Retry rather than a stuck
  // "Loading…" or a card of stale charts under a URL that says otherwise. An
  // expired session no longer arrives here at all: the server answers an htmx
  // request with HX-Redirect, so the browser navigates to /login instead of
  // this reporting a load failure.
  HtmxFilters.onSwapFailure(CHARTS_TARGET_ID, function () {
    htmx.ajax('GET', window.location.pathname + window.location.search,
              { target: '#' + CHARTS_TARGET_ID, swap: 'innerHTML' });
  });
}
//< no module.exports: everything pure lives in static/js/htmx-filters.js, which
//  is where the plain-node unit test points (tests/test_htmx_filters.js)
