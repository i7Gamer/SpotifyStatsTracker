"""The dedicated /genres page: a genre-profile overview (distribution bars,
share donut, genre-mix-over-time) plus a per-genre drill-down (stat strip,
monthly trend, top artists, top tracks). All genre surfaces gate behind the
same Last.fm coverage unlock as Charts/Wrapped/Compare.

Follows the project's route convention: register(app, dashboard), handler
closures, add_url_rule. app-level constants are aliased at register() time.
"""
import logging

from flask import render_template, redirect, request, url_for

import app as appmod
from Database.utils import msToString, convertToDatetime
from services.genre_gate import (
    emptyGenreCoverage, resolveGenreCoverage, genreGatePasses, resolveGenreDistribution,
    resolveGenreTrends, resolveGenreStats, resolveTopArtistsForGenre, resolveTopTracksForGenre,
)

logger = logging.getLogger(__name__)


def register(app, dashboard):
    GENRE_PAGE_LIST_LIMIT = appmod.GENRE_PAGE_LIST_LIMIT
    GENRE_MIX_TREND_TOP_N = appmod.GENRE_MIX_TREND_TOP_N
    GENRE_PAGE_TOP_ARTISTS_LIMIT = appmod.GENRE_PAGE_TOP_ARTISTS_LIMIT
    GENRE_PAGE_TOP_TRACKS_LIMIT = appmod.GENRE_PAGE_TOP_TRACKS_LIMIT

    def genresPage():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        # Same instance-wide kill switch + coverage gate as the Charts genre
        # section: skip every genre query when the admin disabled Last.fm
        # genres or the user's coverage hasn't cleared the unlock threshold.
        lastfmEnabled = dashboard.repo.isLastfmGenreBackfillEnabled()
        genreCoverage = emptyGenreCoverage()
        genreUnlocked = False
        distributionPairs = []
        genreNames = []
        selectedGenre = None
        mixTrend = {"buckets": [], "series": []}
        selectedTrend = {"buckets": [], "series": []}
        genreStatsView = None
        topArtists = []
        topTracks = []

        if lastfmEnabled:
            genreCoverage = resolveGenreCoverage(db, None, None)
            genreUnlocked = genreGatePasses(genreCoverage)
            if genreUnlocked:
                distribution = resolveGenreDistribution(db, None, None, GENRE_PAGE_LIST_LIMIT)
                genreNames = list(distribution.keys())
                distributionPairs = list(distribution.items())
                mixTrend = resolveGenreTrends(db, genreNames[:GENRE_MIX_TREND_TOP_N], None, None)

                requested = request.args.get("genre")
                selectedGenre = requested if requested in distribution else (genreNames[0] if genreNames else None)
                if selectedGenre:
                    selectedTrend = resolveGenreTrends(db, [selectedGenre], None, None)
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

        return render_template(
            "genres.html",
            username=username,
            section="genres",
            lastfmEnabled=lastfmEnabled,
            genreCoverage=genreCoverage,
            genreUnlocked=genreUnlocked,
            distributionPairs=distributionPairs,
            genreNames=genreNames,
            selectedGenre=selectedGenre,
            mixTrend=mixTrend,
            selectedTrend=selectedTrend,
            genreStats=genreStatsView,
            topArtists=topArtists,
            topTracks=topTracks,
        )
    app.add_url_rule("/genres", "genresPage", genresPage, methods=["GET"])
