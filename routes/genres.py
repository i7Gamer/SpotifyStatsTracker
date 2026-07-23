"""The dedicated /genres page: a genre-profile overview (distribution bars,
share donut, genre-mix-over-time) plus a per-genre drill-down (stat strip,
monthly trend, listening clock, top artists, top tracks). All genre surfaces
gate behind the same Last.fm coverage unlock as Charts/Wrapped/Compare.

The page loads in two phases, like Charts: the initial GET renders a lightweight
shell (filter controls + empty chart canvases), and static/js/genres.js then
fetches the data via AJAX after first paint. A time-period filter (defaulting to
the user's profile default window) scopes every displayed chart/stat; the unlock
GATE, however, stays all-time (unlock is a library-wide achievement, like the
Overview coverage card), so a narrow window never hides the whole page.

Two AJAX shapes, both ?ajax=true: the default (scope=all) returns the full data
payload (overview datasets + chip row + the selected genre's detail); scope=detail
returns just the re-rendered detail partial + its two chart datasets, for the
chip-click drill-down swap. Neither reloads the whole page.

Follows the project's route convention: register(app, dashboard), handler
closures, add_url_rule. app-level constants are aliased at register() time.
"""
import logging

from flask import render_template, redirect, request, url_for, jsonify

import app as appmod
from Database.utils import msToString, convertToDatetime
from services.genre_gate import (
    emptyGenreCoverage, resolveGenreCoverage, genreGatePasses, resolveGenreDistribution,
    resolveGenreTrends, resolveGenreStats, resolveTopArtistsForGenre, resolveTopTracksForGenre,
    resolveGenreHeatmap, emptyHeatmapGrid, resolveGenreArtistCounts,
)

logger = logging.getLogger(__name__)


