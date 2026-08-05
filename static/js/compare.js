// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* What is left of the Compare page's browser logic once htmx owns the
 * request/swap layer (see templates/compare.html for the attributes).
 *
 * This file was 314 lines, most of them a loader /history and the Top lists
 * each had their own copy of. The mapping is worth keeping written down,
 * because it is the same list every migrated page deletes:
 *
 *   loadCompareData + AbortController    -> hx-get + hx-sync="...:replace"
 *   compareSwapTargets + .loading-fade   -> hx-indicator + .htmx-fade-target
 *   SORT_BY_LIST_SWAPS + the innerHTML   -> hx-swap-oob on each region, from
 *     assignments beside it                one loop in
 *                                          _compare_sortable_lists.html
 *   replaceCompareUrl                    -> hx-replace-url="true"
 *   the four updateCompare*Filter bodies -> the form IS the parameter list
 *   the badge click listener             -> hx-boost on the badges nav
 *   the 401 -> /login branch             -> HX-Redirect, sent by the server
 *   the popstate handler                 -> nothing, and deliberately: every
 *     URL update here REPLACES, so this page never puts an entry on the history
 *     stack for itself and there is nothing to pop back to. It only ever ran on
 *     a cross-document Back, which reloads the page server-side anyway.
 *
 * What could not move is below: the trend chart (htmx swaps the data island; it
 * has no idea a canvas exists, let alone that it has to be painted), the
 * category filter badges (pure show/hide, no request at all), the custom-range
 * validation, and the page-level failure banner.
 *
 * Note there is no `hx-on:` or event-filter shortcut available here: the CSP
 * withholds 'unsafe-eval' from this page (see the header comment in
 * templates/compare.html). */

//< the form htmx watches, and the queue every request on this page joins
var COMPARE_FORM_ID = 'compareFilters';
//< the JSON island the trend chart is repainted from. Only the full refresh
//  carries one - a sort change renders an identical chart, so it does not
var COMPARE_TREND_DATA_ID = 'compareTrendData';
//< marks the regions a refresh replaces; also what hx-indicator dims
var COMPARE_FADE_TARGET_CLASS = 'htmx-fade-target';

// The params whose blank value means "unset" HERE. This page cannot use
// HtmxFilters.pruneEmptyParams as-is, and the exception is the point: Time
// Period's blank value is All Time, a real choice, while an ABSENT interval
// falls back to the user's saved default_dashboard_window - so pruning it would
// silently move an All Time view to "Last Month" on the next filter change.
// Trend buckets has no such fallback: absent and blank both mean Auto (see
// _resolveGroupBy), and leaving it out is what the old handler's
// params.delete('groupBy') achieved.
var COMPARE_AUTO_PARAMS = ['groupBy'];

// Takes the parameters object htmx hands to htmx:configRequest - a Proxy over a
// FormData with deleteProperty and ownKeys traps, so ordinary object operations
// work on it, and a plain object works in the unit test. Mutated in place,
// because htmx reads the same object back after the event.
function pruneCompareAutoParams(parameters) {
  COMPARE_AUTO_PARAMS.forEach(function (key) {
    if (parameters[key] === '') delete parameters[key];
  });
  return parameters;
}

if (typeof window !== 'undefined') {
  // charts.js draws every chart from window.__chartData as soon as it loads.
  // The shell renders before any comparison query runs (see routes/compare.py),
  // so it would otherwise draw the mirror chart immediately off an empty trend;
  // the afterSwap listener below sets the real data and repaints once it lands.
  window.__chartData = window.__chartData || {};
  window.__deferInitialChartRender = true;
}

