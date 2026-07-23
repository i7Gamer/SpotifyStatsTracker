/* Genres page: fetches its data after first paint (the initial GET is just a
 * shell), draws with the shared window.ChartUtils primitives (chart-utils.js,
 * loaded first), and refreshes on filter/genre changes without a full reload.
 *
 * Two AJAX shapes, both against /genres:
 *   - full (?ajax=true): overview datasets + chip row + the selected genre's
 *     detail - fetched on load and on every time-period filter change.
 *   - detail (?ajax=true&scope=detail): just the drill-down partial + its two
 *     chart datasets - fetched on a chip click.
 * All data is scoped to the selected time window; the URL carries interval/
 * date/genre so Back/Forward and refresh reproduce the view. Self-contained. */
(function () {
  var CU = window.ChartUtils;
  if (!CU) return;

  var bootstrapEl = document.getElementById('genres-bootstrap');
  var bootstrap = bootstrapEl ? JSON.parse(bootstrapEl.textContent) : {};
  window.__genreData = window.__genreData || {};

  var GENRE_LABEL_CHAR_PX = 7;   //< approx px per char, to fit a long genre label into its bar slot
  var MIN_GENRE_LABEL_CHARS = 4;
  var GENRE_BREADTH_LIMIT = 8;   //< keep the horizontal breadth chart to a readable height

  function fitGenreLabel(key, slotWidth) {
    var maxChars = Math.max(MIN_GENRE_LABEL_CHARS, Math.floor(slotWidth / GENRE_LABEL_CHAR_PX));
    return key.length > maxChars ? key.slice(0, maxChars - 1) + '…' : key;
  }

  // ---- Overview charts (redrawn on each time-period change) -----------------

  function renderDistribution() {
    var pairs = (window.__genreData && window.__genreData.distributionPairs) || [];
    CU.renderBarsFromPairs(document.getElementById('genreDistChart'), pairs, {
      emptyMessage: 'No genre data yet.',
      fitLabel: fitGenreLabel,
      valueSuffix: ' plays'
    });
  }

  function renderShare() {
    var canvas = document.getElementById('genreShareChart');
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
    var legend = document.getElementById('genreShareLegend');
    if (legend) {
      legend.innerHTML = grandTotal ? slices.map(function (s) {
        return '<span class="chart-legend-item"><span class="chart-legend-swatch" style="background:' + s.color + '"></span>' +
          CU.escapeHtml(s.label) + '</span>';
      }).join('') : '';
    }
  }

  function renderBreadth() {
    var pairs = ((window.__genreData && window.__genreData.breadthPairs) || []).slice(0, GENRE_BREADTH_LIMIT);
    CU.renderHorizontalBars(document.getElementById('genreBreadthChart'), pairs, {
      emptyMessage: 'No genre data yet.',
      valueSuffix: ' artists'
    });
  }

  function renderMix() {
    CU.renderMultiLineChart(
      document.getElementById('genreMixChart'),
      document.getElementById('genreMixLegend'),
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
      document.getElementById('genreTrendChart'),
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
      document.getElementById('genreClockChart'),
      (window.__genreData && window.__genreData.clock) || [],
      { emptyMessage: 'No listening data for this genre yet.' }
    );
  }

  function renderDetailCharts() {
    renderSelectedTrend();
    renderClock();
  }

  function renderAll() {
    CU.refreshPalette();
    renderOverview();
    renderDetailCharts();
  }

  // ---- URL helpers ---------------------------------------------------------

  function pushGenresUrl(mutate) {
    var params = new URLSearchParams(window.location.search);
    mutate(params);
    params.delete('ajax');
    params.delete('scope');
    var query = params.toString();
    window.history.pushState({}, '', window.location.pathname + (query ? '?' + query : ''));
  }

  function setSelectedChip(genre) {
    document.querySelectorAll('.genre-chip').forEach(function (chip) {
      chip.classList.toggle('selected', chip.getAttribute('data-genre') === genre);
    });
  }

  //< a newer load supersedes an older one - a stale response then no-ops
  var loadToken = 0;

  // ---- Full data load (initial paint + time-period changes) ----------------

  function overviewFadeTargets() {
    return Array.prototype.slice.call(
      document.querySelectorAll('#genresOverview .chart-canvas-wrap'));
  }

  function loadGenresData(opts) {
    opts = opts || {};
    var initial = !!opts.initial;
    var token = ++loadToken;

    var overviewTargets = initial ? [] : overviewFadeTargets();
    var detail = document.getElementById('genreDetail');
    overviewTargets.forEach(function (t) { t.classList.add('loading-fade'); });
    if (!initial && detail) detail.classList.add('is-loading');

    var params = new URLSearchParams(window.location.search);
    params.set('ajax', 'true');
    params.delete('scope');
    fetch(window.location.pathname + '?' + params.toString(), {
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    })
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) {
        if (token !== loadToken) return;   //< superseded by a newer load
        //< ok:false should not happen once the shell rendered unlocked (the
        //  gate is all-time and stable) - leave the placeholders rather than
        //  risk a reload loop
        if (!data || !data.ok) return;

        window.__genreData.distributionPairs = data.distributionPairs;
        window.__genreData.breadthPairs = data.breadthPairs;
        window.__genreData.mixTrend = data.mixTrend;
        window.__genreData.selectedTrend = data.selectedTrend;
        window.__genreData.clock = data.clock;

        var chipRow = document.getElementById('genreChipRow');
        if (chipRow) chipRow.innerHTML = data.chipsHtml;
        if (detail) detail.innerHTML = data.detailHtml;
        var mixLabel = document.getElementById('genreMixLabel');
        if (mixLabel && data.intervalLabel) mixLabel.textContent = data.intervalLabel;

        renderAll();
      })
      .catch(function () { /* leave placeholders; token guard covers staleness */ })
      .finally(function () {
        if (token !== loadToken) return;
        overviewTargets.forEach(function (t) { t.classList.remove('loading-fade'); });
        if (detail) detail.classList.remove('is-loading');
      });
  }

  // ---- Chip-click drill-down swap (detail only) ----------------------------

  function detailFallbackUrl(genre) {
    var params = new URLSearchParams(window.location.search);
    params.set('genre', genre);
    params.delete('ajax');
    params.delete('scope');
    return window.location.pathname + '?' + params.toString();
  }

  function loadGenreDetail(genre, push) {
    var token = ++loadToken;
    var detail = document.getElementById('genreDetail');
    if (detail) detail.classList.add('is-loading');
    var params = new URLSearchParams(window.location.search);
    params.set('genre', genre);
    params.set('ajax', 'true');
    params.set('scope', 'detail');
    fetch(window.location.pathname + '?' + params.toString(), {
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    })
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) {
        if (token !== loadToken) return;
        if (!data || !data.ok) {
          window.location.href = detailFallbackUrl(genre);
          return;
        }
        window.__genreData.selectedTrend = data.selectedTrend;
        window.__genreData.clock = data.clock;
        if (detail) {
          detail.innerHTML = data.detailHtml;
          detail.classList.remove('is-loading');
        }
        setSelectedChip(data.genre);
        renderDetailCharts();
        if (push) {
          pushGenresUrl(function (p) { p.set('genre', data.genre); });
        }
      })
      .catch(function () {
        if (token !== loadToken) return;
        window.location.href = detailFallbackUrl(genre);
      });
  }

  function onChipClick(evt) {
    var chip = evt.target.closest('.genre-chip');
    if (!chip) return;
    // Let modified clicks (new tab, etc.) behave normally.
    if (evt.metaKey || evt.ctrlKey || evt.shiftKey || evt.altKey) return;
    var genre = chip.getAttribute('data-genre');
    if (!genre) return;
    evt.preventDefault();
    if (chip.classList.contains('selected')) return;   //< already showing this genre
    loadGenreDetail(genre, true);
  }

  // The chip row element persists across full loads (only its innerHTML is
  // swapped), so this delegated listener survives every refresh.
  var chipRow = document.getElementById('genreChipRow');
  if (chipRow) chipRow.addEventListener('click', onChipClick);

  // ---- Time-period filter handlers (globals for the inline onchange) -------

  window.updateGenresIntervalFilter = function () {
    var interval = document.getElementById('interval').value;
    var customDates = document.getElementById('genresCustomDates');
    if (interval === 'custom') {
      customDates.style.display = 'flex';
      return;   //< wait for both custom dates before fetching
    }
    customDates.style.display = 'none';
    // The selected genre is kept in the URL: the server keeps it when it still
    // has plays in the new range, else falls back to that range's top genre.
    pushGenresUrl(function (params) {
      params.set('interval', interval);
      params.delete('startDate');
      params.delete('endDate');
    });
    loadGenresData();
  };

  window.updateGenresDateFilter = function () {
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
      pushGenresUrl(function (params) {
        params.set('interval', 'custom');
        params.set('startDate', startDate);
        params.set('endDate', endDate);
      });
      loadGenresData();
    }
  };

  // Back/forward: reconcile the filter controls with the URL, then reload the
  // full payload (which resolves the genre from the URL too).
  window.addEventListener('popstate', function () {
    var params = new URLSearchParams(window.location.search);
    var hasCustom = params.get('startDate') && params.get('endDate');
    var interval = hasCustom ? 'custom' : (params.get('interval') || bootstrap.defaultWindow || 'day');
    var intervalEl = document.getElementById('interval');
    if (intervalEl) intervalEl.value = interval;
    var startEl = document.getElementById('startDate');
    var endEl = document.getElementById('endDate');
    if (startEl) startEl.value = params.get('startDate') || '';
    if (endEl) endEl.value = params.get('endDate') || '';
    var customDates = document.getElementById('genresCustomDates');
    if (customDates) customDates.style.display = (interval === 'custom') ? 'flex' : 'none';
    loadGenresData();
  });

  loadGenresData({ initial: true });

  var resizeTimer;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderAll, 150);
  });

  var themeSelector = document.getElementById('theme-selector');
  if (themeSelector) {
    themeSelector.addEventListener('change', function () {
      setTimeout(renderAll, 50);
    });
  }
})();