def register(app, dashboard):
    GENRE_PAGE_LIST_LIMIT = appmod.GENRE_PAGE_LIST_LIMIT
    GENRE_MIX_TREND_TOP_N = appmod.GENRE_MIX_TREND_TOP_N
    GENRE_PAGE_TOP_ARTISTS_LIMIT = appmod.GENRE_PAGE_TOP_ARTISTS_LIMIT
    GENRE_PAGE_TOP_TRACKS_LIMIT = appmod.GENRE_PAGE_TOP_TRACKS_LIMIT

    def _buildGenreDetail(db, username, selectedGenre, startDate, endDate, intervalLabel):
        """The per-genre drill-down context (stat strip + top lists) plus its
        two chart datasets (monthly trend line, listening-clock heatmap), all
        scoped to the selected date range. Shared by the full payload and the
        chip-click detail swap so they can't drift."""
        selectedTrend = resolveGenreTrends(db, [selectedGenre], startDate, endDate)
        clock = dashboard._embedHeatmapTextElements(resolveGenreHeatmap(db, selectedGenre, startDate, endDate))
        topArtists = resolveTopArtistsForGenre(db, selectedGenre, GENRE_PAGE_TOP_ARTISTS_LIMIT, startDate, endDate)
        topTracks = resolveTopTracksForGenre(db, selectedGenre, GENRE_PAGE_TOP_TRACKS_LIMIT, startDate, endDate)
        stats = resolveGenreStats(db, selectedGenre, startDate, endDate)
        firstTs = stats.get("firstPlayedTs")
        genreStatsView = {
            "plays": stats.get("plays", 0),
            "listenText": msToString(stats.get("listenMs", 0)),
            "sharePercent": stats.get("sharePercent", 0.0),
            "firstPlayedText": convertToDatetime(firstTs, tz=db.tz).strftime("%b %Y") if firstTs else "—",
        }
        return {
            # username is supplied by the caller (the AJAX branch re-adds it) so
            # it never collides with genres.html's own. intervalLabel lets the
            # detail partial's "Plays · <label>" heading match the filter.
            "context": {
                "selectedGenre": selectedGenre,
                "genreStats": genreStatsView,
                "topArtists": topArtists,
                "topTracks": topTracks,
                "intervalLabel": intervalLabel,
            },
            "selectedTrend": selectedTrend,
            "clock": clock,
        }

    def genresPage():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        settings = db.repo.getUserSettings(username)
        defaultWindow = settings.get("default_dashboard_window", "day")

        interval = dashboard._getValidInterval(request.args.get("interval", defaultWindow), default=defaultWindow)
        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")
        if interval == "custom" and not (customStart and customEnd):
            interval = defaultWindow
        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd, default=defaultWindow, tz=db.tz)
        intervalLabel = dashboard._getIntervalLabel(interval, customStart, customEnd)

        # Same instance-wide kill switch as the Charts genre section. The unlock
        # GATE is intentionally all-time (unlock is a library-wide achievement,
        # like the Overview coverage card): the selected window below only scopes
        # the displayed data, never whether the page is unlocked - so a narrow
        # window can't hide the whole page.
        lastfmEnabled = dashboard.repo.isLastfmGenreBackfillEnabled()
        genreCoverage = emptyGenreCoverage()
        genreUnlocked = False
        if lastfmEnabled:
            genreCoverage = resolveGenreCoverage(db, None, None)
            genreUnlocked = genreGatePasses(genreCoverage)

        # Lightweight shell: the disabled/locked/unlocked structure is decided
        # here (all-time gate, one cheap coverage query), but every per-range
        # data query is deferred to the ajax payload below, fetched by
        # static/js/genres.js after first paint and on each filter change.
        if request.args.get("ajax") != "true":
            return render_template(
                "genres.html",
                username=username,
                section="genres",
                lastfmEnabled=lastfmEnabled,
                genreCoverage=genreCoverage,
                genreUnlocked=genreUnlocked,
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                intervalLabel=intervalLabel,
                defaultWindow=defaultWindow,
            )

        # ---- AJAX data payload (scoped to the selected window) ----
        if not genreUnlocked:
            return jsonify({"ok": False})

        distribution = resolveGenreDistribution(db, startDate, endDate, GENRE_PAGE_LIST_LIMIT)
        genreNames = list(distribution.keys())
        requested = request.args.get("genre")
        selectedGenre = requested if requested in distribution else (genreNames[0] if genreNames else None)

        # Chip-click drill-down: just the re-rendered detail partial + its two
        # chart datasets (the overview charts and chips are unchanged by a
        # genre switch, so they're never re-sent).
        if request.args.get("scope") == "detail":
            detail = _buildGenreDetail(db, username, selectedGenre, startDate, endDate, intervalLabel) if selectedGenre else None
            if detail is None:
                return jsonify({"ok": False})
            return jsonify({
                "ok": True,
                "genre": selectedGenre,
                "detailHtml": render_template("_genre_detail.html", username=username, **detail["context"]),
                "selectedTrend": detail["selectedTrend"],
                "clock": detail["clock"],
            })

        # Full payload: overview datasets + chip row + the selected detail. The
        # chip row is re-sent because a range change changes which genres have
        # plays (and thus appear). The mix-over-time trend is computed before the
        # per-genre detail so its top-N query runs first.
        mixTrend = resolveGenreTrends(db, genreNames[:GENRE_MIX_TREND_TOP_N], startDate, endDate)
        # Breadth (distinct artists per genre) stays all-time - the underlying
        # count has no date-range variant - so it reads as overall taste breadth
        # for the genres present in the range. Ranked most-artists-first.
        breadth = resolveGenreArtistCounts(db, genreNames)
        breadthPairs = sorted(breadth.items(), key=lambda kv: (-kv[1], kv[0]))

        detail = _buildGenreDetail(db, username, selectedGenre, startDate, endDate, intervalLabel) if selectedGenre else None
        detailHtml = render_template("_genre_detail.html", username=username, **detail["context"]) if detail else ""
        return jsonify({
            "ok": True,
            "genre": selectedGenre,
            "intervalLabel": intervalLabel,
            "distributionPairs": list(distribution.items()),
            "breadthPairs": breadthPairs,
            "mixTrend": mixTrend,
            "chipsHtml": render_template("_genre_chips.html", genreNames=genreNames, selectedGenre=selectedGenre),
            "detailHtml": detailHtml,
            "selectedTrend": detail["selectedTrend"] if detail else {"buckets": [], "series": []},
            "clock": detail["clock"] if detail else emptyHeatmapGrid(),
        })
    app.add_url_rule("/genres", "genresPage", genresPage, methods=["GET"])