if (typeof document !== 'undefined') {
  var byId = function (id) { return document.getElementById(id); };

  var currentRangeProblem = function () {
    return HtmxFilters.rangeProblem(byId('interval').value, byId('startDate').value, byId('endDate').value);
  };

  var showRangeError = function (problem) {
    var invalid = problem === HtmxFilters.RANGE_INVERTED;
    var errorElem = byId('dateError');
    errorElem.textContent = invalid ? HtmxFilters.RANGE_INVERTED_MESSAGE : '';
    errorElem.style.display = invalid ? 'block' : 'none';
    byId('startDate').style.borderColor = invalid ? 'var(--accent)' : '';
    byId('endDate').style.borderColor = invalid ? 'var(--accent)' : '';
  };

  // Called from the Time Period select's onchange. Both this and htmx's own
  // listener sit on that select (the form's trigger names it with `from:`), and
  // listeners on one element fire in registration order - an inline on*=
  // attribute is registered while the page parses, htmx's when it initializes
  // on DOMContentLoaded. So the disabled flags below are already correct by the
  // time the request is serialized.
  //
  // `disabled`, not merely hidden: a disabled control is not serialized, which
  // is what keeps a stale custom range out of the request - and therefore out
  // of the URL, since hx-replace-url writes back what was requested.
  window.updateCompareIntervalFilter = function () {
    var custom = byId('interval').value === 'custom';
    byId('compareCustomDates').style.display = custom ? 'flex' : 'none';
    byId('startDate').disabled = !custom;
    byId('endDate').disabled = !custom;
    showRangeError(currentRangeProblem());
  };

  // The one place a request gets vetoed, and the guard Compare was the last
  // page to gain: it used to fetch an inverted range and render an empty
  // comparison with no explanation.
  //
  // Scoped to the FORM's own requests. A boosted counterpart badge carries its
  // whole query in its href and must keep working even while the Time Period
  // select sits on a half-entered custom range - which is exactly the state
  // that blocks a form request.
  document.body.addEventListener('htmx:configRequest', function (evt) {
    if (!evt.detail.elt || evt.detail.elt.id !== COMPARE_FORM_ID) return;
    var problem = currentRangeProblem();
    showRangeError(problem);
    if (problem !== HtmxFilters.RANGE_OK) {
      evt.preventDefault();
      return;
    }
    pruneCompareAutoParams(evt.detail.parameters);
  });

  // Smooth cover-art fade-ins for freshly swapped cards. The images are new
  // elements on every swap, so this cannot be a one-time delegated listener.
  // Filtered to the swap CONTAINERS: htmx fires afterSwap on every inserted
  // node as well, and re-binding a load listener per card would stack
  // duplicates on the same image.
  document.body.addEventListener('htmx:afterSwap', function (evt) {
    if (!evt.target.classList || !evt.target.classList.contains(COMPARE_FADE_TARGET_CLASS)) return;
    evt.target.querySelectorAll('img.track-cover').forEach(function (img) {
      if (img.complete) {
        img.classList.add('loaded');
      } else {
        img.addEventListener('load', function () { img.classList.add('loaded'); });
      }
    });
  });

  // The trend chart. htmx swaps the data island in; painting a <canvas> from it
  // is the one thing no attribute can express.
  document.body.addEventListener('htmx:afterSwap', function (evt) {
    if (evt.target.id !== COMPARE_TREND_DATA_ID) return;
    window.__chartData.comparisonTrend = JSON.parse(evt.target.textContent);
    if (window.renderComparisonMirror) {
      window.renderComparisonMirror();
    }
  });

  // A genuine failure gets the shared page-level banner + Retry rather than a
  // stuck "Loading…" or a silently stale comparison. Page-level, not inline
  // (see static/js/ajax-status.js): one response feeds a dozen regions, so
  // there is no single placeholder the message belongs in.
  //
  // An expired session no longer arrives here at all, and neither does a share
  // revoked mid-session: the route answers both with HX-Redirect, so the
  // browser navigates instead of this reporting a load failure.
  var reloadCompare = function () {
    htmx.ajax('GET', window.location.pathname + window.location.search,
              { source: byId(COMPARE_FORM_ID), target: 'body', swap: 'none' });
  };
  var reportCompareFailure = function () {
    if (window.AjaxStatus) window.AjaxStatus.showBanner(reloadCompare);
  };
  document.body.addEventListener('htmx:responseError', reportCompareFailure);
  document.body.addEventListener('htmx:sendError', reportCompareFailure);
  document.body.addEventListener('htmx:afterRequest', function (evt) {
    //< a later success is what clears a stale banner; Retry clears its own
    if (evt.detail && evt.detail.successful && window.AjaxStatus) {
      window.AjaxStatus.clearBanner();
    }
  });

  // ---- Category filter badges - same show/hide + staggered fade-in pattern
  // as the Wrapped page ([data-category].visible in style.css). No request and
  // no URL change, so htmx has nothing to say about it. ----
  var compareFilterButtons = document.querySelectorAll('.stats-filter-button');
  var compareCategoryDivs = document.querySelectorAll('[data-category]');

  compareFilterButtons.forEach(function (button) {
    button.addEventListener('click', function () {
      var filter = button.dataset.filter;

      compareFilterButtons.forEach(function (btn) { btn.classList.remove('active'); });
      button.classList.add('active');

      compareCategoryDivs.forEach(function (div) {
        if (filter === 'all' || div.dataset.category === filter) {
          div.classList.add('visible');
        } else {
          div.classList.remove('visible');
        }
      });
    });
  });

  var compareAllButton = document.querySelector('.stats-filter-button[data-filter="all"]');
  if (compareAllButton) {
    compareAllButton.click();
  }
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { pruneCompareAutoParams, COMPARE_AUTO_PARAMS };
}
