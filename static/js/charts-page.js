/* Drives the /charts page: fetches the chart-data payload after first paint
 * (and on every filter change) so the page shell renders immediately, then
 * hands the actual drawing to charts.js (window.renderAllCharts). Mirrors the
 * AJAX filter pattern of compare.js. The inline onchange="updateCharts*()"
 * handlers in charts.html depend on the globals defined here. */
(function () {
  var CHARTS_FADE_MS = 200;

  // Only the canvas wraps + the genre section dim during a filter fetch (they
  // carry the opacity transition in style.css); the headings stay put.
  function fadeTargets() {
    var nodes = Array.prototype.slice.call(
      document.querySelectorAll('#chartsCard .chart-canvas-wrap'));
    var genreSection = document.getElementById('chartsGenreSection');
    if (genreSection) {
      nodes.push(genreSection);
    }
    return nodes;
  }

  //< the in-flight fetch ({controller, targets}) - a newer filter change aborts
  //  it so a slow older response can't land after (and clobber) the newer one
  var activeLoad = null;

  function loadChartsData(opts) {
    opts = opts || {};
    var initial = !!opts.initial;

    if (activeLoad) {
      activeLoad.controller.abort();
      activeLoad.targets.forEach(function (t) { t.classList.remove('loading-fade'); });
    }
    var controller = new AbortController();
    //< no fade on the very first load: the canvases start empty, so there's
    //  nothing to dim - just fill them in once the payload lands
    var targets = initial ? [] : fadeTargets();
    activeLoad = { controller: controller, targets: targets };
    targets.forEach(function (t) { t.classList.add('loading-fade'); });

    var params = new URLSearchParams(window.location.search);
    params.set('ajax', 'true');
    var delay = new Promise(function (resolve) { setTimeout(resolve, initial ? 0 : CHARTS_FADE_MS); });
    var fetched = fetch(window.location.pathname + '?' + params.toString(), {
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
          applyChartsData(results[0]);
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
          targets.forEach(function (t) { t.classList.remove('loading-fade'); });
        }
      });
  }

  function applyChartsData(data) {
    window.__chartData = {
      timeSeries: data.timeSeries,
      heatmap: data.heatmap,
      artistTrend: data.artistTrend,
      explicitRatio: data.explicitRatio,
      decadeDistribution: data.decadeDistribution,
      completionStats: data.completionStats,
      genreDistribution: data.genreDistribution,
      groupBy: data.groupBy,
      interval: data.interval
    };

    var timeLabel = document.getElementById('chartsTimeSeriesLabel');
    if (timeLabel) {
      timeLabel.textContent = data.intervalLabel + (data.lastDayDate ? ' (' + data.lastDayDate + ')' : '');
    }
    var trendLabel = document.getElementById('chartsArtistTrendLabel');
    if (trendLabel) {
      trendLabel.textContent = data.intervalLabel;
    }

    // Single-day ranges carry no artist trend (server sends null) - hide the
    // whole section rather than drawing an empty-state chart.
    var trendSection = document.getElementById('chartsArtistTrendSection');
    if (trendSection) {
      trendSection.style.display = (data.artistTrend === null) ? 'none' : '';
    }

    // The Top Genres body (locked progress vs. unlocked chart) is range-scoped,
    // so it arrives as pre-rendered HTML - inject it before rendering so the
    // #genreChart canvas exists when renderAllCharts draws into it.
    var genreSection = document.getElementById('chartsGenreSection');
    if (genreSection && typeof data.genreSectionHtml === 'string') {
      genreSection.innerHTML = data.genreSectionHtml;
    }

    if (window.renderAllCharts) {
      window.renderAllCharts();
    }
  }

  // ---- URL + filter handlers (globals for the inline onchange handlers) ----

  // Update the URL in place (replaceState, not push) so a filter change stays
  // shareable/refreshable without stacking a history entry - Back then returns
  // to the page the user came from instead of stepping back through past filters.
  function replaceChartsUrl(mutate) {
    var params = new URLSearchParams(window.location.search);
    mutate(params);
    params.delete('ajax');
    var query = params.toString();
    window.history.replaceState({}, '', window.location.pathname + (query ? '?' + query : ''));
  }

  window.updateChartsIntervalFilter = function () {
    var interval = document.getElementById('interval').value;
    var customDates = document.getElementById('chartsCustomDates');
    var groupByContainer = document.getElementById('groupByContainer');

    groupByContainer.style.display = (interval === 'today' || interval === 'day') ? 'none' : 'flex';

    if (interval === 'custom') {
      customDates.style.display = 'flex';
      return;   //< wait for both custom dates before fetching
    }
    customDates.style.display = 'none';
    window.updateChartsFilters();
  };

  window.updateChartsDateFilter = function () {
    var startEl = document.getElementById('startDate');
    var endEl = document.getElementById('endDate');
    var errorEl = document.getElementById('dateError');
    var startDate = startEl.value, endDate = endEl.value;

    errorEl.style.display = 'none';
    startEl.style.borderColor = '';
    endEl.style.borderColor = '';

    if (startDate && endDate) {
      if (new Date(startDate) > new Date(endDate)) {
        errorEl.textContent = 'Start date cannot be after end date.';
        errorEl.style.display = 'block';
        startEl.style.borderColor = 'var(--accent)';
        endEl.style.borderColor = 'var(--accent)';
        return;
      }
      window.updateChartsFilters(true);
    }
  };

  window.updateChartsFilters = function (forceCustom) {
    var interval = document.getElementById('interval').value;
    var groupBy = document.getElementById('groupBy').value;

    //< Auto ("") drops the param so the server derives the bucket from the
    //  range span - pinning the derived value would freeze auto mode
    function setGroupBy(params) {
      if (groupBy) {
        params.set('groupBy', groupBy);
      } else {
        params.delete('groupBy');
      }
    }

    if (interval === 'custom' || forceCustom) {
      var startDate = document.getElementById('startDate').value;
      var endDate = document.getElementById('endDate').value;
      if (!startDate || !endDate) {
        return;
      }
      replaceChartsUrl(function (params) {
        setGroupBy(params);
        params.set('interval', 'custom');
        params.set('startDate', startDate);
        params.set('endDate', endDate);
      });
    } else {
      replaceChartsUrl(function (params) {
        setGroupBy(params);
        params.set('interval', interval);
        params.delete('startDate');
        params.delete('endDate');
      });
    }
    loadChartsData();
  };

  window.addEventListener('popstate', function () {
    var params = new URLSearchParams(window.location.search);
    var hasCustom = params.get('startDate') && params.get('endDate');
    //< a bare URL (no ?interval) means the server default window, not 'day'
    var interval = hasCustom ? 'custom' : (params.get('interval') || window.__chartsDefaultInterval || 'day');
    document.getElementById('interval').value = interval;
    document.getElementById('startDate').value = params.get('startDate') || '';
    document.getElementById('endDate').value = params.get('endDate') || '';
    document.getElementById('chartsCustomDates').style.display = (interval === 'custom') ? 'flex' : 'none';
    document.getElementById('groupBy').value = params.get('groupBy') || '';   //< bare URL = Auto
    document.getElementById('groupByContainer').style.display =
      (interval === 'today' || interval === 'day') ? 'none' : 'flex';
    loadChartsData();
  });

  loadChartsData({ initial: true });
})();
