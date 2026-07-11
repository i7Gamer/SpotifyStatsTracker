/* Hand-rolled canvas charts for the /charts page - no external dependencies, so
 * the app stays self-contained for offline/Docker use. Reads data from
 * window.__chartData, set inline by charts.html before this script loads. */
(function () {
  var CHART_PALETTE = ['#FB717B', '#5DD97C', '#5AC8FA', '#FFD166', '#C77DFF', '#FF9F45'];
  var GRID_LINE_COUNT = 4;
  var MIN_AXIS_LABEL_SPACING_PX = 70;

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

    var labels = isLastDay ? data.map(function (d, i) { return i + ':00'; }) : data.map(function (d) { return d.label; });
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
        var label = isLastDay ? (hit.hourIndex + ':00') : hit.d.label;
        showTooltip(evt, '<strong>' + label + '</strong><br>' + (hit.d.totalTimeListenedText || '0ms') + ' &middot; ' + hit.d.plays + ' plays');
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
    var r = Math.round(30 + (251 - 30) * clamped);
    var g = Math.round(30 + (113 - 30) * clamped);
    var b = Math.round(30 + (123 - 30) * clamped);
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
        showTooltip(evt, '<strong>' + hit.day + ' ' + hourLabel + ':00</strong><br>' + (hit.cell.totalTimeListenedText || '0ms') + ' &middot; ' + hit.cell.plays + ' plays');
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

  function renderAllCharts() {
    renderTimeSeriesChart();
    renderHeatmap();
    renderArtistTrend();
  }

  renderAllCharts();

  var resizeTimer;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderAllCharts, 150);
  });
})();
