// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* What is left of the /genres page's browser logic once htmx owns the
 * request/swap layer (see templates/genres.html for the attributes).
 *
 * This file was 400 lines, two fetch loaders and a hand-rolled history layer.
 * The mapping is the same one /history and the Top lists wrote down, plus three
 * entries this page contributes:
 *
 *   loadGenresData/loadGenreDetail + loadToken -> hx-get + hx-sync="...:replace"
 *   replaceGenresUrl                           -> hx-replace-url="true"
 *   the delegated chip click listener          -> hx-boost on the chip row
 *   setSelectedChip                            -> the server re-renders the row
 *   detailFallbackUrl                          -> HX-Redirect, sent by the route
 *                                                 when it declines a chip swap
 *   the 401 -> /login branch                   -> HX-Redirect, sent by the
 *                                                 server (app.py's
 *                                                 unauthenticatedResponse)
 *   the popstate handler                       -> nothing, and deliberately:
 *     every URL update here REPLACES, so this page never puts an entry on the
 *     history stack for itself and there is no in-page state to pop back to.
 *     The handler only ever ran on a cross-document Back, which reloads the
 *     page server-side anyway.
 *
 * What htmx genuinely could not absorb is the CHARTS. htmx swaps HTML: a
 * <canvas> arrives blank and has to be painted, and its data is not markup. So
 * each fragment carries its datasets in a <script type="application/json">
 * island, and the htmx:afterSwap listener at the bottom reads them back out and
 * redraws. That listener is hand-written on purpose; there is no htmx feature
 * that owns chart rendering.
 *
 * Note there is no `hx-on:` or event-filter shortcut available as a shortcut
 * here - the CSP withholds 'unsafe-eval' from this page (see the header comment
 * in templates/genres.html). */
