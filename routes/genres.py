"""The dedicated /genres page: a genre-profile overview (distribution bars,
share donut, genre-mix-over-time) plus a per-genre drill-down (stat strip,
monthly trend, listening clock, top artists, top tracks). All genre surfaces
gate behind the same Last.fm coverage unlock as Charts/Wrapped/Compare.

Switching the drill-down genre is an AJAX swap (?ajax=true returns JSON: the
re-rendered detail partial plus the two per-genre chart datasets), so it never
reloads the whole page - the same fade-and-swap pattern as Compare/Wrapped.

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

    def _buildGenreDetail(db, username, selectedGenre):
        """The per-genre drill-down context (stat strip + top lists) plus its
        two chart datasets (monthly trend line, listening-clock heatmap). Shared
        by the full-page render and the AJAX genre swap so they can't drift."""
        selectedTrend = resolveGenreTrends(db, [selectedGenre], None, None)
        clock = dashboard._embedHeatmapTextElements(resolveGenreHeatmap(db, selectedGenre))
        topArtists = resolveTopArtistsForGenre(db, selectedGenre, GENRE_PAGE_TOP_ARTISTS_LIMIT)
        topTracks = resolveTopTracksForGenre(db, selectedGenre, GENRE_PAGE_TOP_TRACKS_LIMIT)
        stats = resolveGenreStats(db, selectedGenre, None, None)
        firstTs = stats.get("firstPlayedTs")
        genreStatsView = {
            "plays": stats.get("plays", 0),
            "listenText": msToString(stats.get("listenMs", 0)),
            "sharePercent": stats.get("sharePercent", 0.0),
            "firstPlayedText": convertToDatetime(firstTs, tz=db.tz).strftime("%b %Y") if firstTs else "—",
        }
        return {
            # username is supplied by the caller (the page passes it once; the
            # AJAX branch re-adds it) so it never collides with genres.html's own.
            "context": {
                "selectedGenre": selectedGenre,
                "genreStats": genreStatsView,
                "topArtists": topArtists,
                "topTracks": topTracks,
            },
            "selectedTrend": selectedTrend,
            "clock": clock,
        }

    def genresPage():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        isAjax = request.args.get("ajax") == "true"

        # Same instance-wide kill switch + coverage gate as the Charts genre
        # section: skip every genre query when the admin disabled Last.fm
        # genres or the user's coverage hasn't cleared the unlock threshold.
        lastfmEnabled = dashboard.repo.isLastfmGenreBackfillEnabled()
        genreCoverage = emptyGenreCoverage()
        genreUnlocked = False
        distribution = {}
        genreNames = []
        selectedGenre = None
        mixTrend = {"buckets": [], "series": []}
        breadthPairs = []
        detail = None

        if lastfmEnabled:
            genreCoverage = resolveGenreCoverage(db, None, None)
            genreUnlocked = genreGatePasses(genreCoverage)
            if genreUnlocked:
                distribution = resolveGenreDistribution(db, None, None, GENRE_PAGE_LIST_LIMIT)
                genreNames = list(distribution.keys())
                mixTrend = resolveGenreTrends(db, genreNames[:GENRE_MIX_TREND_TOP_N], None, None)
                # Breadth (distinct artists per genre) - the companion to the
                # play-weighted share donut. Ranked most-artists-first, so it
                # reads differently from the plays-ranked distribution.
                breadth = resolveGenreArtistCounts(db, genreNames)
                breadthPairs = sorted(breadth.items(), key=lambda kv: (-kv[1], kv[0]))

                requested = request.args.get("genre")
                selectedGenre = requested if requested in distribution else (genreNames[0] if genreNames else None)
                if selectedGenre:
                    detail = _buildGenreDetail(db, username, selectedGenre)

        # AJAX genre swap: just the re-rendered detail partial + its chart data.
        if isAjax:
            if not genreUnlocked or detail is None:
                return jsonify({"ok": False})
            return jsonify({
                "ok": True,
                "genre": selectedGenre,
                "detailHtml": render_template("_genre_detail.html", username=username, **detail["context"]),
                "selectedTrend": detail["selectedTrend"],
                "clock": detail["clock"],
            })

        detailContext = detail["context"] if detail else {
            "selectedGenre": selectedGenre,
            "genreStats": None, "topArtists": [], "topTracks": [],
        }
        return render_template(
            "genres.html",
            username=username,
            section="genres",
            lastfmEnabled=lastfmEnabled,
            genreCoverage=genreCoverage,
            genreUnlocked=genreUnlocked,
            distributionPairs=list(distribution.items()),
            breadthPairs=breadthPairs,
            genreNames=genreNames,
            mixTrend=mixTrend,
            selectedTrend=detail["selectedTrend"] if detail else {"buckets": [], "series": []},
            clock=detail["clock"] if detail else emptyHeatmapGrid(),
            **detailContext,
        )
    app.add_url_rule("/genres", "genresPage", genresPage, methods=["GET"])
