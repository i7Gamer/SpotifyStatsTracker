/* Genres page charts + AJAX genre switching. Reads window.__genreData (set
 * inline by genres.html) and draws with the shared window.ChartUtils primitives
 * (chart-utils.js, loaded first). Switching the drill-down genre fetches just
 * the detail partial + its two chart datasets and swaps them in place - no full
 * page reload. Self-contained, no external dependencies. */
(function () {
  var CU = window.ChartUtils;
  if (!CU) return;

  var GENRE_LABEL_CHAR_PX = 7;   //< approx px per char, to fit a long genre label into its bar slot
  var MIN_GENRE_LABEL_CHARS = 4;

  function fitGenreLabel(key, slotWidth) {
    var maxChars = Math.max(MIN_GENRE_LABEL_CHARS, Math.floor(slotWidth / GENRE_LABEL_CHAR_PX));
    return key.length > maxChars ? key.slice(0, maxChars - 1) + '…' : key;
  }

  // ---- Overview charts (unchanged when the drill-down genre switches) -------

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

  var GENRE_BREADTH_LIMIT = 8;   //< keep the horizontal breadth chart to a readable height

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

  // ---- AJAX genre switching ------------------------------------------------

  var loadToken = 0;

  function setSelectedChip(genre) {
    document.querySelectorAll('.genre-chip').forEach(function (chip) {
      chip.classList.toggle('selected', chip.getAttribute('data-genre') === genre);
    });
  }

  function loadGenre(genre, chipHref, push) {
    var token = ++loadToken;
    var detail = document.getElementById('genreDetail');
    if (detail) detail.classList.add('is-loading');
    fetch('/genres?genre=' + encodeURIComponent(genre) + '&ajax=true', {
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    })
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) {
        if (token !== loadToken) return;   //< a newer click superseded this one
        if (!data || !data.ok) {
          // Fall back to a real navigation if the swap can't be served.
          window.location.href = chipHref || ('/genres?genre=' + encodeURIComponent(genre));
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
          history.pushState({ genre: data.genre }, '', '/genres?genre=' + encodeURIComponent(data.genre));
        }
      })
      .catch(function () {
        if (token !== loadToken) return;
        window.location.href = chipHref || ('/genres?genre=' + encodeURIComponent(genre));
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
    loadGenre(genre, chip.getAttribute('href'), true);
  }

  var chipRow = document.querySelector('.genre-chip-row');
  if (chipRow) {
    chipRow.addEventListener('click', onChipClick);
  }

  // Back/forward within the page: re-swap to the genre in the URL without
  // pushing a new entry. A full navigation (e.g. the detail pages' Back button)
  // reloads /genres server-side instead and never reaches this.
  window.addEventListener('popstate', function () {
    var params = new URLSearchParams(window.location.search);
    var genre = params.get('genre');
    var selected = document.querySelector('.genre-chip.selected');
    if (genre && (!selected || selected.getAttribute('data-genre') !== genre)) {
      loadGenre(genre, null, false);
    }
  });

  renderAll();

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
