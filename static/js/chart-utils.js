/* Shared hand-rolled canvas-chart primitives (no external dependencies, so the
 * app stays self-contained for offline/Docker use). Exposed as window.ChartUtils
 * for pages that build their own charts - currently the Genres page (genres.js).
 * The older /charts + /compare charts.js still carries its own private copies of
 * these primitives; converging it onto this library is tracked separately. */
(function () {
  // Fallback if a theme's --chart-N vars are ever missing (e.g. stale cached CSS).
  var FALLBACK_PALETTE = ['#FB717B', '#5DD97C', '#5AC8FA', '#FFD166', '#C77DFF', '#FF9F45'];
  // Must match the --chart-1..--chart-N custom properties defined per theme in
  // style.css (html.theme-rose/green/purple/red) - one color per chart category
  // so multi-bar/slice charts (genre distribution, breadth, share donut) get a
  // distinct, theme-appropriate color for every category instead of cycling
  // through a handful of colors that repeat and never adapt to the theme.
  var CHART_COLOR_VAR_COUNT = 12;
  var PALETTE = ['#FB717B', '#5DD97C', '#5AC8FA', '#FFD166', '#C77DFF', '#FF9F45'];
  var GRID_LINE_COUNT = 4;
  var MIN_AXIS_LABEL_SPACING_PX = 70;
  var Y_AXIS_LABEL_FONT = '11px sans-serif';
  var Y_AXIS_LABEL_GAP_PX = 8;

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
  refreshPalette();

  function getAccentColor() {
    var accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
    return accent || PALETTE[0];
  }

  // Genre/artist/track names come from the user's own imported data and aren't
  // guaranteed HTML-safe - escape before splicing into an innerHTML string.
  function escapeHtml(str) {
    var div = document.createElement('div');
    div.textContent = str == null ? '' : str;
    return div.innerHTML;
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
      ctx.textAlign = (i === lastIndex) ? 'right' : 'center';
      ctx.fillText(labels[i].slice(0, 7), labelForIndex(i), paddingTop + plotHeight + 8);
    }

    if (lastIndex !== lastStepIndex &&
        labelForIndex(lastIndex) - labelForIndex(lastStepIndex) >= spacing) {
      ctx.textAlign = 'right';
      ctx.fillText(labels[lastIndex].slice(0, 7), labelForIndex(lastIndex), paddingTop + plotHeight + 8);
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
      if (legendEl) legendEl.innerHTML = '';
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

    if (legendEl) {
      legendEl.innerHTML = lines.map(function (l) {
        return '<span class="chart-legend-item"><span class="chart-legend-swatch" style="background:' + l.color + '"></span>' + escapeHtml(l.name) + '</span>';
      }).join('');
    }
  }

  /* Vertical bar chart from [label, value] pairs. opts.emptyMessage,
   * opts.fitLabel(key, slotWidth), opts.valueSuffix (tooltip). */
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
      var label = opts.fitLabel ? opts.fitLabel(key, rawBarWidth) : key;
      ctx.fillStyle = '#b0b0b0';
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(label, x + barWidth / 2, paddingTop + plotHeight + 8);
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
    var rowHeight = opts.rowHeight || 40;   //< label line + bar + gap
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

  window.ChartUtils = {
    PALETTE: PALETTE,
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
    renderHeatmap: renderHeatmap,
    renderMultiLineChart: renderMultiLineChart,
    renderBarsFromPairs: renderBarsFromPairs,
    renderHorizontalBars: renderHorizontalBars,
    drawDonutChart: drawDonutChart,
  };
})();
