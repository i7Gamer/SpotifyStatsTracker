/* Genres page charts. Reads window.__genreData (set inline by genres.html) and
 * draws them with the shared window.ChartUtils primitives (chart-utils.js,
 * loaded first). Self-contained, no external dependencies. */
(function () {
  var CU = window.ChartUtils;
  if (!CU) return;

  var GENRE_LABEL_CHAR_PX = 7;   //< approx px per char, to fit a long genre label into its bar slot
  var MIN_GENRE_LABEL_CHARS = 4;

  function fitGenreLabel(key, slotWidth) {
    var maxChars = Math.max(MIN_GENRE_LABEL_CHARS, Math.floor(slotWidth / GENRE_LABEL_CHAR_PX));
    return key.length > maxChars ? key.slice(0, maxChars - 1) + '…' : key;
  }

  function renderDistribution() {
    var canvas = document.getElementById('genreDistChart');
    var pairs = (window.__genreData && window.__genreData.distributionPairs) || [];
    CU.renderBarsFromPairs(canvas, pairs, {
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
    // Cap the number of coloured slices to the palette; fold the rest into a
    // single muted "Other" slice so the donut stays readable.
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
  }

  function renderMix() {
    var canvas = document.getElementById('genreMixChart');
    var legend = document.getElementById('genreMixLegend');
    var data = (window.__genreData && window.__genreData.mixTrend) || { buckets: [], series: [] };
    CU.renderMultiLineChart(canvas, legend, data, {
      emptyMessage: 'Not enough data yet to show a genre trend.',
      formatValue: function (v) { return Math.round(v) + ' plays'; }
    });
  }

  function renderSelectedTrend() {
    var canvas = document.getElementById('genreTrendChart');
    var data = (window.__genreData && window.__genreData.selectedTrend) || { buckets: [], series: [] };
    CU.renderMultiLineChart(canvas, null, data, {
      emptyMessage: 'Not enough data yet for this genre.',
      formatValue: function (v) { return Math.round(v) + ' plays'; }
    });
  }

  function renderAll() {
    CU.PALETTE[0] = CU.getAccentColor();
    renderDistribution();
    renderShare();
    renderMix();
    renderSelectedTrend();
  }

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
