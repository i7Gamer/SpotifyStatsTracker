/* Hand-rolled canvas charts for the /charts + /compare pages - no external
 * dependencies, so the app stays self-contained for offline/Docker use. Reads
 * data from window.__chartData, set inline by charts.html/compare.html before
 * this script loads.
 *
 * The generic canvas primitives (palette, setupCanvas, accent, tooltip, empty
 * state, axis grid, sparse x-labels, donut, multi-line, categorical bars) live
 * in static/js/chart-utils.js (window.ChartUtils), loaded first and shared with
 * the Genres page. Only the charts-only pieces (time-series bars, heatmap, the
 * Compare mirror, and the ms/padding label helpers those need) stay here. */
(function () {
  var CU = window.ChartUtils;
  // Local aliases for the shared primitives so the charts-only helpers below
  // read the same as before the extraction.
  var PALETTE = CU.PALETTE;
  var getAccentColor = CU.getAccentColor;
  var parseHex = CU.parseHex;
  var escapeHtml = CU.escapeHtml;
  var setupCanvas = CU.setupCanvas;
  var showTooltip = CU.showTooltip;
  var hideTooltip = CU.hideTooltip;
  var drawEmptyState = CU.drawEmptyState;
  var drawYAxisGrid = CU.drawYAxisGrid;
  var drawSparseXLabels = CU.drawSparseXLabels;

  // Consts still needed by the charts-only helpers (yAxisPaddingLeft, the
  // time-series/mirror label spacing). They mirror the same-named constants in
  // chart-utils.js - GRID_LINE_COUNT in particular must match the grid drawn by
  // ChartUtils.drawYAxisGrid so yAxisPaddingLeft sizes for the labels actually rendered.
  var GRID_LINE_COUNT = 4;
  var MIN_AXIS_LABEL_SPACING_PX = 70;
  var Y_AXIS_LABEL_FONT = '11px sans-serif';
  var Y_AXIS_LABEL_GAP_PX = 8;     //< space between a y-axis label's right edge and the axis line
  var Y_AXIS_MIN_PADDING_PX = 34;  //< floor so narrow labels (e.g. "0m") still get consistent left padding

  function msToShortLabel(ms) {
    if (!ms) {
      return '0m';
    }
    var totalMinutes = Math.round(ms / 60000);
    var hours = Math.floor(totalMinutes / 60);
    var minutes = totalMinutes % 60;
    if (hours > 0) {
      return hours + 'h' + (minutes ? minutes + 'm' : '');
    }
    return minutes + 'm';
  }

  // A fixed left padding either wastes space (short labels like "45m") or
  // clips the widest one off the left edge (long ones like "150h30m" - the
  // longer the time-listened axis's labels, the more room they need). Size
  // it to what the grid's own labels will actually render as, sampling the
  // same GRID_LINE_COUNT fractions drawYAxisGrid draws.
  function yAxisPaddingLeft(ctx, maxValue, formatLabel) {
    var prevFont = ctx.font;
    ctx.font = Y_AXIS_LABEL_FONT;
    var maxWidth = 0;
    for (var i = 0; i <= GRID_LINE_COUNT; i++) {
      var width = ctx.measureText(formatLabel(maxValue * i / GRID_LINE_COUNT)).width;
      if (width > maxWidth) {
        maxWidth = width;
      }
    }
    ctx.font = prevFont;
    return Math.max(Y_AXIS_MIN_PADDING_PX, maxWidth + Y_AXIS_LABEL_GAP_PX * 2);
  }

  function renderTimeSeriesChart() {
    var canvas = document.getElementById('timeSeriesChart');
    if (!canvas) {
      return;
    }
    var data = (window.__chartData && window.__chartData.timeSeries) || [];
    var interval = (window.__chartData && window.__chartData.interval) || '';
    var isLastDay = interval === 'day';
    var setup = setupCanvas(canvas, 260);
    var ctx = setup.ctx, width = setup.width, height = setup.height;
    ctx.clearRect(0, 0, width, height);

    if (data.length === 0) {
      drawEmptyState(ctx, width, height, 'No listening data in this period yet.');
      return;
    }

    var maxMs = Math.max(1, Math.max.apply(null, data.map(function (d) { return d.totalTimeListened; })));
    var paddingLeft = yAxisPaddingLeft(ctx, maxMs, msToShortLabel), paddingBottom = 26, paddingTop = 16, paddingRight = 16;
    var plotWidth = width - paddingLeft - paddingRight;
    var plotHeight = height - paddingTop - paddingBottom;
    var slotWidth = plotWidth / data.length;
    var barGap = 4;
    var barWidth = Math.max(2, slotWidth - barGap);

    drawYAxisGrid(ctx, paddingLeft, paddingTop, plotWidth, plotHeight, maxMs, msToShortLabel);

    var bars = data.map(function (d, i) {
      var x = paddingLeft + i * slotWidth + barGap / 2;
      var barHeight = plotHeight * (d.totalTimeListened / maxMs);
      var y = paddingTop + plotHeight - barHeight;
      ctx.fillStyle = PALETTE[0];
      ctx.fillRect(x, y, barWidth, barHeight);
      return { x: x, width: barWidth, d: d, hourIndex: i };
    });

    var labels = isLastDay
      ? data.map(function (d) { return d.label.split(' ')[1]; })  // Extract "HH:00" from "YYYY-MM-DD HH:00"
      : data.map(function (d) { return d.label; });
    var labelSpacing = isLastDay ? 30 : MIN_AXIS_LABEL_SPACING_PX;
    drawSparseXLabels(ctx, labels, paddingLeft, plotWidth, plotHeight, paddingTop, function (i) {
      return paddingLeft + i * slotWidth + slotWidth / 2;
    }, labelSpacing);

    function findBarAt(mx, my) {
      for (var i = 0; i < bars.length; i++) {
        var b = bars[i];
        if (mx >= b.x && mx <= b.x + b.width && my >= paddingTop && my <= paddingTop + plotHeight) {
          return b;
        }
      }
      return null;
    }

    canvas.onmousemove = function (evt) {
      var rect = canvas.getBoundingClientRect();
      var mx = evt.clientX - rect.left, my = evt.clientY - rect.top;
      var hit = findBarAt(mx, my);
      if (hit) {
        var label = isLastDay ? hit.d.label.split(' ')[1] : hit.d.label;
        showTooltip(evt, '<strong>' + label + '</strong><br>' + (hit.d.totalTimeListenedText || '0s') + ' &middot; ' + hit.d.plays + ' plays');
        // rangeStart is only stamped for buckets with a clean calendar-date
        // mapping (see app.py's _timeSeriesBucketRange) - the single-day
        // view's hourly buckets don't get one, so they stay un-clickable.
        canvas.style.cursor = hit.d.rangeStart ? 'pointer' : 'crosshair';
      } else {
        hideTooltip();
        canvas.style.cursor = 'crosshair';
      }
    };
    canvas.onmouseleave = function () {
      hideTooltip();
      canvas.style.cursor = 'crosshair';
    };
    // Clicking a bar scopes the Dashboard's stats and play list to that
    // exact bucket's date range - see app.py's dashboard() route, which
    // only applies list-filtering for an explicit interval=custom range
    // (not the named day/week/month intervals).
    canvas.onclick = function (evt) {
      var rect = canvas.getBoundingClientRect();
      var mx = evt.clientX - rect.left, my = evt.clientY - rect.top;
      var hit = findBarAt(mx, my);
      if (hit && hit.d.rangeStart && hit.d.rangeEnd) {
        window.location.href = '/?interval=custom&startDate=' + encodeURIComponent(hit.d.rangeStart) +
          '&endDate=' + encodeURIComponent(hit.d.rangeEnd);
      }
    };
  }

  function heatColor(intensity) {
    var clamped = Math.max(0, Math.min(1, intensity));
    if (clamped === 0) {
      return 'rgba(255,255,255,0.05)';
    }
    var accent = getAccentColor();
    var rgb = parseHex(accent);
    var r = Math.round(30 + (rgb.r - 30) * clamped);
    var g = Math.round(30 + (rgb.g - 30) * clamped);
    var b = Math.round(30 + (rgb.b - 30) * clamped);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }

  function renderHeatmap() {
    var canvas = document.getElementById('heatmapChart');
    if (!canvas) {
      return;
    }
    var grid = (window.__chartData && window.__chartData.heatmap) || [];
    var rows = grid.length;
    var cols = rows ? grid[0].length : 24;
    var cellHeight = 26;
    var cssHeight = rows * cellHeight + 34;
    var setup = setupCanvas(canvas, cssHeight);
    var ctx = setup.ctx, width = setup.width;
    ctx.clearRect(0, 0, width, cssHeight);

    if (rows === 0) {
      drawEmptyState(ctx, width, cssHeight, 'No listening data in this period yet.');
      return;
    }

    var paddingLeft = 40, paddingTop = 6;
    var plotWidth = width - paddingLeft - 10;
    var cellWidth = plotWidth / cols;
    var dayLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

    var maxMs = 1;
    for (var r = 0; r < rows; r++) {
      for (var c = 0; c < cols; c++) {
        maxMs = Math.max(maxMs, grid[r][c].totalTimeListened);
      }
    }

    ctx.font = '10px sans-serif';
    var cells = [];
    for (r = 0; r < rows; r++) {
      ctx.fillStyle = '#b0b0b0';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText(dayLabels[r], paddingLeft - 8, paddingTop + r * cellHeight + cellHeight / 2);
      for (c = 0; c < cols; c++) {
        var cell = grid[r][c];
        var x = paddingLeft + c * cellWidth;
        var y = paddingTop + r * cellHeight;
        ctx.fillStyle = heatColor(cell.totalTimeListened / maxMs);
        ctx.fillRect(x, y, Math.max(1, cellWidth - 2), cellHeight - 2);
        cells.push({ x: x, y: y, width: cellWidth - 2, height: cellHeight - 2, cell: cell, day: dayLabels[r], hour: c });
      }
    }

    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillStyle = '#b0b0b0';
    for (c = 0; c < cols; c += 3) {
      var lx = paddingLeft + c * cellWidth + cellWidth / 2;
      ctx.fillText(String(c).padStart ? String(c).padStart(2, '0') : ('0' + c).slice(-2), lx, paddingTop + rows * cellHeight + 6);
    }

    canvas.onmousemove = function (evt) {
      var rect = canvas.getBoundingClientRect();
      var mx = evt.clientX - rect.left, my = evt.clientY - rect.top;
      var hit = null;
      for (var i = 0; i < cells.length; i++) {
        var cl = cells[i];
        if (mx >= cl.x && mx <= cl.x + cl.width && my >= cl.y && my <= cl.y + cl.height) {
          hit = cl;
          break;
        }
      }
      if (hit) {
        var hourLabel = (hit.hour < 10 ? '0' : '') + hit.hour;
        showTooltip(evt, '<strong>' + hit.day + ' ' + hourLabel + ':00</strong><br>' + (hit.cell.totalTimeListenedText || '0s') + ' &middot; ' + hit.cell.plays + ' plays');
      } else {
        hideTooltip();
      }
    };
    canvas.onmouseleave = hideTooltip;
  }

  function renderArtistTrend() {
    CU.renderMultiLineChart(
      document.getElementById('artistTrendChart'),
      document.getElementById('artistTrendLegend'),
      (window.__chartData && window.__chartData.artistTrend) || { buckets: [], series: [] },
      {
        emptyMessage: 'Not enough data yet to show an artist trend.',
        formatValue: function (v) { return Math.round(v) + ' plays'; },
        // Clicking a line/point navigates to that artist's detail page - see
        // app.py's getArtistTrend, which picks a representative id for
        // same-named artists sharing one merged line.
        onClickId: function (id) { window.location.href = '/artist/' + encodeURIComponent(id); }
      }
    );
  }

  // Compare page: two users' listening time as a MIRRORED area chart - the
  // viewer's series filled above a center baseline, the counterpart's
  // mirrored below it. Replaces a two-line overlay where similar series sat
  // on top of each other and only one stayed visible; mirrored halves can't
  // overlap, and both halves share one symmetric scale so their areas stay
  // comparable. Values are milliseconds listened per bucket.
  var MIRROR_DOT_MAX_BUCKETS = 60;   //< point markers only while they don't smear into a thick line

  function withAlpha(hexColor, alpha) {
    var rgb = parseHex(hexColor);
    return 'rgba(' + rgb.r + ',' + rgb.g + ',' + rgb.b + ',' + alpha + ')';
  }

  // The counterpart's identity color, shared with the CSS (--compare-theirs
  // drives the split bars, table headers, and column headings) so the chart
  // series can never drift from the rest of the page.
  function getTheirsColor() {
    var value = getComputedStyle(document.documentElement).getPropertyValue('--compare-theirs').trim();
    return value || PALETTE[1];
  }

  function renderComparisonMirror() {
    var canvas = document.getElementById('comparisonTrendChart');
    var legendEl = document.getElementById('comparisonTrendLegend');
    if (!canvas) {
      return;
    }
    var data = (window.__chartData && window.__chartData.comparisonTrend) || { buckets: [], series: [] };
    var setup = setupCanvas(canvas, 320);
    var ctx = setup.ctx, width = setup.width, height = setup.height;
    ctx.clearRect(0, 0, width, height);

    if (!data.buckets.length || data.series.length < 2) {
      drawEmptyState(ctx, width, height, 'No listening data in this period yet.');
      if (legendEl) {
        legendEl.innerHTML = '';
      }
      return;
    }

    var maxMs = 1;
    data.series.forEach(function (s) {
      maxMs = Math.max(maxMs, Math.max.apply(null, s.data));
    });
    var paddingLeft = yAxisPaddingLeft(ctx, maxMs, msToShortLabel), paddingBottom = 26, paddingTop = 16, paddingRight = 16;
    var plotWidth = width - paddingLeft - paddingRight;
    var plotHeight = height - paddingTop - paddingBottom;
    var half = plotHeight / 2;
    var midY = paddingTop + half;
    var stepX = data.buckets.length > 1 ? plotWidth / (data.buckets.length - 1) : 0;
    var colors = [PALETTE[0], getTheirsColor()];

    // Hour buckets are "YYYY-MM-DD HH:00" - drawSparseXLabels' 7-char slice
    // would render them all as the same date prefix, so show the time part
    // (like renderTimeSeriesChart's single-day handling).
    var isHourly = data.buckets[0].indexOf(' ') > -1;
    var axisLabels = isHourly
      ? data.buckets.map(function (b) { return b.split(' ')[1]; })
      : data.buckets;
    var labelSpacing = isHourly ? 42 : MIN_AXIS_LABEL_SPACING_PX;

    // The whole frame is drawn by one closure so the hover crosshair can
    // redraw cleanly whenever the highlighted bucket changes.
    function draw(highlightIdx) {
      ctx.clearRect(0, 0, width, height);

      // Symmetric grid: time labels at 50%/100% above and below the baseline.
      ctx.font = Y_AXIS_LABEL_FONT;
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      [0.5, 1].forEach(function (fraction) {
        [-1, 1].forEach(function (direction) {
          var y = midY - direction * half * fraction;
          ctx.strokeStyle = 'rgba(255,255,255,0.08)';
          ctx.beginPath();
          ctx.moveTo(paddingLeft, y);
          ctx.lineTo(paddingLeft + plotWidth, y);
          ctx.stroke();
          ctx.fillStyle = '#b0b0b0';
          ctx.fillText(msToShortLabel(maxMs * fraction), paddingLeft - Y_AXIS_LABEL_GAP_PX, y);
        });
      });
      // stronger center baseline separating the two users
      ctx.strokeStyle = 'rgba(255,255,255,0.25)';
      ctx.beginPath();
      ctx.moveTo(paddingLeft, midY);
      ctx.lineTo(paddingLeft + plotWidth, midY);
      ctx.stroke();
      ctx.fillStyle = '#b0b0b0';
      ctx.fillText('0', paddingLeft - Y_AXIS_LABEL_GAP_PX, midY);

      drawSparseXLabels(ctx, axisLabels, paddingLeft, plotWidth, plotHeight, paddingTop, function (i) {
        return paddingLeft + i * stepX;
      }, labelSpacing);

      // vertical crosshair under the areas, aligning both halves' values
      if (highlightIdx !== null) {
        var hx = paddingLeft + highlightIdx * stepX;
        ctx.strokeStyle = 'rgba(255,255,255,0.35)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(hx, paddingTop);
        ctx.lineTo(hx, paddingTop + plotHeight);
        ctx.stroke();
      }

      data.series.slice(0, 2).forEach(function (series, si) {
        var direction = si === 0 ? 1 : -1;   //< first series up, second mirrored down
        var color = colors[si];
        var points = series.data.map(function (v, i) {
          return { x: paddingLeft + i * stepX, y: midY - direction * (half * v / maxMs) };
        });

        ctx.fillStyle = withAlpha(color, 0.3);
        ctx.beginPath();
        ctx.moveTo(points[0].x, midY);
        points.forEach(function (p) { ctx.lineTo(p.x, p.y); });
        ctx.lineTo(points[points.length - 1].x, midY);
        ctx.closePath();
        ctx.fill();

        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        points.forEach(function (p, i) {
          if (i === 0) {
            ctx.moveTo(p.x, p.y);
          } else {
            ctx.lineTo(p.x, p.y);
          }
        });
        ctx.stroke();

        if (points.length <= MIRROR_DOT_MAX_BUCKETS) {
          ctx.fillStyle = color;
          points.forEach(function (p) {
            ctx.beginPath();
            ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
            ctx.fill();
          });
        }

        // highlighted bucket gets an emphasized marker on both series
        if (highlightIdx !== null) {
          var hp = points[highlightIdx];
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(hp.x, hp.y, 4.5, 0, Math.PI * 2);
          ctx.fill();
        }
      });
    }

    draw(null);
    var highlightedIdx = null;

    canvas.onmousemove = function (evt) {
      var rect = canvas.getBoundingClientRect();
      var mx = evt.clientX - rect.left, my = evt.clientY - rect.top;
      if (mx < paddingLeft || mx > paddingLeft + plotWidth || my < paddingTop || my > paddingTop + plotHeight) {
        if (highlightedIdx !== null) {
          highlightedIdx = null;
          draw(null);
        }
        hideTooltip();
        return;
      }
      var idx = stepX > 0 ? Math.round((mx - paddingLeft) / stepX) : 0;
      idx = Math.max(0, Math.min(data.buckets.length - 1, idx));
      if (idx !== highlightedIdx) {
        highlightedIdx = idx;
        draw(idx);
      }
      var rows = data.series.slice(0, 2).map(function (series, si) {
        return '<span style="color:' + colors[si] + '">&#9679;</span> ' +
          escapeHtml(series.name) + ': ' + msToShortLabel(series.data[idx]);
      });
      showTooltip(evt, '<strong>' + data.buckets[idx] + '</strong><br>' + rows.join('<br>'));
    };
    canvas.onmouseleave = function () {
      if (highlightedIdx !== null) {
        highlightedIdx = null;
        draw(null);
      }
      hideTooltip();
    };

    if (legendEl) {
      legendEl.innerHTML = data.series.slice(0, 2).map(function (series, si) {
        return '<span class="chart-legend-item"><span class="chart-legend-swatch" style="background:' + colors[si] + '"></span>' + escapeHtml(series.name) + '</span>';
      }).join('');
    }
  }

  function renderExplicitChart() {
    var canvas = document.getElementById('explicitChart');
    if (!canvas) return;
    var data = window.__chartData.explicitRatio;
    if (!data) return;

    var slices = [
      { label: 'Explicit', value: data.explicit, color: getAccentColor() },
      { label: 'Clean', value: data.clean, color: '#5AC8FA' }
    ];

    CU.drawDonutChart(canvas, slices, data.explicit + data.clean,
      { height: 250, showLabels: true, emptyMessage: 'No listening history in this period.' });
  }

  function renderCompletionChart() {
    var canvas = document.getElementById('completionChart');
    if (!canvas) return;
    var data = window.__chartData.completionStats;
    if (!data) return;

    var slices = [
      { label: 'Completed', value: data.completes, color: getAccentColor() },
      { label: 'Partial', value: data.partials, color: '#5DD97C' },
      { label: 'Skipped', value: data.skips, color: '#5AC8FA' }
    ];

    CU.drawDonutChart(canvas, slices, data.skips + data.completes + data.partials,
      { height: 250, showLabels: true, emptyMessage: 'No listening history in this period.' });
  }

  function renderDecadeChart() {
    CU.renderBarsFromPairs(document.getElementById('decadeChart'),
      window.__chartData.decadeDistribution,
      { emptyMessage: 'No album release information in this period.' });
  }

  function renderGenreChart() {
    // Genre names run long ("progressive electronic") - fit the axis label
    // to the bar slot.
    CU.renderBarsFromPairs(document.getElementById('genreChart'),
      window.__chartData.genreDistribution,
      {
        emptyMessage: 'No genre data for the plays in this period.',
        fitLabel: function (key, rawBarWidth) {
          var maxLabelChars = Math.max(4, Math.floor(rawBarWidth / 7));
          return key.length > maxLabelChars ? key.slice(0, maxLabelChars - 1) + '…' : key;
        }
      });
  }

  function renderAllCharts() {
    PALETTE[0] = getAccentColor();
    renderTimeSeriesChart();
    renderHeatmap();
    renderArtistTrend();
    renderComparisonMirror();
    renderExplicitChart();
    renderCompletionChart();
    renderDecadeChart();
    renderGenreChart();
  }

  window.renderTimeSeriesChart = renderTimeSeriesChart;
  // The Compare page re-renders the mirror chart after AJAX filter swaps.
  window.renderComparisonMirror = renderComparisonMirror;

  renderAllCharts();

  var resizeTimer;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderAllCharts, 150);
  });

  var themeSelector = document.getElementById('theme-selector');
  if (themeSelector) {
    themeSelector.addEventListener('change', function () {
      setTimeout(renderAllCharts, 50);
    });
  }
})();
