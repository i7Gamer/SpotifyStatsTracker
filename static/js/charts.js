/* Hand-rolled canvas charts for the /charts page - no external dependencies, so
 * the app stays self-contained for offline/Docker use. Reads data from
 * window.__chartData, set inline by charts.html before this script loads. */
(function () {
  var CHART_PALETTE = ['#FB717B', '#5DD97C', '#5AC8FA', '#FFD166', '#C77DFF', '#FF9F45'];
  var GRID_LINE_COUNT = 4;
  var MIN_AXIS_LABEL_SPACING_PX = 70;

  function getAccentColor() {
    var computedAccent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
    return computedAccent || '#FB717B';
  }

  function parseHex(hex) {
    hex = hex.replace(/^#/, '');
    if (hex.length === 3) {
      hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
    }
    var num = parseInt(hex, 16);
    if (isNaN(num)) {
      return { r: 251, g: 113, b: 123 };
    }
    return {
      r: (num >> 16) & 255,
      g: (num >> 8) & 255,
      b: num & 255
    };
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

  function drawYAxisGrid(ctx, paddingLeft, paddingTop, plotWidth, plotHeight, maxValue, formatLabel) {
    ctx.strokeStyle = 'rgba(255,255,255,0.08)';
    ctx.fillStyle = '#b0b0b0';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (var i = 0; i <= GRID_LINE_COUNT; i++) {
      var y = paddingTop + plotHeight - (plotHeight * i / GRID_LINE_COUNT);
      ctx.beginPath();
      ctx.moveTo(paddingLeft, y);
      ctx.lineTo(paddingLeft + plotWidth, y);
      ctx.stroke();
      ctx.fillText(formatLabel(maxValue * i / GRID_LINE_COUNT), paddingLeft - 8, y);
    }
  }

  function drawSparseXLabels(ctx, labels, paddingLeft, plotWidth, plotHeight, paddingTop, labelForIndex, minSpacing) {
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillStyle = '#b0b0b0';
    ctx.font = '11px sans-serif';
    var spacing = minSpacing !== undefined ? minSpacing : MIN_AXIS_LABEL_SPACING_PX;
    var maxLabels = Math.max(2, Math.floor(plotWidth / spacing));
    var step = Math.max(1, Math.ceil(labels.length / maxLabels));
    for (var i = 0; i < labels.length; i++) {
      if (i % step !== 0 && i !== labels.length - 1) {
        continue;
      }
      var x = labelForIndex(i);
      ctx.fillText(labels[i].slice(0, 7), x, paddingTop + plotHeight + 8);
    }
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

    var paddingLeft = 46, paddingBottom = 26, paddingTop = 16, paddingRight = 16;
    var plotWidth = width - paddingLeft - paddingRight;
    var plotHeight = height - paddingTop - paddingBottom;
    var maxMs = Math.max(1, Math.max.apply(null, data.map(function (d) { return d.totalTimeListened; })));
    var slotWidth = plotWidth / data.length;
    var barGap = 4;
    var barWidth = Math.max(2, slotWidth - barGap);

    drawYAxisGrid(ctx, paddingLeft, paddingTop, plotWidth, plotHeight, maxMs, msToShortLabel);

    var bars = data.map(function (d, i) {
      var x = paddingLeft + i * slotWidth + barGap / 2;
      var barHeight = plotHeight * (d.totalTimeListened / maxMs);
      var y = paddingTop + plotHeight - barHeight;
      ctx.fillStyle = CHART_PALETTE[0];
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

    canvas.onmousemove = function (evt) {
      var rect = canvas.getBoundingClientRect();
      var mx = evt.clientX - rect.left, my = evt.clientY - rect.top;
      var hit = null;
      for (var i = 0; i < bars.length; i++) {
        var b = bars[i];
        if (mx >= b.x && mx <= b.x + b.width && my >= paddingTop && my <= paddingTop + plotHeight) {
          hit = b;
          break;
        }
      }
      if (hit) {
        var label = isLastDay ? hit.d.label.split(' ')[1] : hit.d.label;
        showTooltip(evt, '<strong>' + label + '</strong><br>' + (hit.d.totalTimeListenedText || '0s') + ' &middot; ' + hit.d.plays + ' plays');
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
    var canvas = document.getElementById('artistTrendChart');
    var legendEl = document.getElementById('artistTrendLegend');
    if (!canvas) {
      return;
    }
    var data = (window.__chartData && window.__chartData.artistTrend) || { buckets: [], series: [] };
    var setup = setupCanvas(canvas, 260);
    var ctx = setup.ctx, width = setup.width, height = setup.height;
    ctx.clearRect(0, 0, width, height);

    if (!data.buckets.length || !data.series.length) {
      drawEmptyState(ctx, width, height, 'Not enough data yet to show an artist trend.');
      if (legendEl) {
        legendEl.innerHTML = '';
      }
      return;
    }

    var paddingLeft = 40, paddingBottom = 26, paddingTop = 16, paddingRight = 16;
    var plotWidth = width - paddingLeft - paddingRight;
    var plotHeight = height - paddingTop - paddingBottom;
    var maxPlays = 1;
    data.series.forEach(function (s) {
      maxPlays = Math.max(maxPlays, Math.max.apply(null, s.data));
    });
    var stepX = data.buckets.length > 1 ? plotWidth / (data.buckets.length - 1) : 0;

    drawYAxisGrid(ctx, paddingLeft, paddingTop, plotWidth, plotHeight, maxPlays, function (v) { return Math.round(v); });
    drawSparseXLabels(ctx, data.buckets, paddingLeft, plotWidth, plotHeight, paddingTop, function (i) {
      return paddingLeft + i * stepX;
    }, MIN_AXIS_LABEL_SPACING_PX);

    var lines = data.series.map(function (series, si) {
      var color = CHART_PALETTE[si % CHART_PALETTE.length];
      var points = series.data.map(function (v, i) {
        return { x: paddingLeft + i * stepX, y: paddingTop + plotHeight - (plotHeight * v / maxPlays), v: v };
      });

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

      ctx.fillStyle = color;
      points.forEach(function (p) {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
        ctx.fill();
      });

      return { name: series.name, color: color, points: points };
    });

    canvas.onmousemove = function (evt) {
      var rect = canvas.getBoundingClientRect();
      var mx = evt.clientX - rect.left, my = evt.clientY - rect.top;
      var closest = null, closestDist = 12;
      lines.forEach(function (line) {
        line.points.forEach(function (p, i) {
          var dist = Math.hypot(p.x - mx, p.y - my);
          if (dist < closestDist) {
            closestDist = dist;
            closest = { name: line.name, bucket: data.buckets[i], value: p.v };
          }
        });
      });
      if (closest) {
        showTooltip(evt, '<strong>' + closest.name + '</strong><br>' + closest.bucket + ' &middot; ' + closest.value + ' plays');
      } else {
        hideTooltip();
      }
    };
    canvas.onmouseleave = hideTooltip;

    if (legendEl) {
      legendEl.innerHTML = lines.map(function (l) {
        return '<span class="chart-legend-item"><span class="chart-legend-swatch" style="background:' + l.color + '"></span>' + l.name + '</span>';
      }).join('');
    }
  }

  function drawDonutChart(ctx, width, height, slices, total, canvas) {
    var cx = width / 2;
    var cy = height / 2 - 15;
    var outerRadius = Math.min(width, height) / 2 - 30;
    var innerRadius = outerRadius * 0.65;

    var startAngle = -Math.PI / 2;

    slices.forEach(function (slice) {
      if (slice.value === 0) return;
      var angle = (slice.value / total) * Math.PI * 2;
      var endAngle = startAngle + angle;

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

    ctx.textBaseline = 'middle';
    ctx.font = '11px sans-serif';

    var labelY = height - 20;
    var activeSlices = slices.filter(function(s) { return s.value > 0; });
    var stepX = width / (activeSlices.length + 1);

    activeSlices.forEach(function(slice, idx) {
      var x = stepX * (idx + 1);
      var percentage = Math.round((slice.value / total) * 100);
      var text = slice.label + ': ' + slice.value + ' (' + percentage + '%)';

      // Draw circle (swatch) first
      ctx.fillStyle = slice.color;
      ctx.beginPath();
      ctx.arc(x - 60, labelY, 5, 0, Math.PI * 2);
      ctx.fill();

      // Draw text aligned to the right of the circle
      ctx.fillStyle = '#ffffff';
      ctx.textAlign = 'left';
      ctx.fillText(text, x - 45, labelY);
    });

    canvas.onmousemove = function(evt) {
      var rect = canvas.getBoundingClientRect();
      var mx = evt.clientX - rect.left, my = evt.clientY - rect.top;
      var dx = mx - cx, dy = my - cy;
      var dist = Math.hypot(dx, dy);

      if (dist >= innerRadius && dist <= outerRadius) {
        var angle = Math.atan2(dy, dx);
        if (angle < -Math.PI / 2) {
          angle += Math.PI * 2;
        }
        var found = null;
        slices.forEach(function(slice) {
          if (slice.value > 0) {
            var start = slice.startAngle;
            var end = slice.endAngle;
            if (angle >= start && angle <= end) {
              found = slice;
            }
          }
        });

        if (found) {
          var pct = ((found.value / total) * 100).toFixed(1);
          showTooltip(evt, '<strong>' + found.label + '</strong><br>' + found.value + ' plays (' + pct + '%)');
        } else {
          hideTooltip();
        }
      } else {
        hideTooltip();
      }
    };
    canvas.onmouseleave = hideTooltip;
  }

  function renderExplicitChart() {
    var canvas = document.getElementById('explicitChart');
    if (!canvas) return;
    var data = window.__chartData.explicitRatio;
    if (!data) return;

    var config = setupCanvas(canvas, 250);
    var ctx = config.ctx, width = config.width, height = config.height;

    var total = data.explicit + data.clean;
    if (total === 0) {
      drawEmptyState(ctx, width, height, 'No listening history in this period.');
      return;
    }

    var slices = [
      { label: 'Explicit', value: data.explicit, color: getAccentColor() },
      { label: 'Clean', value: data.clean, color: '#5AC8FA' }
    ];

    drawDonutChart(ctx, width, height, slices, total, canvas);
  }

  function renderCompletionChart() {
    var canvas = document.getElementById('completionChart');
    if (!canvas) return;
    var data = window.__chartData.completionStats;
    if (!data) return;

    var config = setupCanvas(canvas, 250);
    var ctx = config.ctx, width = config.width, height = config.height;

    var total = data.skips + data.completes + data.partials;
    if (total === 0) {
      drawEmptyState(ctx, width, height, 'No listening history in this period.');
      return;
    }

    var slices = [
      { label: 'Completed', value: data.completes, color: getAccentColor() },
      { label: 'Partial', value: data.partials, color: '#5DD97C' },
      { label: 'Skipped', value: data.skips, color: '#5AC8FA' }
    ];

    drawDonutChart(ctx, width, height, slices, total, canvas);
  }

  function renderDecadeChart() {
    var canvas = document.getElementById('decadeChart');
    if (!canvas) return;
    var data = window.__chartData.decadeDistribution;
    if (!data) return;

    var config = setupCanvas(canvas, 300);
    var ctx = config.ctx, width = config.width, height = config.height;

    var keys = Object.keys(data);
    if (keys.length === 0) {
      drawEmptyState(ctx, width, height, 'No album release information in this period.');
      return;
    }

    var values = keys.map(function(k) { return data[k]; });
    var maxVal = Math.max.apply(null, values);
    if (maxVal === 0) maxVal = 1;

    var paddingLeft = 50, paddingRight = 20, paddingTop = 20, paddingBottom = 40;
    var plotWidth = width - paddingLeft - paddingRight;
    var plotHeight = height - paddingTop - paddingBottom;

    drawYAxisGrid(ctx, paddingLeft, paddingTop, plotWidth, plotHeight, maxVal, function(v) { return Math.round(v); });

    var barCount = keys.length;
    var rawBarWidth = plotWidth / barCount;
    var spacing = Math.max(rawBarWidth * 0.25, 6);
    var barWidth = rawBarWidth - spacing;

    var bars = keys.map(function(key, i) {
      var val = data[key];
      var barHeight = plotHeight * val / maxVal;
      var x = paddingLeft + i * rawBarWidth + spacing / 2;
      var y = paddingTop + plotHeight - barHeight;

      ctx.fillStyle = CHART_PALETTE[i % CHART_PALETTE.length];
      ctx.fillRect(x, y, barWidth, barHeight);

      ctx.fillStyle = '#b0b0b0';
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(key, x + barWidth / 2, paddingTop + plotHeight + 8);

      return { key: key, value: val, x: x, y: y, w: barWidth, h: barHeight };
    });

    canvas.onmousemove = function(evt) {
      var rect = canvas.getBoundingClientRect();
      var mx = evt.clientX - rect.left, my = evt.clientY - rect.top;
      var found = null;

      bars.forEach(function(bar) {
        if (mx >= bar.x && mx <= bar.x + bar.w && my >= bar.y && my <= bar.y + bar.h) {
          found = bar;
        }
      });

      if (found) {
        showTooltip(evt, '<strong>' + found.key + '</strong><br>' + found.value + ' plays');
      } else {
        hideTooltip();
      }
    };
    canvas.onmouseleave = hideTooltip;
  }

  function renderAllCharts() {
    CHART_PALETTE[0] = getAccentColor();
    renderTimeSeriesChart();
    renderHeatmap();
    renderArtistTrend();
    renderExplicitChart();
    renderCompletionChart();
    renderDecadeChart();
  }

  window.renderTimeSeriesChart = renderTimeSeriesChart;

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
