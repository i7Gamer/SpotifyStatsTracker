// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

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

  // Consts still needed by the charts-only helpers (valueAxisPadding, the
  // time-series/mirror label spacing), read off the library like the functions
  // above rather than re-declared: GRID_LINE_COUNT in particular has to be the
  // grid ChartUtils.drawYAxisGrid draws, so valueAxisPadding sizes for the
  // labels actually rendered - a local copy could drift from it silently.
  var GRID_LINE_COUNT = CU.GRID_LINE_COUNT;
  var MIN_AXIS_LABEL_SPACING_PX = CU.MIN_AXIS_LABEL_SPACING_PX;
  var Y_AXIS_LABEL_FONT = CU.Y_AXIS_LABEL_FONT;
  var Y_AXIS_LABEL_GAP_PX = CU.Y_AXIS_LABEL_GAP_PX;
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

  // Skip-axis labels are whole play counts. skipAxisMax keeps every grid line
  // on a whole number, so this only has to drop the float division's dust.
  function skipCountLabel(count) {
    return String(Math.round(count));
  }

  // A fixed padding either wastes space (short labels like "45m") or clips the
  // widest one off the edge (long ones like "150h30m" - the longer a value
  // axis's labels, the more room they need). Size it to what the grid's own
  // labels will actually render as, sampling the same GRID_LINE_COUNT
  // fractions drawYAxisGrid draws. Used for both sides of the time-series
  // chart, which carries a second axis for the skips series.
  function valueAxisPadding(ctx, maxValue, formatLabel) {
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

  // The skips series' own axis, labelled down the right-hand edge against the
  // grid drawYAxisGrid already drew for the left one. Labels only - a second
  // set of grid lines at different heights would just be noise over the bars.
  function drawRightAxisLabels(ctx, axisX, paddingTop, plotHeight, maxValue, formatLabel) {
    ctx.fillStyle = '#b0b0b0';
    ctx.font = Y_AXIS_LABEL_FONT;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    for (var i = 0; i <= GRID_LINE_COUNT; i++) {
      var y = paddingTop + plotHeight - (plotHeight * i / GRID_LINE_COUNT);
      ctx.fillText(formatLabel(maxValue * i / GRID_LINE_COUNT), axisX + Y_AXIS_LABEL_GAP_PX, y);
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

    // Skips carry no listening time, so they cannot share the millisecond axis
    // - they get their own count axis down the right-hand edge and a second,
    // narrower bar in each slot.
    // Only the detail pages ask for that (showSkips, set in detail-page.js):
    // there the series answers "does this one item get skipped", and without it
    // a track whose plays are ALL skips renders every bar at zero height and
    // reads as "nothing here". The aggregate pages (/charts, /wrapped) leave it
    // off. Two units on one axis is unreadable once the buckets hold real
    // volume: the tallest skip count is always drawn at full height, so a week
    // with 26 skips out-towered its own 128 plays and, against the only axis on
    // screen, appeared to claim 9 hours.
    var showSkips = !!(window.__chartData && window.__chartData.showSkips);
    // Present on the detail pages only, and filled in below - the second bar is
    // meaningless without a key naming it and the axis it is read against.
    var legendEl = document.getElementById('timeSeriesLegend');
    if (CU.timeSeriesHasNothingToDraw(data, showSkips)) {
      drawEmptyState(ctx, width, height, 'No listening data in this period yet.');
      CU.renderLegend(legendEl, []);
      return;
    }
    var maxSkips = showSkips ? CU.maxSkipsIn(data) : 0;
    var hasSkips = maxSkips > 0;
    // Keyed off what was actually drawn, not off showSkips: a range with no
    // skips in it draws one series, and a key naming a bar that isn't there
    // reads as "you have skips somewhere on this chart". Re-rendered on every
    // redraw so the bucket select can add and remove it (see detail-chart.js).
    CU.renderLegend(legendEl, CU.timeSeriesLegendItems(hasSkips));

    // Rounded up off maxSkips so the right axis's labels are whole plays; the
    // bars scale against it too, or the labels would not describe them.
    var skipAxisTop = CU.skipAxisMax(maxSkips, GRID_LINE_COUNT);

    var maxMs = Math.max(1, Math.max.apply(null, data.map(function (d) { return d.totalTimeListened; })));
    var paddingLeft = valueAxisPadding(ctx, maxMs, msToShortLabel), paddingBottom = 26, paddingTop = 16;
    // Room for the skip axis's labels only where that axis is drawn.
    var paddingRight = hasSkips ? valueAxisPadding(ctx, skipAxisTop, skipCountLabel) : 16;
    var plotWidth = width - paddingLeft - paddingRight;
    var plotHeight = height - paddingTop - paddingBottom;
    var slotWidth = plotWidth / data.length;
    var barGap = 4;
    var barWidth = Math.max(2, slotWidth - barGap);
    // Split the slot between the two series only when both are on show.
    var playWidth = hasSkips ? Math.max(1, barWidth / 2) : barWidth;

    drawYAxisGrid(ctx, paddingLeft, paddingTop, plotWidth, plotHeight, maxMs, msToShortLabel);
    if (hasSkips) {
      drawRightAxisLabels(ctx, paddingLeft + plotWidth, paddingTop, plotHeight, skipAxisTop, skipCountLabel);
    }

    var bars = data.map(function (d, i) {
      var x = paddingLeft + i * slotWidth + barGap / 2;
      var barHeight = plotHeight * (d.totalTimeListened / maxMs);
      var y = paddingTop + plotHeight - barHeight;
      ctx.fillStyle = PALETTE[CU.TIME_SERIES_PLAY_COLOR_INDEX];
      ctx.fillRect(x, y, playWidth, barHeight);
      if (hasSkips && (d.skips || 0) > 0) {
        var skipHeight = plotHeight * (d.skips / skipAxisTop);
        ctx.fillStyle = PALETTE[CU.TIME_SERIES_SKIP_COLOR_INDEX];
        ctx.fillRect(x + playWidth, paddingTop + plotHeight - skipHeight, playWidth, skipHeight);
      }
      // The hit box stays the full slot so both series share one tooltip.
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
        var body = (hit.d.totalTimeListenedText || '0s') + ' &middot; ' + hit.d.plays + ' plays';
        // Only mentioned where the series is drawn and there are some: the
        // count is exact here, where the right axis only labels the grid.
        if (hasSkips && (hit.d.skips || 0) > 0) {
          body += ' &middot; ' + hit.d.skips + (hit.d.skips === 1 ? ' skip' : ' skips');
        }
        showTooltip(evt, '<strong>' + label + '</strong><br>' + body);
        // rangeStart is only stamped for buckets with a clean calendar-date
        // mapping (see dashboard/date_ranges.py's _timeSeriesBucketRange) - the
        // single-day view's hourly buckets don't get one, so they stay
        // un-clickable.
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
    // Clicking a bar opens the play history scoped to that exact bucket's date
    // range - see routes/charts.py's historyPage, which owns the play list now
    // and only applies list-filtering for an explicit interval=custom range
    // (not the named day/week/month intervals).
    canvas.onclick = function (evt) {
      var rect = canvas.getBoundingClientRect();
      var mx = evt.clientX - rect.left, my = evt.clientY - rect.top;
      var hit = findBarAt(mx, my);
      if (hit && hit.d.rangeStart && hit.d.rangeEnd) {
        window.location.href = CU.bucketDrilldownUrl(hit.d.rangeStart, hit.d.rangeEnd);
      }
    };
  }

  function renderHeatmap() {
    CU.renderHeatmap(document.getElementById('heatmapChart'),
      (window.__chartData && window.__chartData.heatmap) || [],
      { emptyMessage: 'No listening data in this period yet.' });
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
        // Database/database.py's getArtistTrend, which picks a representative
        // id for same-named artists sharing one merged line.
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
      CU.renderLegend(legendEl, []);
      return;
    }

    var maxMs = 1;
    data.series.forEach(function (s) {
      maxMs = Math.max(maxMs, Math.max.apply(null, s.data));
    });
    var paddingLeft = valueAxisPadding(ctx, maxMs, msToShortLabel), paddingBottom = 26, paddingTop = 16, paddingRight = 16;
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

    CU.renderLegend(legendEl, data.series.slice(0, 2).map(function (series, si) {
      return { name: series.name, color: colors[si] };
    }));
  }

  function renderExplicitChart() {
    var canvas = document.getElementById('explicitChart');
    if (!canvas) return;
    // Guard __chartData itself: on /charts the canvas exists in the shell before
    // the deferred payload lands, and the resize handler calls this regardless.
    var data = window.__chartData && window.__chartData.explicitRatio;
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
    var data = window.__chartData && window.__chartData.completionStats;
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
      (window.__chartData && window.__chartData.decadeDistribution) || [],
      { emptyMessage: 'No album release information in this period.' });
  }

  function renderGenreChart() {
    // Horizontal bars (full label per row, not a cramped x-axis) - long
    // genre names never truncate into an ambiguous collision, like
    // "alternative rock"/"alternative metal" both cutting down to
    // "alternative…" used to.
    CU.renderHorizontalBars(document.getElementById('genreChart'),
      (window.__chartData && window.__chartData.genreDistribution) || [],
      { emptyMessage: 'No genre data for the plays in this period.', valueSuffix: ' plays' });
  }

  // Most-skipped songs/artists: the bar length is the skip RATE (share of
  // encounters), and the raw count rides in the label so both numbers are
  // visible - rate alone hides how much listening it is based on, count alone
  // just resurfaces whatever is played most. Both lists arrive sorted by that
  // rate (Database._byDisplayedSkipRate) and are drawn in payload order.
  function skipPairs(entries) {
    return (entries || []).map(function (entry) {
      var count = entry.skips + (entry.skips === 1 ? ' skip' : ' skips');
      return [entry.name + ' · ' + count, entry.skipPercent];
    });
  }

  function renderMostSkippedCharts() {
    var data = window.__chartData || {};
    var songs = skipPairs(data.mostSkippedSongs);
    var artists = skipPairs(data.mostSkippedArtists);

    // Nothing skipped at all in this range (a new library, or a narrow one) -
    // hide the whole row rather than show two empty frames.
    var grid = document.getElementById('mostSkippedGrid');
    if (grid) {
      grid.style.display = (songs.length || artists.length) ? 'grid' : 'none';
    }
    if (!songs.length && !artists.length) return;

    // "% skipped", not "% of plays skipped": the denominator is every time it
    // came up, skips included, so a song played 6 and skipped 4 reads 40% - as
    // a share of PLAYS the same row would be 67%.
    CU.renderHorizontalBars(document.getElementById('mostSkippedSongsChart'), songs,
      { emptyMessage: 'Nothing skipped in this period yet.', valueSuffix: '% skipped' });
    CU.renderHorizontalBars(document.getElementById('mostSkippedArtistsChart'), artists,
      { emptyMessage: 'Nothing skipped in this period yet.', valueSuffix: '% skipped' });
  }

  function renderAllCharts() {
    CU.refreshPalette();
    renderTimeSeriesChart();
    renderHeatmap();
    renderArtistTrend();
    renderComparisonMirror();
    renderExplicitChart();
    renderCompletionChart();
    renderDecadeChart();
    renderMostSkippedCharts();
    renderGenreChart();
  }

  window.renderTimeSeriesChart = renderTimeSeriesChart;
  // The Compare page re-renders the mirror chart after AJAX filter swaps.
  window.renderComparisonMirror = renderComparisonMirror;
  // The /charts page fetches its data after first paint and then drives
  // rendering itself (see charts-page.js), so it opts out of this initial
  // render via window.__deferInitialChartRender. /compare (which sets
  // window.__chartData inline before this script loads) renders immediately.
  window.renderAllCharts = renderAllCharts;

  if (!window.__deferInitialChartRender) {
    renderAllCharts();
  }

  //< the resize debounce and the theme-change repaint (see bindRepaint's
  //  comment in chart-utils.js for why the latter waits, and for the
  //  '#theme-selector' listener that never fired)
  CU.bindRepaint(renderAllCharts);
})();
