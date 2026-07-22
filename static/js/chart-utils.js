/* Shared hand-rolled canvas-chart primitives (no external dependencies, so the
 * app stays self-contained for offline/Docker use). Exposed as window.ChartUtils
 * for pages that build their own charts - currently the Genres page (genres.js).
 * The older /charts + /compare charts.js still carries its own private copies of
 * these primitives; converging it onto this library is tracked separately. */
(function () {
  var PALETTE = ['#FB717B', '#5DD97C', '#5AC8FA', '#FFD166', '#C77DFF', '#FF9F45'];
  var GRID_LINE_COUNT = 4;
  var MIN_AXIS_LABEL_SPACING_PX = 70;
  var Y_AXIS_LABEL_FONT = '11px sans-serif';
  var Y_AXIS_LABEL_GAP_PX = 8;

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

  window.ChartUtils = {
    PALETTE: PALETTE,
    getAccentColor: getAccentColor,
    escapeHtml: escapeHtml,
    setupCanvas: setupCanvas,
    showTooltip: showTooltip,
    hideTooltip: hideTooltip,
    drawEmptyState: drawEmptyState,
    renderMultiLineChart: renderMultiLineChart,
    renderBarsFromPairs: renderBarsFromPairs,
    drawDonutChart: drawDonutChart,
  };
})();
