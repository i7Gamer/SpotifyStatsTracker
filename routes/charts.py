"""Main stats pages: the public /overview, the dashboard index (/), the Top
Songs/Albums/Artists lists, the /charts analytics page, and the song/artist/
album detail pages.

Extracted verbatim from app.py. Genre-gate/coverage helpers come from services/;
the app-level PAGE_SIZE / CHART_* constants are aliased from the app module at
register() time. Every stats/pagination/embed helper is reached through the
dashboard instance.
"""
import logging

from flask import render_template, redirect, request, url_for, session

import app as appmod
from Database.utils import msToString
from services.genre_gate import (
    emptyGenreCoverage, resolveGenreCoverage, genreGatePasses, resolveGenreDistribution,
    emptyBiographyCoverage, resolveBiographyCoverage,
)

logger = logging.getLogger(__name__)


def register(app, dashboard):
    PAGE_SIZE = appmod.PAGE_SIZE
    CHART_ARTIST_TREND_TOP_N = appmod.CHART_ARTIST_TREND_TOP_N
    CHART_TOP_GENRES_LIMIT = appmod.CHART_TOP_GENRES_LIMIT

    def overviewPage():
        from datetime import datetime
        # Intentionally unauthenticated: aggregate counts/DB size carry no
        # per-user listening data, so they're shown to any visitor as a
        # public "is this instance alive" summary - only the per-user
        # status widget below is gated on login. The full multi-user
        # table and every admin-only setting live on /admin now.
        global_stats = dashboard.repo.getGlobalDatabaseStats()

        total_time_ms = global_stats.get("total_time_ms", 0)
        total_hours = total_time_ms // (1000 * 60 * 60)
        if total_hours >= 24:
            days = total_hours // 24
            hours = total_hours % 24
            global_time_text = f"{days}d {hours}h"
        else:
            global_time_text = f"{total_hours}h"

        db_size_bytes = global_stats.get("db_size_bytes", 0)
        if db_size_bytes >= 1024 * 1024 * 1024:
            global_size_text = f"{db_size_bytes / (1024 * 1024 * 1024):.2f} GB"
        elif db_size_bytes >= 1024 * 1024:
            global_size_text = f"{db_size_bytes / (1024 * 1024):.2f} MB"
        else:
            global_size_text = f"{db_size_bytes / 1024:.1f} KB"

        email = session.get("email")
        is_logged_in = email is not None and dashboard.is_user_logged_in(email)

        # Instance-wide (not per-user), so it's resolved regardless of
        # login state - it also gates the public "Last.fm Genre Backfill"
        # info card further down the page.
        lastfm_enabled = dashboard.repo.isLastfmGenreBackfillEnabled()
        artist_bio_enabled = dashboard.repo.isArtistBioEnabled()
        album_bio_enabled = dashboard.repo.isAlbumBioEnabled()

        # Get current user's timezone for consistent date display
        current_user_tz = None
        current_username = None
        genre_coverage = emptyGenreCoverage()
        genre_unlocked = False
        genre_worker = {"configured": False, "running": False}
        biography_coverage = emptyBiographyCoverage()
        biography_worker = {"artist": {"configured": False, "running": False},
                            "album": {"configured": False, "running": False}}
        if is_logged_in:
            current_username = dashboard.get_username_for_email(email) or dashboard.get_or_create_user(email)
            current_db = dashboard.get_user_db(current_username, email)
            current_user_tz = current_db.tz if current_db else None
            if current_db is not None and lastfm_enabled:
                # All-time coverage: the progress card tracks the whole
                # library, unlike the range-scoped gates on charts/wrapped.
                genre_coverage = resolveGenreCoverage(current_db, None, None)
                genre_unlocked = genreGatePasses(genre_coverage)
                try:
                    workerStatus = current_db.getLastfmWorkerStatus()
                    if isinstance(workerStatus, dict):
                        genre_worker = {"configured": bool(workerStatus.get("configured")),
                                        "running": bool(workerStatus.get("running"))}
                except Exception as e:
                    logger.warning("Last.fm worker status lookup failed: %s", e)
            if current_db is not None and (artist_bio_enabled or album_bio_enabled):
                biography_coverage = resolveBiographyCoverage(current_db, current_username)
                try:
                    artistWorkerStatus = current_db.getLastfmBiographyWorkerStatus()
                    if isinstance(artistWorkerStatus, dict):
                        biography_worker["artist"] = {"configured": bool(artistWorkerStatus.get("configured")),
                                                      "running": bool(artistWorkerStatus.get("running"))}
                except Exception as e:
                    logger.warning("Last.fm artist biography worker status lookup failed: %s", e)
                try:
                    albumWorkerStatus = current_db.getLastfmAlbumBiographyWorkerStatus()
                    if isinstance(albumWorkerStatus, dict):
                        biography_worker["album"] = {"configured": bool(albumWorkerStatus.get("configured")),
                                                     "running": bool(albumWorkerStatus.get("running"))}
                except Exception as e:
                    logger.warning("Last.fm album biography worker status lookup failed: %s", e)

        # The logged-in user's own sync/backfill state, as a simple
        # three-badge summary - not a table (the full multi-user table
        # with per-account admin controls lives on /admin now).
        your_status = None
        if is_logged_in:
            own = dashboard.repo.getAllUsersDetails(username=current_username)
            if own:
                u = own[0]
                if u["cookies_json"] and current_db is not None:
                    health = current_db.getListenerHealth()
                    sync_status = health.get("status", "UNKNOWN")
                else:
                    sync_status = "Not Configured"
                has_api = bool(u["spotify_client_id"] and u["spotify_refresh_token"])
                needs_reauth = bool(u.get("spotify_needs_reauth"))
                your_status = {
                    "sync_status": sync_status,
                    "spotify_api_status": "Needs Re-Auth" if (has_api and needs_reauth) else ("Configured" if has_api else "Not Configured"),
                    #< .get(): raw row presence check only - the stored key
                    #  is encrypted and never needs decrypting here
                    "lastfm_api_status": "Configured" if u.get("lastfm_api_key") else "Not Configured",
                }

        # One row per entity kind for the combined "Biography Backfill
        # Progress" card (templates/_biography_progress.html) - built
        # here rather than assembled in Jinja so the template stays a
        # dumb iteration over a pre-shaped list, same spirit as how
        # users_list is built above.
        biography_rows = [
            {"label": "Artist", "enabled": artist_bio_enabled, "worker": biography_worker["artist"],
             **biography_coverage["artist"]},
            {"label": "Album", "enabled": album_bio_enabled, "worker": biography_worker["album"],
             **biography_coverage["album"]},
        ]

        return render_template(
            "overview.html",
            global_stats=global_stats,
            global_time_text=global_time_text,
            global_size_text=global_size_text,
            is_logged_in=is_logged_in,
            your_status=your_status,
            spotify_backfill_enabled=dashboard.repo.isSpotifyApiBackfillEnabled(),
            genre_coverage=genre_coverage,
            genre_unlocked=genre_unlocked,
            genre_worker=genre_worker,
            lastfm_enabled=lastfm_enabled,
            biography_rows=biography_rows,
            section="overview"
        )
    app.add_url_rule("/overview", "overviewPage", overviewPage, methods=["GET"])

    def dashboardIndex():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        settings = db.repo.getUserSettings(username)
        default_window = settings.get("default_dashboard_window", "day")

        page = dashboard._getPageParam()
        searchQuery = request.args.get("q", "")
        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")

        interval = request.args.get("interval", default_window)
        if interval == "":
            interval = default_window

        if interval == "custom" and not (customStart and customEnd):
            interval = "all time"

        intervalLabel = dashboard._getIntervalLabel(interval, customStart, customEnd)
        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd, default="day", tz=db.tz)

        # Only an explicit custom range (typically a chart click-through -
        # see static/js/charts.js) scopes the play-history list below. The
        # named intervals (day/week/...) only ever scoped the stats cards
        # above; making every default/named-interval visit also filter the
        # list would silently hide most of a user's history behind
        # whatever their default dashboard window happens to be.
        listStartDate = startDate if interval == "custom" else None
        listEndDate = endDate if interval == "custom" else None

        if searchQuery:
            # Matching and pagination both happen in SQL (Repository.searchPlays)
            # instead of fetching every play ever recorded and filtering in Python.
            totalCount = db.searchEntriesCount(searchQuery, startDate=listStartDate, endDate=listEndDate)
            page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
            tracks = db.searchEntries(searchQuery, count=PAGE_SIZE, startIndex=startIndex,
                                      startDate=listStartDate, endDate=listEndDate)
        else:
            # Only materialize the page being shown - joining full track
            # metadata onto every entry ever recorded on every request gets
            # slow once the history grows large.
            totalCount = db.getEntriesCount(startDate=listStartDate, endDate=listEndDate)
            page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
            tracks = db.getEntriesFromNew(count=PAGE_SIZE, startIndex=startIndex,
                                          startDate=listStartDate, endDate=listEndDate)
        tracks = dashboard._embedSongsTextElements(tracks)
        tracks = dashboard._attachGenres(db, tracks, "track")

        stats = db.getOverallStats(startDate, endDate)

        totalDurationText = msToString(stats["totalDurationMs"],
                                       hideSecondsAboveHours=appmod.LISTEN_TIME_HIDE_SECONDS_ABOVE_HOURS)

        currentTopSong = dashboard._embedTopSongTextElements(stats["currentTopSongs"][0], sortBy="plays", totalPlays=stats["totalSongsPlayed"], totalMs=stats["totalDurationMs"]) if stats["currentTopSongs"] else None
        currentTopArtist = dashboard._embedArtistTextElement(stats["currentTopArtists"][0], sortBy="totalTimeListened", totalPlays=stats["totalSongsPlayed"], totalMs=stats["totalDurationMs"]) if stats["currentTopArtists"] else None

        totalSongsChangeText, totalSongsChangeClass = dashboard._getChangeText(stats["totalSongsPlayed"], stats["previousSongsPlayed"])
        totalListenChangeText, totalListenChangeClass = dashboard._getChangeText(stats["totalDurationMs"], stats["previousDurationMs"])

        pagination = dashboard._buildPaginationContext(
            "dashboard",
            page,
            totalPages,
            totalCount,
            q=searchQuery,
            interval=interval,
            startDate=customStart,
            endDate=customEnd,
        )

        creds = db.getUserSpotifyCredentials() or {}
        has_api = bool(creds.get("client_id") and creds.get("client_secret"))
        is_authenticated = bool(creds.get("refresh_token"))

        # Unfiltered dashboard cards (independent of the interval/date-range
        # filter above): live streak, "on this day" resurfacing, and genre-
        # based recommendations. Recommendations reuse the same Last.fm kill
        # switch + coverage unlock as the Charts/Genres genre surfaces.
        currentStreak = db.getCurrentStreak()
        onThisDay = db.getOnThisDay(limit=appmod.ON_THIS_DAY_YEARS_LIMIT)
        lastfmGenreEnabled = dashboard.repo.isLastfmGenreBackfillEnabled()
        recommendations = []
        recommendationsUnlocked = False
        if lastfmGenreEnabled:
            recommendationsUnlocked = genreGatePasses(resolveGenreCoverage(db, None, None))
            if recommendationsUnlocked:
                recommendations = db.getRecommendedArtists(
                    limit=appmod.RECOMMENDATION_ARTIST_LIMIT,
                    genrePool=appmod.RECOMMENDATION_GENRE_POOL,
                    excludeTopN=appmod.RECOMMENDATION_EXCLUDE_TOP_N,
                )

        return render_template(
            "tracks.html",
            currentStreak=currentStreak,
            onThisDay=onThisDay,
            lastfmGenreEnabled=lastfmGenreEnabled,
            recommendations=recommendations,
            recommendationsUnlocked=recommendationsUnlocked,
            tracks=tracks,
            totalSongsPlayed=stats["totalSongsPlayed"],
            totalListenTime=totalDurationText,
            totalSongsChangeText=totalSongsChangeText,
            totalSongsChangeClass=totalSongsChangeClass,
            totalListenChangeText=totalListenChangeText,
            totalListenChangeClass=totalListenChangeClass,
            currentTopSong=currentTopSong,
            currentTopArtist=currentTopArtist,
            intervalLabel=intervalLabel,
            username=username,
            startIndex=startIndex,
            section="dashboard",
            interval=interval,
            customStart=customStart,
            customEnd=customEnd,
            has_api=has_api,
            is_authenticated=is_authenticated,
            **pagination,
        )
    app.add_url_rule("/", "dashboard", dashboardIndex, methods=["GET"])

    def topSongsPage():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        page = dashboard._getPageParam()
        searchQuery = request.args.get("q", "")
        sortBy = dashboard._getSortByParam()
        interval = request.args.get("interval", "")
        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")

        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd, default="all time", tz=db.tz)
        # totalPlays/totalMs are a whole-range aggregate regardless of search -
        # a cheap dedicated query instead of summing every song's metadata.
        totalPlays, totalMs = db.getPlayTotals(startDate, endDate)
        uniqueSongs = db.getSongsCount(startDate, endDate)

        # Only materialize the page being shown - SQL-level LIMIT/OFFSET and
        # WHERE-clause matching (see Repository.getSongsPage) instead of
        # sorting+hydrating+filtering every song ever played in Python.
        if searchQuery:
            totalCount = db.getSongsCount(startDate, endDate, searchQuery=searchQuery)
        else:
            totalCount = uniqueSongs
        page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
        tracks = db.getTopSongs(startDate=startDate, endDate=endDate, by=sortBy,
                                 limit=PAGE_SIZE, offset=startIndex, searchQuery=searchQuery)

        pagination = dashboard._buildPaginationContext(
            "topSongsPage",
            page,
            totalPages,
            totalCount,
            q=searchQuery,
            sortBy=sortBy,
            interval=interval,
            startDate=customStart,
            endDate=customEnd,
        )

        tracks = dashboard._embedSongsTextElements(tracks)
        tracks = dashboard._embedTopSongsTextElements(tracks, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        tracks = dashboard._attachGenres(db, tracks, "track")

        return render_template(
            "top_songs.html",
            tracks=tracks,
            username=username,
            totalPlays=totalPlays,
            totalTime=msToString(totalMs),
            uniqueSongs=uniqueSongs,
            startIndex=startIndex,
            section="top_songs",
            sortBy=sortBy,
            interval=interval,
            customStart=customStart,
            customEnd=customEnd,
            **pagination,
        )
    app.add_url_rule("/top-songs", "topSongsPage", topSongsPage, methods=["GET"])

    def topAlbumsPage():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        page = dashboard._getPageParam()
        searchQuery = request.args.get("q", "")
        sortBy = dashboard._getSortByParam()
        interval = request.args.get("interval", "")
        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")

        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd, default="all time", tz=db.tz)
        totalPlays, totalMs = db.getPlayTotals(startDate, endDate)
        uniqueAlbums = db.getAlbumsCount(startDate, endDate)

        # Only materialize the page being shown - SQL-level LIMIT/OFFSET and
        # WHERE-clause matching (see Repository.getAlbumsPage) instead of
        # sorting+hydrating+filtering every album ever played in Python.
        if searchQuery:
            totalCount = db.getAlbumsCount(startDate, endDate, searchQuery=searchQuery)
        else:
            totalCount = uniqueAlbums
        page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
        albums = db.getTopAlbums(startDate=startDate, endDate=endDate, by=sortBy,
                                  limit=PAGE_SIZE, offset=startIndex, searchQuery=searchQuery)

        pagination = dashboard._buildPaginationContext(
            "topAlbumsPage",
            page,
            totalPages,
            totalCount,
            q=searchQuery,
            sortBy=sortBy,
            interval=interval,
            startDate=customStart,
            endDate=customEnd,
        )

        albums = dashboard._embedAlbumsTextElements(albums, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        albums = dashboard._attachGenres(db, albums, "album")

        return render_template(
            "top_albums.html",
            tracks=albums,
            username=username,
            totalPlays=totalPlays,
            totalTime=msToString(totalMs),
            uniqueAlbums=uniqueAlbums,
            startIndex=startIndex,
            section="top_albums",
            sortBy=sortBy,
            interval=interval,
            customStart=customStart,
            customEnd=customEnd,
            **pagination,
        )
    app.add_url_rule("/top-albums", "topAlbumsPage", topAlbumsPage, methods=["GET"])

    def topArtistsPage():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        page = dashboard._getPageParam()
        searchQuery = request.args.get("q", "")
        sortBy = dashboard._getSortByParam()
        interval = request.args.get("interval", "")
        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")

        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd, default="all time", tz=db.tz)
        # totalPlays/totalUnique/totalMs are the whole (date-range-scoped) top
        # list's totals regardless of search - mirrors getPlayTotals()'s role
        # for the songs/albums pages, computed via a dedicated SQL aggregate
        # instead of fetching every artist and summing in Python.
        totalPlays, totalUnique, totalMs = db.getArtistTotals(startDate, endDate)
        uniqueArtists = db.getArtistsCount(startDate, endDate)

        # Only materialize the page being shown - SQL-level LIMIT/OFFSET
        # instead of sorting+hydrating every artist ever played.
        if searchQuery:
            totalCount = db.getArtistsCount(startDate, endDate, searchQuery=searchQuery)
        else:
            totalCount = uniqueArtists
        page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
        artists = db.getTopArtists(startDate=startDate, endDate=endDate, by=sortBy,
                                    limit=PAGE_SIZE, offset=startIndex, searchQuery=searchQuery)

        artists = dashboard._embedArtistsTextElements(artists, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        artists = dashboard._attachGenres(db, artists, "artist")
        pagination = dashboard._buildPaginationContext(
            "topArtistsPage",
            page,
            totalPages,
            totalCount,
            q=searchQuery,
            sortBy=sortBy,
            interval=interval,
            startDate=customStart,
            endDate=customEnd,
        )

        return render_template(
            "top_artists.html",
            tracks=artists,
            username=username,
            totalPlays=totalPlays,
            totalUnique=totalUnique,
            uniqueArtists=uniqueArtists,
            totalTime=msToString(totalMs),
            startIndex=startIndex,
            section="top_artists",
            sortBy=sortBy,
            interval=interval,
            customStart=customStart,
            customEnd=customEnd,
            **pagination,
        )
    app.add_url_rule("/top-artists", "topArtistsPage", topArtistsPage, methods=["GET"])

    def chartsPage():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        interval = dashboard._getValidInterval(request.args.get("interval", "month"), default="month")
        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")
        if interval == "custom" and not (customStart and customEnd):
            interval = "month"
        groupBy = dashboard._getValidGroupBy(request.args.get("groupBy", "day"))

        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd, default="month", tz=db.tz)
        intervalLabel = dashboard._getIntervalLabel(interval, customStart, customEnd)

        isSingleDayView = interval in ("day", "today")
        lastDayDate = startDate.strftime("%Y-%m-%d") if isSingleDayView and startDate else None

        timeSeriesGroupBy = "hour" if isSingleDayView else groupBy

        timeSeries = dashboard._embedTimeSeriesTextElements(
            db.getListeningTimeSeries(startDate=startDate, endDate=endDate, groupBy=timeSeriesGroupBy),
            groupBy=timeSeriesGroupBy,
        )
        heatmap = dashboard._embedHeatmapTextElements(db.getHourOfDayHeatmap(startDate=startDate, endDate=endDate))
        artistTrend = None if isSingleDayView else db.getArtistTrend(startDate=startDate, endDate=endDate, topN=CHART_ARTIST_TREND_TOP_N, groupBy=groupBy)

        explicitRatio = db.getExplicitRatio(startDate=startDate, endDate=endDate)
        # Flask's JSON provider sorts dict keys alphabetically on
        # serialization (app.json.sort_keys, on by default) - a {label:
        # value} dict handed to |tojson loses whatever order the SQL
        # produced. A JSON array preserves element order regardless, so
        # both bar-chart datasets are shipped as [label, value] pairs
        # instead (see renderCategoryBarChart in charts.js).
        decadeDistribution = list(db.getReleaseDecadeDistribution(startDate=startDate, endDate=endDate).items())
        completionStats = db.getCompletionStats(startDate=startDate, endDate=endDate)

        # The admin's instance-wide kill switch: checked before spending
        # any genre queries, and the whole Top Genres section (chart AND
        # its locked-progress fallback) hides on the template side when
        # this is False - showing "add a Last.fm key" for a feature the
        # admin turned off would be misleading.
        lastfmEnabled = dashboard.repo.isLastfmGenreBackfillEnabled()
        genreCoverage = emptyGenreCoverage()
        genreUnlocked = False
        genreDistribution = None
        if lastfmEnabled:
            genreCoverage = resolveGenreCoverage(db, startDate, endDate)
            genreUnlocked = genreGatePasses(genreCoverage)
            if genreUnlocked:
                distribution = resolveGenreDistribution(db, startDate, endDate,
                                                        CHART_TOP_GENRES_LIMIT)
                # Selection stays the same top-N by plays as every other
                # genre surface (Wrapped/Compare keep descending) - only this
                # bar chart's own display order is reversed to read ascending.
                genreDistribution = list(reversed(distribution.items()))

        return render_template(
            "charts.html",
            username=username,
            section="charts",
            interval=interval,
            customStart=customStart,
            customEnd=customEnd,
            groupBy=groupBy,
            intervalLabel=intervalLabel,
            lastDayDate=lastDayDate,
            timeSeries=timeSeries,
            heatmap=heatmap,
            artistTrend=artistTrend,
            explicitRatio=explicitRatio,
            decadeDistribution=decadeDistribution,
            completionStats=completionStats,
            genreCoverage=genreCoverage,
            genreUnlocked=genreUnlocked,
            genreDistribution=genreDistribution,
            lastfmEnabled=lastfmEnabled,
        )
    app.add_url_rule("/charts", "chartsPage", chartsPage, methods=["GET"])

    def songDetailPage(track_id):
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        song = db.getSong(track_id)
        if song is None:
            return redirect(url_for("topSongsPage"))

        groupBy = dashboard._getValidGroupBy(request.args.get("groupBy", "week"), default="week")

        song = dashboard._embedSongTextElements(song)
        song = dashboard._embedTopSongTextElements(song)
        song = dashboard._attachGenres(db, [song], "track")[0]

        timeSeries = dashboard._embedTimeSeriesTextElements(
            db.getListeningTimeSeries(trackId=track_id, groupBy=groupBy)
        )
        heatmap = dashboard._embedHeatmapTextElements(db.getHourOfDayHeatmap(trackId=track_id))

        return render_template(
            "song_detail.html",
            song=song,
            username=username,
            groupBy=groupBy,
            timeSeries=timeSeries,
            heatmap=heatmap,
            section="top_songs",
            success=request.args.get("success"),
            error=request.args.get("error"),
        )
    app.add_url_rule("/song/<track_id>", "songDetailPage", songDetailPage, methods=["GET"])

    def artistDetailPage(artist_id):
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        artist = db.getArtist(artist_id)
        if artist is None:
            return redirect(url_for("topArtistsPage"))

        groupBy = dashboard._getValidGroupBy(request.args.get("groupBy", "week"), default="week")

        songs = db.getSongsStats(sortBy="plays", artistId=artist_id)
        firstSong = min(songs, key=lambda s: s.get("firstListenedAt") or float("inf")) if songs else None
        firstSongName = firstSong.get("name") if firstSong else None

        songs = dashboard._embedSongsTextElements(songs)
        songs = dashboard._embedTopSongsTextElements(
            songs, sortBy="plays", totalPlays=artist.get("plays", 0), totalMs=artist.get("totalTimeListened", 0)
        )
        songs = dashboard._attachGenres(db, songs, "track")
        artist = dashboard._embedArtistTextElement(artist)
        artist = dashboard._attachGenres(db, [artist], "artist")[0]

        # lazyFetchArtistBio no-ops (and skips fetching) when the admin's
        # instance-wide toggle is off, same contract as the Last.fm genre
        # backfill kill switch - but the displayed bio is suppressed here
        # too, so disabling the feature also hides an artist's
        # already-fetched bio, not just new ones.
        db.lazyFetchArtistBio(artist_id, artist.get("name", ""))
        artist["bio"] = db.getArtistBio(artist_id) if dashboard.repo.isArtistBioEnabled() else None

        timeSeries = dashboard._embedTimeSeriesTextElements(
            db.getListeningTimeSeries(artistId=artist_id, groupBy=groupBy)
        )

        return render_template(
            "artist_detail.html",
            artist=artist,
            songs=songs,
            firstSongName=firstSongName,
            username=username,
            groupBy=groupBy,
            timeSeries=timeSeries,
            section="top_artists",
            success=request.args.get("success"),
            error=request.args.get("error"),
        )
    app.add_url_rule("/artist/<artist_id>", "artistDetailPage", artistDetailPage, methods=["GET"])

    def albumDetailPage(album_id):
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        album = db.getAlbum(album_id)
        if album is None:
            return redirect(url_for("topAlbumsPage"))

        groupBy = dashboard._getValidGroupBy(request.args.get("groupBy", "week"), default="week")

        songs = db.getSongsStats(sortBy="plays", albumId=album_id)
        firstSong = min(songs, key=lambda s: s.get("firstListenedAt") or float("inf")) if songs else None
        firstSongName = firstSong.get("name") if firstSong else None

        songs = dashboard._embedSongsTextElements(songs)
        songs = dashboard._embedTopSongsTextElements(
            songs, sortBy="plays", totalPlays=album.get("plays", 0), totalMs=album.get("totalTimeListened", 0)
        )
        songs = dashboard._attachGenres(db, songs, "track")
        album = dashboard._embedAlbumTextElements(album)
        album = dashboard._attachGenres(db, [album], "album")[0]

        # Mirrors artistDetailPage's bio wiring: lazyFetchAlbumBio no-ops
        # (and skips fetching) when the admin's instance-wide toggle is
        # off, and the displayed bio is suppressed here too, so disabling
        # the feature also hides an album's already-fetched bio. The
        # primary artist (album.getinfo needs one) comes from the
        # already-loaded artists list.
        primaryArtists = album.get("artists") or []
        primaryArtistName = primaryArtists[0].get("name", "") if primaryArtists else ""
        if primaryArtistName:
            db.lazyFetchAlbumBio(album_id, album.get("name", ""), primaryArtistName)
        album["bio"] = db.getAlbumBio(album_id) if dashboard.repo.isAlbumBioEnabled() else None

        timeSeries = dashboard._embedTimeSeriesTextElements(
            db.getListeningTimeSeries(albumId=album_id, groupBy=groupBy)
        )

        return render_template(
            "album_detail.html",
            album=album,
            songs=songs,
            firstSongName=firstSongName,
            groupBy=groupBy,
            username=username,
            timeSeries=timeSeries,
            section="top_albums",
            success=request.args.get("success"),
            error=request.args.get("error"),
        )
    app.add_url_rule("/album/<album_id>", "albumDetailPage", albumDetailPage, methods=["GET"])
