// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* Shared hand-rolled canvas-chart primitives (no external dependencies, so the
 * app stays self-contained for offline/Docker use). Exposed as window.ChartUtils
 * and consumed by charts.js (/charts + /compare, plus the Wrapped and detail
 * time series) and genres.js, which keep only the charts each page draws
 * itself. Exported constants are read by charts.js rather than restated there,
 * so an axis sized here and a grid drawn here cannot drift apart. */
(function () {
  // Fallback if a theme's --chart-N vars are ever missing (e.g. stale cached CSS).
  var FALLBACK_PALETTE = ['#FB717B', '#5DD97C', '#5AC8FA', '#FFD166', '#C77DFF', '#FF9F45'];
  // Must match the --chart-1..--chart-N custom properties defined per theme in
  // style.css (html.theme-rose/green/purple/red) - one color per chart category
  // so multi-bar/slice charts (genre distribution, breadth, share donut) get a
  // distinct, theme-appropriate color for every category instead of cycling
  // through a handful of colors that repeat and never adapt to the theme.
  var CHART_COLOR_VAR_COUNT = 12;
  //< a copy, not the fallback itself: refreshPalette mutates PALETTE in place
  var PALETTE = FALLBACK_PALETTE.slice();
  var GRID_LINE_COUNT = 4;
  var MIN_AXIS_LABEL_SPACING_PX = 70;
  var Y_AXIS_LABEL_FONT = '11px sans-serif';
  var Y_AXIS_LABEL_GAP_PX = 8;     //< space between a y-axis label's right edge and the axis line
  var RESIZE_REDRAW_MS = 150;       //< coalesce a drag-resize into one repaint
  var THEME_REDRAW_MS = 50;         //< let the new theme's CSS variables land first

  // Re-reads --chart-1..--chart-N from the current theme into PALETTE in place
  // (mutated, not reassigned, so existing `CU.PALETTE` references stay live).
  // Call whenever the theme may have changed, before rendering charts.
  function refreshPalette() {
    var style = getComputedStyle(document.documentElement);
    var colors = [];
    for (var i = 1; i <= CHART_COLOR_VAR_COUNT; i++) {
      var value = style.getPropertyValue('--chart-' + i).trim();
      if (value) colors.push(value);
    }
    if (!colors.length) colors = FALLBACK_PALETTE;
    PALETTE.length = 0;
    Array.prototype.push.apply(PALETTE, colors);
    return PALETTE;
  }
  // Guarded so the module can be required under node (no document) for unit
  // tests of the pure helpers; in the browser this still primes the palette.
  if (typeof document !== 'undefined') {
    refreshPalette();
  }

  // The two repaints that are not swaps, bound once per page for its renderer.
  // The canvas reads its colours from the CSS variables at paint time, so a
  // theme change needs an explicit repaint - after a short wait for the new
  // variables to land, or it reads the OLD ones. 'themechange' is what
  // chrome-common.js raises when another tab switches the theme, the only way
  // it can change under a chart; both pages once listened on '#theme-selector'
  // instead, an element that exists only on /profile, a page with no charts,
  // so it had never fired.
  function bindRepaint(repaint) {
    var resizeTimer;
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(repaint, RESIZE_REDRAW_MS);
    });
    window.addEventListener('themechange', function () {
      setTimeout(repaint, THEME_REDRAW_MS);
    });
  }

  // Where clicking a time-series bucket takes the user: the play-history list,
  // scoped to that bucket's exact date range. It has to be /history (not '/'),
  // because the list moved off the dashboard - and an explicit custom range is
  // the only interval historyPage scopes the list by (named ones don't).
  function bucketDrilldownUrl(rangeStart, rangeEnd) {
    return '/history?interval=custom&startDate=' + encodeURIComponent(rangeStart) +
      '&endDate=' + encodeURIComponent(rangeEnd);
  }

  function getAccentColor() {
    var accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
    return accent || PALETTE[0];
  }

  // Genre/artist/track names come from the user's own imported data and aren't
  // guaranteed HTML-safe - escape before splicing into an innerHTML string.
  // Escaped in one pass so the "&" of an entity this just produced is never
  // re-escaped. Plain string work, not the old textContent/innerHTML
  // round-trip, so everything built on it (legendHtml, the tooltip bodies)
  // runs under plain node - see tests/test_chart_skip_series.js.
  var HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function escapeHtml(str) {
    return String(str == null ? '' : str).replace(/[&<>"']/g, function (ch) {
      return HTML_ESCAPES[ch];
    });
  }

  function parseHex(hex) {
    hex = (hex || '').replace(/^#/, '');
    if (hex.length === 3) {
      hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
    }
    var num = parseInt(hex, 16);
    if (isNaN(num)) {
      return { r: 251, g: 113, b: 123 };
    }
    return { r: (num >> 16) & 255, g: (num >> 8) & 255, b: num & 255 };
  }

  function setupCanvas(canvas, cssHeight) {
    var dpr = window.devicePixelRatio || 1;
    var width = Math.max(canvas.parentElement.getBoundingClientRect().width, 280);
    canvas.style.width = width + 'px';
    canvas.style.height = cssHeight + 'px';
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(cssHeight * dpr);
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, width: width, height: cssHeight };
  }

  function ensureTooltip() {
    var tooltip = document.getElementById('chartTooltip');
    if (!tooltip) {
      tooltip = document.createElement('div');
      tooltip.id = 'chartTooltip';
      tooltip.className = 'chart-tooltip';
      document.body.appendChild(tooltip);
    }
    return tooltip;
  }

  function showTooltip(evt, html) {
    var tooltip = ensureTooltip();
    tooltip.innerHTML = html;
    tooltip.style.left = (evt.clientX + 14) + 'px';
    tooltip.style.top = (evt.clientY + 14) + 'px';
    tooltip.style.display = 'block';
  }

  function hideTooltip() {
    var tooltip = document.getElementById('chartTooltip');
    if (tooltip) {
      tooltip.style.display = 'none';
    }
  }

  function drawEmptyState(ctx, width, height, message) {
    ctx.fillStyle = '#b0b0b0';
    ctx.font = '13px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(message, width / 2, height / 2);
  }

  function drawYAxisGrid(ctx, paddingLeft, paddingTop, plotWidth, plotHeight, maxValue, formatLabel) {
    ctx.strokeStyle = 'rgba(255,255,255,0.08)';
    ctx.fillStyle = '#b0b0b0';
    ctx.font = Y_AXIS_LABEL_FONT;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (var i = 0; i <= GRID_LINE_COUNT; i++) {
      var y = paddingTop + plotHeight - (plotHeight * i / GRID_LINE_COUNT);
      ctx.beginPath();
      ctx.moveTo(paddingLeft, y);
      ctx.lineTo(paddingLeft + plotWidth, y);
      ctx.stroke();
      ctx.fillText(formatLabel(maxValue * i / GRID_LINE_COUNT), paddingLeft - Y_AXIS_LABEL_GAP_PX, y);
    }
  }

  // Bucket labels come from the server as "YYYY-MM-DD" (day/week),
  // "YYYY-MM-DD HH:00" (hour), or "YYYY-MM" (month). A blanket slice(0,7) used
  // to collapse the first two to "YYYY-MM", so every day/week bar in a month
  // rendered the identical label. Keep month labels whole; drop the redundant
  // leading year from finer buckets so labels within a month stay distinct.
  function formatAxisLabel(label) {
    if (typeof label !== 'string') return label;
    if (/^\d{4}-\d{2}$/.test(label)) return label;
    if (/^\d{4}-\d{2}-\d{2}/.test(label)) return label.slice(5);
    return label;
  }

  function drawSparseXLabels(ctx, labels, paddingLeft, plotWidth, plotHeight, paddingTop, labelForIndex, minSpacing) {
    ctx.textBaseline = 'top';
    ctx.fillStyle = '#b0b0b0';
    ctx.font = '11px sans-serif';
    var spacing = minSpacing !== undefined ? minSpacing : MIN_AXIS_LABEL_SPACING_PX;
    var maxLabels = Math.max(2, Math.floor(plotWidth / spacing));
    var step = Math.max(1, Math.ceil(labels.length / maxLabels));
    var lastIndex = labels.length - 1;
    var lastStepIndex = Math.floor(lastIndex / step) * step;

    for (var i = 0; i <= lastStepIndex; i += step) {
      //< right-aligned only when there is something to its LEFT: the last label
      //  is pulled in so it ends at the plot edge rather than overflowing it,
      //  but with a single bucket the last label is also the first, sitting at
      //  x = paddingLeft - and right-aligning there draws it backwards off the
      //  canvas. One bucket is what a new account's daily trend has on day one.
      ctx.textAlign = (labels.length > 1 && i === lastIndex) ? 'right' : 'center';
      ctx.fillText(formatAxisLabel(labels[i]), labelForIndex(i), paddingTop + plotHeight + 8);
    }

    if (lastIndex !== lastStepIndex &&
        labelForIndex(lastIndex) - labelForIndex(lastStepIndex) >= spacing) {
      ctx.textAlign = 'right';
      ctx.fillText(formatAxisLabel(labels[lastIndex]), labelForIndex(lastIndex), paddingTop + plotHeight + 8);
    }
  }

  /* Multi-line (or single-line) trend chart over shared string buckets.
   * data = { buckets: [...], series: [{ name, data: [...], id? }] }.
   * opts.formatValue(v) -> tooltip value text; opts.emptyMessage; opts.onClickId
   * (optional) navigates when a series carries an id. Mirrors the /charts
   * artist-trend chart, generalized for reuse. */
  function renderMultiLineChart(canvas, legendEl, data, opts) {
    opts = opts || {};
    var formatValue = opts.formatValue || function (v) { return Math.round(v); };
    var emptyMessage = opts.emptyMessage || 'Not enough data yet.';
    if (!canvas) return;

    data = data || { buckets: [], series: [] };
    var setup = setupCanvas(canvas, opts.height || 260);
    var ctx = setup.ctx, width = setup.width, height = setup.height;
    ctx.clearRect(0, 0, width, height);

    if (!data.buckets.length || !data.series.length) {
      drawEmptyState(ctx, width, height, emptyMessage);
      renderLegend(legendEl, []);
      return;
    }

    var paddingLeft = 40, paddingBottom = 26, paddingTop = 16, paddingRight = 16;
    var plotWidth = width - paddingLeft - paddingRight;
    var plotHeight = height - paddingTop - paddingBottom;
    var maxVal = 1;
    data.series.forEach(function (s) { maxVal = Math.max(maxVal, Math.max.apply(null, s.data)); });
    var stepX = data.buckets.length > 1 ? plotWidth / (data.buckets.length - 1) : 0;

    drawYAxisGrid(ctx, paddingLeft, paddingTop, plotWidth, plotHeight, maxVal, function (v) { return Math.round(v); });
    drawSparseXLabels(ctx, data.buckets, paddingLeft, plotWidth, plotHeight, paddingTop, function (i) {
      return paddingLeft + i * stepX;
    }, MIN_AXIS_LABEL_SPACING_PX);

    var lines = data.series.map(function (series, si) {
      var color = PALETTE[si % PALETTE.length];
      var points = series.data.map(function (v, i) {
        return { x: paddingLeft + i * stepX, y: paddingTop + plotHeight - (plotHeight * v / maxVal), v: v };
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      points.forEach(function (p, i) { i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y); });
      ctx.stroke();
      ctx.fillStyle = color;
      points.forEach(function (p) {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
        ctx.fill();
      });
      return { name: series.name, id: series.id, color: color, points: points };
    });

    function findClosest(mx, my) {
      var closest = null, closestDist = 12;
      lines.forEach(function (line) {
        line.points.forEach(function (p, i) {
          var dist = Math.hypot(p.x - mx, p.y - my);
          if (dist < closestDist) {
            closestDist = dist;
            closest = { name: line.name, id: line.id, bucket: data.buckets[i], value: p.v };
          }
        });
      });
      return closest;
    }

    canvas.onmousemove = function (evt) {
      var rect = canvas.getBoundingClientRect();
      var closest = findClosest(evt.clientX - rect.left, evt.clientY - rect.top);
      if (closest) {
        showTooltip(evt, '<strong>' + escapeHtml(closest.name) + '</strong><br>' + closest.bucket + ' &middot; ' + formatValue(closest.value));
        canvas.style.cursor = (opts.onClickId && closest.id) ? 'pointer' : 'crosshair';
      } else {
        hideTooltip();
        canvas.style.cursor = 'crosshair';
      }
    };
    canvas.onmouseleave = function () { hideTooltip(); canvas.style.cursor = 'crosshair'; };
    canvas.onclick = function (evt) {
      if (!opts.onClickId) return;
      var rect = canvas.getBoundingClientRect();
      var closest = findClosest(evt.clientX - rect.left, evt.clientY - rect.top);
      if (closest && closest.id) opts.onClickId(closest.id);
    };

    renderLegend(legendEl, lines);
  }

  /* Vertical bar chart from [label, value] pairs. opts.emptyMessage,
   * opts.valueSuffix (tooltip), opts.height. */
  function renderBarsFromPairs(canvas, pairs, opts) {
    opts = opts || {};
    if (!canvas) return;
    pairs = pairs || [];
    var config = setupCanvas(canvas, opts.height || 300);
    var ctx = config.ctx, width = config.width, height = config.height;

    if (pairs.length === 0) {
      drawEmptyState(ctx, width, height, opts.emptyMessage || 'No data yet.');
      return;
    }

    var maxVal = Math.max.apply(null, pairs.map(function (p) { return p[1]; }));
    if (maxVal === 0) maxVal = 1;

    var paddingLeft = 50, paddingRight = 20, paddingTop = 20, paddingBottom = 40;
    var plotWidth = width - paddingLeft - paddingRight;
    var plotHeight = height - paddingTop - paddingBottom;

    drawYAxisGrid(ctx, paddingLeft, paddingTop, plotWidth, plotHeight, maxVal, function (v) { return Math.round(v); });

    var rawBarWidth = plotWidth / pairs.length;
    var spacing = Math.max(rawBarWidth * 0.25, 6);
    var barWidth = rawBarWidth - spacing;
    var suffix = opts.valueSuffix || ' plays';

    var bars = pairs.map(function (pair, i) {
      var key = pair[0], val = pair[1];
      var barHeight = plotHeight * val / maxVal;
      var x = paddingLeft + i * rawBarWidth + spacing / 2;
      var y = paddingTop + plotHeight - barHeight;
      ctx.fillStyle = PALETTE[i % PALETTE.length];
      ctx.fillRect(x, y, barWidth, barHeight);
      ctx.fillStyle = '#b0b0b0';
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(key, x + barWidth / 2, paddingTop + plotHeight + 8);
      return { key: key, value: val, x: x, y: y, w: barWidth, h: barHeight };
    });

    canvas.onmousemove = function (evt) {
      var rect = canvas.getBoundingClientRect();
      var mx = evt.clientX - rect.left, my = evt.clientY - rect.top;
      var found = null;
      bars.forEach(function (bar) {
        if (mx >= bar.x && mx <= bar.x + bar.w && my >= bar.y && my <= bar.y + bar.h) found = bar;
      });
      if (found) {
        showTooltip(evt, '<strong>' + escapeHtml(found.key) + '</strong><br>' + found.value + suffix);
      } else {
        hideTooltip();
      }
    };
    canvas.onmouseleave = hideTooltip;
  }

  /* Horizontal bar chart from [label, value] pairs - one row each, the full
   * label above its bar (so long category names stay readable, unlike the
   * cramped x-axis of the vertical bars). The canvas sizes itself to the row
   * count, so its wrapper should NOT have a fixed height. */
  function renderHorizontalBars(canvas, pairs, opts) {
    opts = opts || {};
    if (!canvas) return;
    pairs = pairs || [];
    var rowHeight = 40;                     //< label line + bar + gap
    var padTop = 6, padBottom = 6;
    var valueColWidth = 34;                 //< reserved right column for the value number
    var barHeight = 11, labelGap = 3;
    var cssHeight = pairs.length ? padTop + padBottom + pairs.length * rowHeight : 120;
    var config = setupCanvas(canvas, cssHeight);
    var ctx = config.ctx, width = config.width, height = config.height;
    ctx.clearRect(0, 0, width, height);

    if (pairs.length === 0) {
      drawEmptyState(ctx, width, height, opts.emptyMessage || 'No data yet.');
      return;
    }

    var maxVal = Math.max.apply(null, pairs.map(function (p) { return p[1]; }));
    if (maxVal === 0) maxVal = 1;
    var plotWidth = width - valueColWidth;
    var suffix = opts.valueSuffix || '';

    var bars = pairs.map(function (pair, i) {
      var key = pair[0], val = pair[1];
      var rowTop = padTop + i * rowHeight;

      // Label above the bar, using the full width; ellipsize only if truly huge.
      ctx.fillStyle = '#e0e0e0';
      ctx.font = '12px sans-serif';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      var label = key;
      if (ctx.measureText(label).width > width) {
        while (label.length > 1 && ctx.measureText(label + '…').width > width) {
          label = label.slice(0, -1);
        }
        label += '…';
      }
      ctx.fillText(label, 0, rowTop);

      var barY = rowTop + 12 + labelGap;
      var barW = Math.max(2, plotWidth * val / maxVal);
      ctx.fillStyle = PALETTE[i % PALETTE.length];
      ctx.fillRect(0, barY, barW, barHeight);

      ctx.fillStyle = '#b0b0b0';
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText(String(val), width, barY + barHeight / 2);

      return { key: key, value: val, top: rowTop, bottom: rowTop + rowHeight };
    });

    canvas.onmousemove = function (evt) {
      var rect = canvas.getBoundingClientRect();
      var my = evt.clientY - rect.top;
      var found = null;
      bars.forEach(function (bar) {
        if (my >= bar.top && my <= bar.bottom) found = bar;
      });
      if (found) {
        showTooltip(evt, '<strong>' + escapeHtml(found.key) + '</strong><br>' + found.value + suffix);
      } else {
        hideTooltip();
      }
    };
    canvas.onmouseleave = hideTooltip;
  }

  /* Donut chart. slices = [{ label, value, color }]; the caller supplies the
   * total (so a leftover "other" share can be represented without a slice). */
  function drawDonutChart(canvas, slices, total, opts) {
    opts = opts || {};
    if (!canvas) return;
    var config = setupCanvas(canvas, opts.height || 300);
    var ctx = config.ctx, width = config.width, height = config.height;
    ctx.clearRect(0, 0, width, height);

    if (!total) {
      drawEmptyState(ctx, width, height, opts.emptyMessage || 'No data yet.');
      return;
    }

    var cx = width / 2;
    var cy = height / 2 - 15;
    var outerRadius = Math.min(width, height) / 2 - 30;
    var innerRadius = outerRadius * 0.65;
    var startAngle = -Math.PI / 2;

    slices.forEach(function (slice) {
      if (slice.value === 0) return;
      var endAngle = startAngle + (slice.value / total) * Math.PI * 2;
      ctx.fillStyle = slice.color;
      ctx.beginPath();
      ctx.arc(cx, cy, outerRadius, startAngle, endAngle);
      ctx.arc(cx, cy, innerRadius, endAngle, startAngle, true);
      ctx.closePath();
      ctx.fill();
      slice.startAngle = startAngle;
      slice.endAngle = endAngle;
      startAngle = endAngle;
    });

    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--bg-card').trim() || '#1c1c1e';
    ctx.beginPath();
    ctx.arc(cx, cy, innerRadius, 0, Math.PI * 2);
    ctx.fill();

    // Optional swatch + "Label: value (pct%)" legend row under the ring - the
    // /charts explicit/completion donuts show it; the Genres share donut leaves
    // it off and relies on the hover tooltip + chip list instead.
    if (opts.showLabels) {
      ctx.textBaseline = 'middle';
      ctx.font = '11px sans-serif';
      var labelY = height - 20;
      var activeSlices = slices.filter(function (s) { return s.value > 0; });
      var stepX = width / (activeSlices.length + 1);
      activeSlices.forEach(function (slice, idx) {
        var x = stepX * (idx + 1);
        var percentage = Math.round((slice.value / total) * 100);
        var text = slice.label + ': ' + slice.value + ' (' + percentage + '%)';
        ctx.fillStyle = slice.color;
        ctx.beginPath();
        ctx.arc(x - 60, labelY, 5, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = '#ffffff';
        ctx.textAlign = 'left';
        ctx.fillText(text, x - 45, labelY);
      });
    }

    canvas.onmousemove = function (evt) {
      var rect = canvas.getBoundingClientRect();
      var dx = (evt.clientX - rect.left) - cx, dy = (evt.clientY - rect.top) - cy;
      var dist = Math.hypot(dx, dy);
      if (dist >= innerRadius && dist <= outerRadius) {
        var angle = Math.atan2(dy, dx);
        if (angle < -Math.PI / 2) angle += Math.PI * 2;
        var found = null;
        slices.forEach(function (slice) {
          if (slice.value > 0 && angle >= slice.startAngle && angle <= slice.endAngle) found = slice;
        });
        if (found) {
          var pct = ((found.value / total) * 100).toFixed(1);
          showTooltip(evt, '<strong>' + escapeHtml(found.label) + '</strong><br>' + found.value + ' plays (' + pct + '%)');
        } else {
          hideTooltip();
        }
      } else {
        hideTooltip();
      }
    };
    canvas.onmouseleave = hideTooltip;
  }

  function heatColor(intensity) {
    var clamped = Math.max(0, Math.min(1, intensity));
    if (clamped === 0) {
      return 'rgba(255,255,255,0.05)';
    }
    var rgb = parseHex(getAccentColor());
    var r = Math.round(30 + (rgb.r - 30) * clamped);
    var g = Math.round(30 + (rgb.g - 30) * clamped);
    var b = Math.round(30 + (rgb.b - 30) * clamped);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }

  /* Day-of-week x hour-of-day heatmap. grid = 7 rows (Mon..Sun) x 24 cols, each
   * cell { totalTimeListened, totalTimeListenedText, plays }. Used by the /charts
   * "When You Listen" heatmap and the Genres page per-genre listening clock. */
  function renderHeatmap(canvas, grid, opts) {
    opts = opts || {};
    if (!canvas) return;
    grid = grid || [];
    var rows = grid.length;
    var cols = rows ? grid[0].length : 24;
    var cellHeight = 26;
    var cssHeight = rows * cellHeight + 34;
    var setup = setupCanvas(canvas, cssHeight);
    var ctx = setup.ctx, width = setup.width;
    ctx.clearRect(0, 0, width, cssHeight);

    if (rows === 0) {
      drawEmptyState(ctx, width, cssHeight, opts.emptyMessage || 'No listening data in this period yet.');
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

  // --- time-series skip series ------------------------------------------
  // Pure helpers so the two decisions the skips series turns on are testable
  // without a canvas (renderTimeSeriesChart itself needs a real DOM).

  function maxSkipsIn(data) {
    /* Highest skip count across the buckets - the skip bars' own scale. Skips
     * carry no listening time, so they cannot share the millisecond axis. */
    return (data || []).reduce(function (max, d) {
      return Math.max(max, (d && d.skips) || 0);
    }, 0);
  }

  function skipAxisMax(maxSkips, gridLineCount) {
    /* Top of the skip bars' right-hand axis: the tallest bar rounded up to a
     * whole multiple of the grid, so every grid line lands on a whole skip
     * count. Labelling the raw maximum repeats itself - 3 skips across 4 lines
     * reads 0 / 1 / 2 / 2 / 3 once each is rounded to a play count. 0 when
     * nothing was skipped, where the series isn't drawn at all (see
     * renderTimeSeriesChart's hasSkips) and there is no axis to label. */
    if (!maxSkips) {
      return 0;
    }
    return Math.ceil(maxSkips / gridLineCount) * gridLineCount;
  }

  // Which PALETTE entry each of the time-series chart's two bars is drawn in.
  // Shared with charts.js's renderTimeSeriesChart so the swatch in the legend
  // and the bar on the canvas can never name different colours.
  var TIME_SERIES_PLAY_COLOR_INDEX = 0;
  var TIME_SERIES_SKIP_COLOR_INDEX = 3;

  function timeSeriesLegendItems(hasSkips) {
    /* The time-series chart's key, or [] when there is nothing to key.
     *
     * Only the detail pages draw the skips series, and only where the range
     * actually holds skips - everywhere else the chart is one series against
     * one axis and a legend would be noise. Where it IS drawn the key is not
     * optional: two differently coloured bars shared each bucket with a single
     * millisecond axis and nothing naming either of them, so the narrower bar
     * read as some second measure of listening. It is a count, read against
     * its own axis on the right (see skipAxisMax) - hence the axis note on
     * each. */
    if (!hasSkips) {
      return [];
    }
    return [
      { name: 'Listening time (left axis)', color: PALETTE[TIME_SERIES_PLAY_COLOR_INDEX] },
      { name: 'Skips (right axis)', color: PALETTE[TIME_SERIES_SKIP_COLOR_INDEX] },
    ];
  }

  function legendHtml(items) {
    /* Swatch + name markup for a list of {name, color}, styled by .chart-legend
     * in style.css. Names are escaped: renderMultiLineChart feeds artist names
     * through here. */
    return (items || []).map(function (item) {
      return '<span class="chart-legend-item"><span class="chart-legend-swatch" style="background:' +
        item.color + '"></span>' + escapeHtml(item.name) + '</span>';
    }).join('');
  }

  function renderLegend(legendEl, items) {
    /* Fills `legendEl` with `items`' key, emptying it when there is none - a
     * chart that redraws (the detail pages' bucket select) must not leave the
     * previous render's legend behind. No-op without the element, so a page
     * that doesn't have one just gets no legend. */
    if (legendEl) {
      legendEl.innerHTML = legendHtml(items);
    }
  }

  function timeSeriesHasNothingToDraw(data, showSkips) {
    /* True only when there is nothing the chart would actually draw, which
     * depends on whether the caller draws the skips series (see
     * renderTimeSeriesChart). With it on, a bucket holding only skips counts:
     * a track whose plays are ALL skips has zero listening time everywhere, so
     * a time-only check would show "no data" on a page whose whole point is
     * the skips. With it off - the default, and what every aggregate page
     * gets - skips are not drawn, so such a range genuinely has nothing to
     * show and the empty state says more than a row of zero-height bars. */
    if (!data || data.length === 0) {
      return true;
    }
    return !data.some(function (d) {
      return (d && d.totalTimeListened > 0) || (showSkips && ((d && d.skips) || 0) > 0);
    });
  }

  var ChartUtils = {
    PALETTE: PALETTE,
    TIME_SERIES_PLAY_COLOR_INDEX: TIME_SERIES_PLAY_COLOR_INDEX,
    TIME_SERIES_SKIP_COLOR_INDEX: TIME_SERIES_SKIP_COLOR_INDEX,
    GRID_LINE_COUNT: GRID_LINE_COUNT,
    MIN_AXIS_LABEL_SPACING_PX: MIN_AXIS_LABEL_SPACING_PX,
    Y_AXIS_LABEL_FONT: Y_AXIS_LABEL_FONT,
    Y_AXIS_LABEL_GAP_PX: Y_AXIS_LABEL_GAP_PX,
    bindRepaint: bindRepaint,
    maxSkipsIn: maxSkipsIn,
    skipAxisMax: skipAxisMax,
    timeSeriesLegendItems: timeSeriesLegendItems,
    legendHtml: legendHtml,
    renderLegend: renderLegend,
    timeSeriesHasNothingToDraw: timeSeriesHasNothingToDraw,
    bucketDrilldownUrl: bucketDrilldownUrl,
    refreshPalette: refreshPalette,
    getAccentColor: getAccentColor,
    parseHex: parseHex,
    escapeHtml: escapeHtml,
    setupCanvas: setupCanvas,
    showTooltip: showTooltip,
    hideTooltip: hideTooltip,
    drawEmptyState: drawEmptyState,
    drawYAxisGrid: drawYAxisGrid,
    drawSparseXLabels: drawSparseXLabels,
    formatAxisLabel: formatAxisLabel,
    renderHeatmap: renderHeatmap,
    renderMultiLineChart: renderMultiLineChart,
    renderBarsFromPairs: renderBarsFromPairs,
    renderHorizontalBars: renderHorizontalBars,
    drawDonutChart: drawDonutChart,
  };

  if (typeof window !== 'undefined') {
    window.ChartUtils = ChartUtils;
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ChartUtils;
  }
})();