(function () {
  var CU = window.ChartUtils;
  if (!CU) return;

  //< the form htmx watches, and the two regions it and the chips swap into
  var GENRES_FORM_ID = 'genresFilters';
  var GENRES_RESULTS_ID = 'genresResults';
  var GENRE_EXPLORE_ID = 'genreExplore';
  //< the JSON islands each fragment carries its canvas data in (see
  //  templates/_genres_results.html and templates/_genre_explore.html)
  var OVERVIEW_DATA_ID = 'genres-overview-data';
  var DETAIL_DATA_ID = 'genres-detail-data';

  var GENRE_BREADTH_LIMIT = 8;      //< keep the horizontal breadth chart to a readable height

  window.__genreData = window.__genreData || {};

  var byId = function (id) { return document.getElementById(id); };

  // ---- Overview charts (redrawn on each time-period change) -----------------

  function renderDistribution() {
    var pairs = (window.__genreData && window.__genreData.distributionPairs) || [];
    // Horizontal bars, like Artists per Genre below - long genre names never
    // truncate into an ambiguous collision, like "alternative rock"/
    // "alternative metal" both cutting down to "alternative…" used to.
    CU.renderHorizontalBars(byId('genreDistChart'), pairs, {
      emptyMessage: 'No genre data yet.',
      valueSuffix: ' plays'
    });
  }

  function renderShare() {
    var canvas = byId('genreShareChart');
    if (!canvas) return;
    var pairs = (window.__genreData && window.__genreData.distributionPairs) || [];
    var grandTotal = pairs.reduce(function (sum, p) { return sum + p[1]; }, 0);
    // Cap the coloured slices to the palette; fold the rest into one muted
    // "Other" slice so the donut stays readable.
    var maxSlices = CU.PALETTE.length - 1;
    var slices = pairs.slice(0, maxSlices).map(function (p, i) {
      return { label: p[0], value: p[1], color: CU.PALETTE[i % CU.PALETTE.length] };
    });
    var shownTotal = slices.reduce(function (sum, s) { return sum + s.value; }, 0);
    var otherValue = grandTotal - shownTotal;
    if (otherValue > 0) {
      slices.push({ label: 'Other', value: otherValue, color: 'rgba(255,255,255,0.25)' });
    }
    CU.drawDonutChart(canvas, slices, grandTotal, { emptyMessage: 'No genre data yet.' });

    // The donut has many slices, so the legend lives in HTML below it. It shows
    // just the genre name; the play count + % stay in the hover tooltip only.
    var legend = byId('genreShareLegend');
    if (legend) {
      legend.innerHTML = grandTotal ? slices.map(function (s) {
        return '<span class="chart-legend-item"><span class="chart-legend-swatch" style="background:' + s.color + '"></span>' +
          CU.escapeHtml(s.label) + '</span>';
      }).join('') : '';
    }
  }

  function renderBreadth() {
    var pairs = ((window.__genreData && window.__genreData.breadthPairs) || []).slice(0, GENRE_BREADTH_LIMIT);
    CU.renderHorizontalBars(byId('genreBreadthChart'), pairs, {
      emptyMessage: 'No genre data yet.',
      valueSuffix: ' artists'
    });
  }

  function renderMix() {
    CU.renderMultiLineChart(
      byId('genreMixChart'),
      byId('genreMixLegend'),
      (window.__genreData && window.__genreData.mixTrend) || { buckets: [], series: [] },
      {
        emptyMessage: 'Not enough data yet to show a genre trend.',
        formatValue: function (v) { return Math.round(v) + ' plays'; }
      }
    );
  }

  function renderOverview() {
    renderDistribution();
    renderShare();
    renderBreadth();
    renderMix();
  }

  // ---- Per-genre drill-down charts (redrawn on each genre swap) -------------

  function renderSelectedTrend() {
    CU.renderMultiLineChart(
      byId('genreTrendChart'),
      null,
      (window.__genreData && window.__genreData.selectedTrend) || { buckets: [], series: [] },
      {
        emptyMessage: 'Not enough data yet for this genre.',
        formatValue: function (v) { return Math.round(v) + ' plays'; }
      }
    );
  }

  function renderClock() {
    CU.renderHeatmap(
      byId('genreClockChart'),
      (window.__genreData && window.__genreData.clock) || [],
      { emptyMessage: 'No listening data for this genre yet.' }
    );
  }

  function renderDetailCharts() {
    renderSelectedTrend();
    renderClock();
  }

  // Everything, from whatever is currently in window.__genreData. Used by the
  // resize and theme handlers, which change how the canvases look without
  // changing what is on them.
  function renderAll() {
    CU.refreshPalette();
    renderOverview();
    renderDetailCharts();
  }

  // ---- Reading the datasets back out of the swapped fragment ----------------

  function readIsland(id) {
    var el = byId(id);
    if (!el) return null;
    try {
      return JSON.parse(el.textContent);
    } catch (err) {
      //< a truncated or half-swapped fragment; the charts keep their last data
      //  rather than being cleared to an empty state that looks like real zero
      return null;
    }
  }

  function absorbOverviewData() {
    var data = readIsland(OVERVIEW_DATA_ID);
    if (!data) return;
    window.__genreData.distributionPairs = data.distributionPairs;
    window.__genreData.breadthPairs = data.breadthPairs;
    window.__genreData.mixTrend = data.mixTrend;
  }

  function absorbDetailData() {
    var data = readIsland(DETAIL_DATA_ID);
    if (!data) return;
    window.__genreData.selectedTrend = data.selectedTrend;
    window.__genreData.clock = data.clock;
    // The filter form serializes itself on a time-period change, and the
    // drill-down selection has no visible control in it - so the RESOLVED genre
    // is carried back into the form's hidden field. Without this, picking a new
    // time period would silently reset the page to that range's top genre.
    var field = byId('genresSelectedGenre');
    if (field) field.value = data.genre || '';
  }

  // Called from the Time Period select's onchange. Runs before htmx's own
  // listener does (an inline on*= handler fires at the target, htmx's is on the
  // form and fires as the event bubbles), so the disabled flags below are
  // already correct by the time the request is serialized.
  //
  // `disabled`, not merely hidden: a disabled control is not serialized, which
  // is what keeps a stale custom range out of the request - and therefore out
  // of the URL - after switching back to a named interval.
  window.updateGenresIntervalFilter = function () {
    HtmxFilters.syncCustomRange('genresCustomDates');
    //< the same rule /charts applies, now in one spelling rather than two that
    //  only a comment kept in step (see HtmxFilters.hidesTrendBuckets)
    var groupByContainer = byId('groupByContainer');
    if (groupByContainer) {
      groupByContainer.style.display =
        HtmxFilters.hidesTrendBuckets(byId('interval').value) ? 'none' : 'flex';
    }
  };

  // The one place a request gets vetoed.
  document.body.addEventListener('htmx:configRequest', function (evt) {
    var elt = evt.detail.elt;
    if (!elt) return;

    // A chip that is already showing its genre. The old delegated listener
    // returned early rather than re-fetching an identical drill-down, and
    // hx-boost has no attribute that says "not this one", so the check lives
    // here instead.
    if (elt.classList && elt.classList.contains('genre-chip')) {
      if (elt.classList.contains('selected')) evt.preventDefault();
      return;
    }

    // Everything below is about the FILTER form only: a boosted chip carries
    // its whole query in its href and must keep working even while the Time
    // Period select sits on a half-entered custom range, which is exactly the
    // state that blocks a form request.
    if (elt.id !== GENRES_FORM_ID) return;
    var problem = HtmxFilters.rangeProblemFromDom();
    HtmxFilters.showRangeError(problem);
    if (problem !== HtmxFilters.RANGE_OK) {
      evt.preventDefault();
      return;
    }
    HtmxFilters.pruneEmptyParams(evt.detail.parameters);
  });

  // ---- The part htmx cannot own: painting the canvases it just swapped in ---
  //
  // Scoped to the two swap targets by id. htmx fires afterSwap on the target
  // AND on each top-level element it inserted, so an unguarded listener would
  // redraw several times per swap.
  document.body.addEventListener('htmx:afterSwap', function (evt) {
    var swapped = evt.target.id;
    if (swapped !== GENRES_RESULTS_ID && swapped !== GENRE_EXPLORE_ID) return;
    CU.refreshPalette();
    //< a chip swap does not carry the overview island at all - its datasets are
    //  unchanged by a genre switch, and its canvases were not replaced either
    if (swapped === GENRES_RESULTS_ID) {
      absorbOverviewData();
      renderOverview();
    }
    absorbDetailData();
    renderDetailCharts();
    //< a swap landed, so whatever failed before has been recovered from
    if (window.AjaxStatus) window.AjaxStatus.clearBanner();
  });

  // A genuine failure gets the shared error + Retry rather than a stuck
  // "Loading…" or silently stale charts. This page uses the page-level banner
  // rather than renderInto: its swap targets are mostly canvases, and replacing
  // their markup with an error block would leave nothing left to draw on.
  //
  // An expired session no longer arrives here at all: the server answers an
  // htmx request with HX-Redirect, so the browser navigates to /login instead
  // of this reporting a load failure.
  var reportGenresFailure = function (evt) {
    if (!window.AjaxStatus) return;
    var detail = evt.detail || {};
    //< retry the request that actually failed - which of the two shapes it was
    //  is already encoded in its target and its URL
    var target = detail.target || byId(GENRES_RESULTS_ID);
    var pathInfo = detail.pathInfo || {};
    //< and off the element that issued it (htmx stamps it on every event as
    //  detail.elt), so htmx re-serialises the form's CURRENT values, runs the
    //  configRequest veto/prune above and inherits its hx-replace-url - without
    //  a source, a successful Retry left the address bar on the previous
    //  period. That element's BARE hx-get path then, not the final one: htmx
    //  appends the serialised values to whatever path it is handed, so the
    //  final path would carry every form value twice.
    var elt = detail.elt;
    var path = (elt && pathInfo.requestPath) || pathInfo.finalRequestPath ||
      (window.location.pathname + window.location.search);
    if (!target) return;
    window.AjaxStatus.showBanner(function () {
      htmx.ajax('GET', path, { source: elt, target: target, swap: 'innerHTML' });
    });
  };
  document.body.addEventListener('htmx:responseError', reportGenresFailure);
  document.body.addEventListener('htmx:sendError', reportGenresFailure);

  // ---- Repaints that are not swaps -----------------------------------------

  //< the resize debounce and the theme-change repaint (see bindRepaint's
  //  comment in chart-utils.js for why the latter waits, and for the
  //  '#theme-selector' listener that never fired)
  CU.bindRepaint(renderAll);
})();
//< no module.exports: the pure filter helpers moved to static/js/htmx-filters.js,
//  which is where the plain-node unit test points (tests/test_htmx_filters.js)
