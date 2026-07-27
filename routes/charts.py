"""Main stats pages: the public /overview, the dashboard index (/), the Top
Songs/Albums/Artists lists, the /charts analytics page, and the song/artist/
album detail pages.

Extracted verbatim from app.py. Genre-gate/coverage helpers come from services/;
the app-level PAGE_SIZE / CHART_* constants are aliased from the app module at
register() time. Every stats/pagination/embed helper is reached through the
dashboard instance.
"""
import logging

from flask import render_template, redirect, request, url_for, session, jsonify

import app as appmod
from routes._auth import makeRequiresUser
from Database.utils import convertToDatetime, msToString
from services.genre_gate import (
    emptyGenreCoverage, resolveGenreCoverage, genreGatePasses, resolveGenreDistribution,
    emptyBiographyCoverage, resolveBiographyCoverage,
)
from services.milestones import buildNextMilestones, MS_PER_HOUR

logger = logging.getLogger(__name__)

#< The detail pages' third AJAX mode. ?ajax=true (re-fetch the time series for
#  a new Trend bucket) and ?ajax=list (re-fetch the play log) are partial
#  refetches OF the body this one delivers, so the routes test them first and
#  this value is deliberately neither of theirs. See static/js/detail-page.js.
DETAIL_BODY_AJAX = "page"

#< How many pages' worth of rows one ?limit= may ask for. The detail history's
#  "Show more" grows its batch, so limit legitimately exceeds PAGE_SIZE - but it
#  was the one pagination parameter in the codebase with no ceiling at all, so
#  ?limit=500000 fetched and rendered half a million rows. Own data only, so this
#  is a footgun rather than a hole, and every other pager is already clamped.
MAX_DETAIL_HISTORY_PAGES = 10


def register(app, dashboard):
    PAGE_SIZE = appmod.PAGE_SIZE
    CHART_ARTIST_TREND_TOP_N = appmod.CHART_ARTIST_TREND_TOP_N
    CHART_TOP_GENRES_LIMIT = appmod.CHART_TOP_GENRES_LIMIT
    CHART_MOST_SKIPPED_LIMIT = appmod.CHART_MOST_SKIPPED_LIMIT
    #< only the three Top pages can rank by skips - see config.TOP_LIST_SORT_BY
    TOP_LIST_SORT_BY = appmod.TOP_LIST_SORT_BY
    # Read off the class, not the per-request db instance. A route test's
    # MagicMock db answers `db.SKIP_SORT_BY` with a Mock, which equals no
    # string - so every branch guarded on it silently took the other path and
    # the whole skip-count path went untested.
    SKIP_SORT_BY = appmod.Database.SKIP_SORT_BY
    requiresUser = makeRequiresUser(dashboard)

    # ---- Top Songs/Albums/Artists: the parts all three share -----------------
    # The three pages differ in which aggregate they read and what they call
    # the things they list. Everything else - parsing the filter card, the
    # two-phase AJAX split, choosing between the dedicated and the unique
    # count, building the pagination context - was written out three times,
    # which is how the skip-sort filters got fixed in one and missed in the
    # others.

    def _topListFilters(db, username):
        """The filter card's state, read identically by all three Top pages."""
        # The tag filter is gated on the admin's instance-wide tags kill
        # switch: with tags off we ignore a hand-crafted ?tag= (the dropdown is
        # already hidden template-side) and skip the getUserTags query.
        tagsOn = dashboard.repo.isTagsEnabled()
        # fullOnly defaults to on (a favorite has to have actually been heard)
        # - see templates/_page_card.html's checkbox. Explicit ?fullOnly=0 opts
        # out. Both spellings are kept: the raw one rebuilds pagination links,
        # the bool goes to the queries.
        fullOnly = request.args.get("fullOnly", "1")
        return {
            "searchQuery": request.args.get("q", ""),
            "sortBy": dashboard._getSortByParam(allowed=TOP_LIST_SORT_BY),
            "interval": request.args.get("interval", ""),
            "customStart": request.args.get("startDate", ""),
            "customEnd": request.args.get("endDate", ""),
            "tag": request.args.get("tag", "") if tagsOn else "",
            "fullOnly": fullOnly,
            "fullPlaysOnly": fullOnly != "0",
            #< only offered to users who've actually tagged something - see
            #  _page_card.html's {% if tags_enabled and user_tags %} guard
            "userTags": db.repo.getUserTags(username) if tagsOn else [],
        }

    def _topListShell(section, template, username, filters):
        """The plain GET half of the two-phase load: the filter card plus an
        empty #topListResults placeholder. top-list.js then fetches the stat
        header + list + pagination via ?ajax=true on first paint and on every
        filter/sort/tag/page change."""
        return render_template(
            template, section=section, username=username,
            sortBy=filters["sortBy"], interval=filters["interval"],
            customStart=filters["customStart"], customEnd=filters["customEnd"],
            tag=filters["tag"], user_tags=filters["userTags"],
            fullPlaysOnly=filters["fullPlaysOnly"])

    def _topListTotal(filters, countFn, uniqueCount, **idKwarg):
        """How many rows the pager is sizing itself for.

        The plain unique count already on the stat card is reused when nothing
        narrows the list. A search or tag obviously changes it - and so does
        the skip sort, whose page lists only entities that were actually
        skipped, so its total is smaller than the unique count above it."""
        if filters["sortBy"] == SKIP_SORT_BY or filters["searchQuery"] or filters["tag"]:
            return countFn(searchQuery=filters["searchQuery"], fullPlaysOnly=filters["fullPlaysOnly"],
                           sortBy=filters["sortBy"], **idKwarg)
        return uniqueCount

    def _topListResults(section, endpoint, username, filters, items, statCards,
                         page, totalPages, totalCount, startIndex, emptyMessage):
        pagination = dashboard._buildPaginationContext(
            endpoint, page, totalPages, totalCount,
            q=filters["searchQuery"], tag=filters["tag"], sortBy=filters["sortBy"],
            interval=filters["interval"], startDate=filters["customStart"],
            endDate=filters["customEnd"], fullOnly=filters["fullOnly"])
        return jsonify(resultsHtml=render_template(
            "_top_list_results.html", tracks=items, statCards=statCards, startIndex=startIndex,
            section=section, username=username, emptyMessage=emptyMessage, **pagination))

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
        # dumb iteration over a pre-shaped list.
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

    @requiresUser
    def dashboardIndex(username, db):

        settings = db.repo.getUserSettings(username)
        default_window = settings.get("default_dashboard_window", "day")

        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")

        #< resolved the same way /charts and /genres resolve it. Reading the raw
        #  param meant an unrecognised value (a stale or hand-edited URL, one
        #  truncated in a chat client) reached _getDateRange and _getIntervalLabel
        #  unchecked, where default="day" - not the user's configured window -
        #  decided what they got, and the heading then named it confidently.
        #< `or default_window` before validating: _getValidInterval accepts ""
        #  (it means "unset" on other paths), and while _getDateRange coerces it
        #  for the DATA, the template's <select> compares against this variable -
        #  so an empty ?interval= left every option unselected and the control
        #  displayed "All Time" over month-scoped numbers
        interval = dashboard._getValidInterval(request.args.get("interval", default_window) or default_window,
                                               default=default_window)
        if interval == "custom" and not (customStart and customEnd):
            interval = default_window

        #< no _getIntervalLabel here: unlike /charts and /genres, neither
        #  tracks.html nor _dashboard_summary.html renders one - the dashboard
        #  names its window with the <select> itself
        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd,
                                                     default=default_window, tz=db.tz)

        # The Time Period filter scopes these summary cards. The searchable play
        # history itself lives on its own /history page now (see historyPage).
        stats = db.getOverallStats(startDate, endDate)

        totalDurationText = msToString(stats["totalDurationMs"],
                                       hideSecondsAboveHours=appmod.LISTEN_TIME_HIDE_SECONDS_ABOVE_HOURS)

        currentTopSong = dashboard._embedTopSongTextElements(stats["currentTopSongs"][0], sortBy="plays", totalPlays=stats["totalSongsPlayed"], totalMs=stats["totalDurationMs"]) if stats["currentTopSongs"] else None
        currentTopArtist = dashboard._embedArtistTextElement(stats["currentTopArtists"][0], sortBy="totalTimeListened", totalPlays=stats["totalSongsPlayed"], totalMs=stats["totalDurationMs"]) if stats["currentTopArtists"] else None

        totalSongsChangeText, totalSongsChangeClass = dashboard._getChangeText(stats["totalSongsPlayed"], stats["previousSongsPlayed"])
        totalListenChangeText, totalListenChangeClass = dashboard._getChangeText(stats["totalDurationMs"], stats["previousDurationMs"])

        summaryArgs = dict(
            totalSongsPlayed=stats["totalSongsPlayed"],
            totalListenTime=totalDurationText,
            totalSongsChangeText=totalSongsChangeText,
            totalSongsChangeClass=totalSongsChangeClass,
            totalListenChangeText=totalListenChangeText,
            totalListenChangeClass=totalListenChangeClass,
            currentTopSong=currentTopSong,
            currentTopArtist=currentTopArtist,
            username=username,
        )

        # The Time Period filter only rescopes these four cards - the live
        # cards below (streak, on this day, discover, calendar) and next-
        # milestones are unfiltered, so a filter change's ajax fetch re-renders
        # just this one partial and skips every query below entirely (same
        # fade-and-swap pattern as compare.html/genres.html).
        if request.args.get("ajax") == "true":
            return jsonify({"summaryHtml": render_template("_dashboard_summary.html", **summaryArgs)})

        # Unfiltered dashboard cards (independent of the interval/date-range
        # filter above): live streak and "on this day" resurfacing are cheap
        # and rendered inline. The Discover card's genre-coverage gate and
        # recommendations are full-history queries (~700ms combined on a large
        # library - see dashboardDiscover) so they're fetched by the page's
        # own JS after first paint instead of blocking this render.
        currentStreak = db.getCurrentStreak()
        onThisDay = db.getOnThisDay(limit=appmod.ON_THIS_DAY_YEARS_LIMIT)
        lastfmGenreEnabled = dashboard.repo.isLastfmGenreBackfillEnabled()
        # Streak calendar: ~1 year of daily play counts, rendered inline below
        # the live cards. Comparable cost to getCurrentStreak above (a similar
        # bounded bucket scan), so it rides along in this render rather than
        # being deferred like the full-history Discover card.
        listeningCalendar = db.getListeningCalendar()

        # "Next milestones" progress bars: lifetime totals against the same
        # thresholds detection uses. getPlayTotals is a single COUNT+SUM scan;
        # removing the play-history list from this page more than pays for it.
        totalPlays, totalMs = db.getPlayTotals(None, None)
        streakDays = currentStreak.get("days", 0) if isinstance(currentStreak, dict) else 0
        nextMilestones = buildNextMilestones(totalPlays, (totalMs or 0) // MS_PER_HOUR, streakDays)

        return render_template(
            "tracks.html",
            currentStreak=currentStreak,
            onThisDay=onThisDay,
            listeningCalendar=listeningCalendar,
            nextMilestones=nextMilestones,
            lastfmGenreEnabled=lastfmGenreEnabled,
            friends_now_playing_enabled=dashboard.repo.isFriendsNowPlayingEnabled(),
            section="dashboard",
            interval=interval,
            customStart=customStart,
            customEnd=customEnd,
            #< the popstate fallback when a Back navigation lands on a bare
            #  URL with no explicit ?interval= (see loadDashboardSummary)
            defaultWindow=default_window,
            **summaryArgs,
        )
    app.add_url_rule("/", "dashboard", dashboardIndex, methods=["GET"])

    @requiresUser
    def historyPage(username, db):
        """The searchable, paginated play-history list - split out of the
        dashboard so that page can stay a glanceable overview. Carries the same
        search + Time Period filter the dashboard used to host, and the same
        list-scoping rule: only an explicit custom range (a chart click-through)
        scopes the list; named intervals don't."""

        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")

        # History defaults to All Time (the full list); the Time Period filter
        # then scopes it to any named interval or a custom range.
        interval = request.args.get("interval") or "all time"
        if interval == "custom" and not (customStart and customEnd):
            interval = "all time"

        sortOrder = dashboard._getHistorySortParam()
        oldestFirst = sortOrder == "oldest"

        # The tag filter mirrors the Top pages' (see _topListFilters): gated on
        # the admin's instance-wide tags kill switch, so a hand-crafted ?tag=
        # is ignored (the dropdown is already hidden template-side) and the
        # getUserTags query is skipped when tags are off.
        tagsOn = dashboard.repo.isTagsEnabled()
        tag = request.args.get("tag", "") if tagsOn else ""
        userTags = db.repo.getUserTags(username) if tagsOn else []

        # Lightweight shell, same two-phase load as /compare, /charts, /genres:
        # the initial GET renders just the filter controls + an empty results
        # placeholder, and history.html's own JS fetches the real list (and
        # pagination strip) via ?ajax=true right after first paint, and again
        # on every search/filter/page change - see loadHistoryResults.
        if request.args.get("ajax") != "true":
            return render_template(
                "history.html",
                username=username,
                section="history",
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                defaultWindow="all time",
                sort=sortOrder,
                tag=tag,
                user_tags=userTags,
            )

        page = dashboard._getPageParam()
        searchQuery = request.args.get("q", "")

        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd, default="all time", tz=db.tz)

        # The Time Period filter scopes the list for every interval, not just
        # custom ranges: "Last Week" shows last week's plays, "All Time" (the
        # default) resolves to (None, None) i.e. the full history.
        listStartDate = startDate
        listEndDate = endDate

        # Same expand-outward semantics as the Top Songs tag filter
        # (getTaggedTrackIds also matches a track via its tagged album/artist).
        trackIds = db.repo.getTaggedTrackIds(username, [tag]) if tag else None

        if searchQuery:
            # Matching and pagination both happen in SQL (Repository.searchPlays)
            # instead of fetching every play ever recorded and filtering in Python.
            totalCount = db.searchEntriesCount(searchQuery, startDate=listStartDate, endDate=listEndDate,
                                               trackIds=trackIds)
            page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
            tracks = db.searchEntries(searchQuery, count=PAGE_SIZE, startIndex=startIndex,
                                      startDate=listStartDate, endDate=listEndDate,
                                      oldestFirst=oldestFirst, trackIds=trackIds)
        else:
            # Only materialize the page being shown - joining full track
            # metadata onto every entry ever recorded on every request gets
            # slow once the history grows large.
            totalCount = db.getEntriesCount(startDate=listStartDate, endDate=listEndDate, trackIds=trackIds)
            page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
            fetchEntries = db.getEntriesFromOld if oldestFirst else db.getEntriesFromNew
            tracks = fetchEntries(count=PAGE_SIZE, startIndex=startIndex,
                                  startDate=listStartDate, endDate=listEndDate, trackIds=trackIds)
        tracks = dashboard._embedSongsTextElements(tracks)
        tracks = dashboard._attachGenres(db, tracks, "track")

        pagination = dashboard._buildPaginationContext(
            "history",
            page,
            totalPages,
            totalCount,
            q=searchQuery,
            interval=interval,
            startDate=customStart,
            endDate=customEnd,
            sort=sortOrder if oldestFirst else None,
            tag=tag,
        )

        creds = db.getUserSpotifyCredentials() or {}
        is_authenticated = bool(creds.get("refresh_token"))

        return jsonify({
            "resultsHtml": render_template(
                "_history_results.html",
                tracks=tracks,
                startIndex=startIndex,
                interval=interval,
                is_authenticated=is_authenticated,
                username=username,
                **pagination,
            ),
        })
    app.add_url_rule("/history", "history", historyPage, methods=["GET"])

    @requiresUser(api=True)
    def dashboardDiscover(username, db):
        """JSON for the dashboard's Discover card, fetched by tracks.html's own
        JS after first paint (see dashboardIndex) rather than computed inline -
        the genre-coverage gate check and recommendation query are full-history
        scans that noticeably slowed the dashboard once added."""

        if not dashboard.repo.isLastfmGenreBackfillEnabled():
            return jsonify({"unlocked": False, "recommendations": []})

        unlocked = genreGatePasses(resolveGenreCoverage(db, None, None))
        recommendations = []
        if unlocked:
            recommendations = db.getRecommendedArtists(
                # Admin-tunable, read live per request; falls back to the code default.
                limit=dashboard.repo.getDiscoverArtistLimit(appmod.RECOMMENDATION_ARTIST_LIMIT),
                genrePool=appmod.RECOMMENDATION_GENRE_POOL,
                excludeTopN=appmod.RECOMMENDATION_EXCLUDE_TOP_N,
            )
        return jsonify({"unlocked": unlocked, "recommendations": recommendations})
    app.add_url_rule("/api/dashboard-discover", "dashboardDiscover", dashboardDiscover, methods=["GET"])

    @requiresUser(api=True)
    def dashboardTrends(username, db):
        """JSON/HTML for the dashboard's Obsession, Rediscovery, and Forgotten Favorite trend cards."""

        trends = db.getDashboardTrends()
        html = render_template("_dashboard_trends.html", username=username, trends=trends)
        return jsonify({"trendsHtml": html, "trends": trends})
    app.add_url_rule("/api/dashboard-trends", "dashboardTrends", dashboardTrends, methods=["GET"])

    @requiresUser
    def topSongsPage(username, db):
        filters = _topListFilters(db, username)
        if request.args.get("ajax") != "true":
            return _topListShell("top_songs", "top_songs.html", username, filters)

        tag = filters["tag"]
        trackIds = db.repo.getTaggedTrackIds(username, [tag]) if tag else None
        startDate, endDate = dashboard._getDateRange(
            filters["interval"], filters["customStart"], filters["customEnd"],
            default="all time", tz=db.tz)
        fullPlaysOnly = filters["fullPlaysOnly"]
        # totalPlays/totalMs are a whole-range aggregate regardless of search -
        # a cheap dedicated query instead of summing every song's metadata.
        # The TAG filter does scope them (unlike search): a tag narrows what
        # the page is about, so cards reading whole-library numbers above a
        # tag-filtered list contradicted the pager right below them.
        totalPlays, totalMs = db.getPlayTotals(startDate, endDate, fullPlaysOnly=fullPlaysOnly,
                                               trackIds=trackIds)
        uniqueSongs = db.getSongsCount(startDate, endDate, fullPlaysOnly=fullPlaysOnly,
                                       trackIds=trackIds)

        totalCount = _topListTotal(
            filters, lambda **kw: db.getSongsCount(startDate, endDate, **kw), uniqueSongs,
            trackIds=trackIds)
        page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
        # Only materialize the page being shown - SQL-level LIMIT/OFFSET and
        # WHERE-clause matching (see Repository.getSongsPage) instead of
        # sorting+hydrating+filtering every song ever played in Python.
        tracks = db.getTopSongs(startDate=startDate, endDate=endDate, by=filters["sortBy"],
                                 limit=PAGE_SIZE, offset=startIndex, searchQuery=filters["searchQuery"],
                                 trackIds=trackIds, fullPlaysOnly=fullPlaysOnly)

        tracks = dashboard._embedSongsTextElements(tracks)
        tracks = dashboard._embedTopSongsTextElements(
            tracks, sortBy=filters["sortBy"], totalPlays=totalPlays, totalMs=totalMs)
        tracks = dashboard._attachGenres(db, tracks, "track")

        return _topListResults(
            "top_songs", "topSongsPage", username, filters, tracks,
            statCards=[
                {"label": "Total Plays", "value": totalPlays},
                {"label": "Time", "value": msToString(totalMs)},
                {"label": "Unique Songs", "value": uniqueSongs},
            ],
            page=page, totalPages=totalPages, totalCount=totalCount, startIndex=startIndex,
            emptyMessage="No top songs available. Import some listening history first.")
    app.add_url_rule("/top-songs", "topSongsPage", topSongsPage, methods=["GET"])

    @requiresUser
    def topAlbumsPage(username, db):
        filters = _topListFilters(db, username)
        if request.args.get("ajax") != "true":
            return _topListShell("top_albums", "top_albums.html", username, filters)

        tag = filters["tag"]
        albumIds = db.repo.getTaggedAlbumIds(username, [tag]) if tag else None
        startDate, endDate = dashboard._getDateRange(
            filters["interval"], filters["customStart"], filters["customEnd"],
            default="all time", tz=db.tz)
        fullPlaysOnly = filters["fullPlaysOnly"]
        #< albumIds: the tag filter scopes the header cards too - see topSongsPage
        totalPlays, totalMs = db.getPlayTotals(startDate, endDate, fullPlaysOnly=fullPlaysOnly,
                                               albumIds=albumIds)
        uniqueAlbums = db.getAlbumsCount(startDate, endDate, fullPlaysOnly=fullPlaysOnly,
                                         albumIds=albumIds)

        totalCount = _topListTotal(
            filters, lambda **kw: db.getAlbumsCount(startDate, endDate, **kw), uniqueAlbums,
            albumIds=albumIds)
        page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
        albums = db.getTopAlbums(startDate=startDate, endDate=endDate, by=filters["sortBy"],
                                  limit=PAGE_SIZE, offset=startIndex, searchQuery=filters["searchQuery"],
                                  albumIds=albumIds, fullPlaysOnly=fullPlaysOnly)

        albums = dashboard._embedAlbumsTextElements(
            albums, sortBy=filters["sortBy"], totalPlays=totalPlays, totalMs=totalMs)
        albums = dashboard._attachGenres(db, albums, "album")

        return _topListResults(
            "top_albums", "topAlbumsPage", username, filters, albums,
            statCards=[
                {"label": "Total Plays (top list)", "value": totalPlays},
                {"label": "Time", "value": msToString(totalMs)},
                {"label": "Unique Albums", "value": uniqueAlbums},
            ],
            page=page, totalPages=totalPages, totalCount=totalCount, startIndex=startIndex,
            emptyMessage="No top albums available. Import some listening history first.")
    app.add_url_rule("/top-albums", "topAlbumsPage", topAlbumsPage, methods=["GET"])

    @requiresUser
    def topArtistsPage(username, db):
        filters = _topListFilters(db, username)
        if request.args.get("ajax") != "true":
            return _topListShell("top_artists", "top_artists.html", username, filters)

        tag = filters["tag"]
        artistIds = db.repo.getTaggedArtistIds(username, [tag]) if tag else None
        startDate, endDate = dashboard._getDateRange(
            filters["interval"], filters["customStart"], filters["customEnd"],
            default="all time", tz=db.tz)
        fullPlaysOnly = filters["fullPlaysOnly"]
        # totalPlays/totalUnique/totalMs are the whole (date-range-scoped) top
        # list's totals regardless of search - mirrors getPlayTotals()'s role
        # for the songs/albums pages, computed via a dedicated SQL aggregate
        # instead of fetching every artist and summing in Python. The tag
        # filter scopes them, like the songs/albums headers - see topSongsPage.
        totalPlays, totalUnique, totalMs = db.getArtistTotals(startDate, endDate,
                                                              fullPlaysOnly=fullPlaysOnly,
                                                              artistIds=artistIds)
        uniqueArtists = db.getArtistsCount(startDate, endDate, fullPlaysOnly=fullPlaysOnly,
                                           artistIds=artistIds)

        totalCount = _topListTotal(
            filters, lambda **kw: db.getArtistsCount(startDate, endDate, **kw), uniqueArtists,
            artistIds=artistIds)
        page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
        artists = db.getTopArtists(startDate=startDate, endDate=endDate, by=filters["sortBy"],
                                    limit=PAGE_SIZE, offset=startIndex, searchQuery=filters["searchQuery"],
                                    artistIds=artistIds, fullPlaysOnly=fullPlaysOnly)

        artists = dashboard._embedArtistsTextElements(
            artists, sortBy=filters["sortBy"], totalPlays=totalPlays, totalMs=totalMs)
        artists = dashboard._attachGenres(db, artists, "artist")

        return _topListResults(
            "top_artists", "topArtistsPage", username, filters, artists,
            statCards=[
                {"label": "Total Plays (top list)", "value": totalPlays},
                {"label": "Unique Songs (top list)", "value": totalUnique},
                {"label": "Unique Artists", "value": uniqueArtists},
            ],
            page=page, totalPages=totalPages, totalCount=totalCount, startIndex=startIndex,
            emptyMessage="No top artists available. Import some listening history first.")
    app.add_url_rule("/top-artists", "topArtistsPage", topArtistsPage, methods=["GET"])

    @requiresUser
    def chartsPage(username, db):

        settings = db.repo.getUserSettings(username)
        defaultWindow = settings.get("default_dashboard_window", "day")

        interval = dashboard._getValidInterval(request.args.get("interval", defaultWindow), default=defaultWindow)
        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")
        if interval == "custom" and not (customStart and customEnd):
            interval = defaultWindow
        #< the raw param, not the resolved bucketing - the template's select
        #  must keep showing Auto rather than pinning the derived value
        groupByParam = request.args.get("groupBy", "")

        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd, default=defaultWindow, tz=db.tz)
        spanStart, spanEnd = startDate, endDate
        if spanStart is None or spanEnd is None:
            spanStart, spanEnd = dashboard._playRangeSpanDates(username, db.tz)   #< "All Time" has no explicit range
        groupBy = dashboard._resolveGroupBy(groupByParam, spanStart, spanEnd)
        #< same default as the _getDateRange call above, or the heading can
        #  name a different window than the data covers
        intervalLabel = dashboard._getIntervalLabel(interval, customStart, customEnd,
                                                    default=defaultWindow)

        isSingleDayView = interval in ("day", "today")
        lastDayDate = startDate.strftime("%Y-%m-%d") if isSingleDayView and startDate else None

        # The admin's instance-wide kill switch: checked before spending any
        # genre queries, and the whole Top Genres section hides on the template
        # side when this is False. Cheap instance setting, so it's resolved for
        # both the shell and the ajax payload.
        lastfmEnabled = dashboard.repo.isLastfmGenreBackfillEnabled()

        # Lightweight shell: the page's structure (filter, headings, empty
        # canvases) renders immediately; static/js/charts-page.js then fetches
        # the ajax payload below after first paint (and on every filter change),
        # so none of the heavy per-range chart queries block the initial load.
        if request.args.get("ajax") != "true":
            return render_template(
                "charts.html",
                username=username,
                section="charts",
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                groupBy=groupByParam,
                intervalLabel=intervalLabel,
                lastDayDate=lastDayDate,
                isSingleDayView=isSingleDayView,
                defaultWindow=defaultWindow,
                lastfmEnabled=lastfmEnabled,
            )

        timeSeriesGroupBy = "hour" if isSingleDayView else groupBy

        # The timeline and the heatmap are two different local-time views of
        # the SAME pre-aggregated rows, so the aggregate runs once here rather
        # than once inside each.
        bucketRows = db.getPlayBuckets(startDate=startDate, endDate=endDate)
        timeSeries = dashboard._embedTimeSeriesTextElements(
            db.getListeningTimeSeries(startDate=startDate, endDate=endDate, groupBy=timeSeriesGroupBy,
                                       bucketRows=bucketRows),
            groupBy=timeSeriesGroupBy,
        )
        heatmap = dashboard._embedHeatmapTextElements(
            db.getHourOfDayHeatmap(startDate=startDate, endDate=endDate, bucketRows=bucketRows))
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
        # "How often do I skip" is the donut above; these answer "what do I
        # skip". Ranked by shrunk rate - see Repository.getMostSkippedTracks.
        mostSkippedSongs = db.getMostSkippedSongs(
            startDate=startDate, endDate=endDate, limit=CHART_MOST_SKIPPED_LIMIT)
        mostSkippedArtists = db.getMostSkippedArtists(
            startDate=startDate, endDate=endDate, limit=CHART_MOST_SKIPPED_LIMIT)

        genreCoverage = emptyGenreCoverage()
        genreUnlocked = False
        genreDistribution = None
        if lastfmEnabled:
            genreCoverage = resolveGenreCoverage(db, startDate, endDate)
            genreUnlocked = genreGatePasses(genreCoverage)
            if genreUnlocked:
                distribution = resolveGenreDistribution(db, startDate, endDate,
                                                        CHART_TOP_GENRES_LIMIT)
                # Most-played first, like every other genre surface
                # (Wrapped/Compare): the section is called Top Genres, so the
                # top one belongs in the first row rather than at the bottom of
                # a chart that climbs toward it.
                genreDistribution = list(distribution.items())

        # The Top Genres section's locked/unlocked structure is range-scoped
        # (coverage over the selected window), so it's shipped as pre-rendered
        # HTML the client swaps in - not just data - and the whole section
        # stays hidden when the admin killed the feature.
        genreSectionHtml = render_template(
            "_charts_genre_section.html", genreUnlocked=genreUnlocked, genreCoverage=genreCoverage,
        ) if lastfmEnabled else ""

        return jsonify(
            interval=interval,
            groupBy=groupBy,
            intervalLabel=intervalLabel,
            lastDayDate=lastDayDate,
            timeSeries=timeSeries,
            heatmap=heatmap,
            artistTrend=artistTrend,
            explicitRatio=explicitRatio,
            decadeDistribution=decadeDistribution,
            completionStats=completionStats,
            mostSkippedSongs=mostSkippedSongs,
            mostSkippedArtists=mostSkippedArtists,
            genreDistribution=genreDistribution,
            genreUnlocked=genreUnlocked,
            genreSectionHtml=genreSectionHtml,
        )
    app.add_url_rule("/charts", "chartsPage", chartsPage, methods=["GET"])

    def _detailHistoryContext(db, endpoint, linkArgs, groupByParam="",
                               trackId=None, artistId=None, albumId=None,
                               trackDurationMs=None):
        """The detail pages' play-history list context: one sorted+paginated
        page of the item's individual plays, the Date-sort toggle URL, and
        _pagination.html's context. `linkArgs` are the endpoint kwargs every
        list URL must carry (the item id, plus view=history for the artist/
        album tabs); groupBy rides along in every URL so list navigation
        never resets the Trend-buckets chart selection."""
        sortOrder = dashboard._getHistorySortParam()
        oldestFirst = sortOrder == "oldest"
        isSongDetail = trackId is not None and artistId is None and albumId is None

        if isSongDetail:
            skipsParam = request.args.get("skips", "true").lower()
            showSkips = skipsParam != "false"

            try:
                pageParam = int(request.args.get("page", 1))
                defaultOffset = (pageParam - 1) * PAGE_SIZE if pageParam > 1 else 0
            except (ValueError, TypeError):
                defaultOffset = 0

            try:
                offset = max(0, int(request.args.get("offset", defaultOffset)))
            except (ValueError, TypeError):
                offset = defaultOffset

            try:
                limit = min(PAGE_SIZE * MAX_DETAIL_HISTORY_PAGES,
                            max(1, int(request.args.get("limit", PAGE_SIZE))))
            except (ValueError, TypeError):
                limit = PAGE_SIZE

            totalCount = db.getEntriesCount(trackId=trackId, includeSkips=showSkips)
            fetchEntries = db.getEntriesFromOld if oldestFirst else db.getEntriesFromNew
            plays = fetchEntries(count=limit, startIndex=offset,
                                 trackId=trackId, includeSkips=showSkips)
            plays = dashboard._embedSongsTextElements(plays)
            plays = dashboard._enrichSongTimelineEntries(plays, trackDurationMs=trackDurationMs)

            hasMore = (offset + len(plays)) < totalCount
            nextOffset = offset + len(plays)
            remainingCount = max(0, totalCount - nextOffset)
            nextBatchSize = min(PAGE_SIZE, remainingCount)

            sharedArgs = dict(linkArgs, groupBy=groupByParam,
                              sort=sortOrder if oldestFirst else None,
                              skips="false" if not showSkips else None)
            sortToggleArgs = dict(sharedArgs, sort=None if oldestFirst else "oldest", offset=0)
            skipsToggleArgs = dict(sharedArgs, skips="false" if showSkips else "true", offset=0)

            return {
                "plays": plays,
                "totalCount": totalCount,
                "offset": offset,
                "hasMore": hasMore,
                "nextOffset": nextOffset,
                "nextBatchSize": nextBatchSize,
                "remainingCount": remainingCount,
                "sortOldest": oldestFirst,
                "showSkips": showSkips,
                "isSongDetail": True,
                "sortToggleUrl": dashboard._buildPageUrl(endpoint, 1, **sortToggleArgs),
                "skipsToggleUrl": dashboard._buildPageUrl(endpoint, 1, **skipsToggleArgs),
            }

        totalCount = db.getEntriesCount(trackId=trackId, artistId=artistId, albumId=albumId)
        page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
        fetchEntries = db.getEntriesFromOld if oldestFirst else db.getEntriesFromNew
        plays = fetchEntries(count=PAGE_SIZE, startIndex=startIndex,
                             trackId=trackId, artistId=artistId, albumId=albumId)
        plays = dashboard._embedSongsTextElements(plays)
        sharedArgs = dict(linkArgs, groupBy=groupByParam, sort=sortOrder if oldestFirst else None)
        return {
            "plays": plays,
            "startIndex": startIndex,
            "sortOldest": oldestFirst,
            "isSongDetail": False,
            "sortToggleUrl": dashboard._buildPageUrl(endpoint, 1, **dict(sharedArgs, sort=None if oldestFirst else "oldest")),
            **dashboard._buildPaginationContext(endpoint, page, totalPages, totalCount, **sharedArgs),
        }

    @requiresUser
    def songDetailPage(username, db, track_id):

        song = db.getSong(track_id)
        if song is None:
            return redirect(url_for("topSongsPage"))

        groupByParam = request.args.get("groupBy", "")   #< raw: the select keeps showing Auto
        # Three AJAX modes share this route. They're tested most-specific first
        # so the precedence can't drift: the two partial refetches claim their
        # own value, the deferred whole-body load claims DETAIL_BODY_AJAX, and
        # anything else (no ?ajax at all) is the shell.
        ajax = request.args.get("ajax", "")
        # The bucket select re-fetches just the play-history series (see
        # static/js/detail-chart.js) - everything else on the page is
        # bucket-independent, so the full render below is skipped.
        if ajax == "true":
            groupBy = dashboard._resolveGroupBy(
                groupByParam, *dashboard._playRangeSpanDates(username, db.tz, trackId=track_id))
            timeSeries = dashboard._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(trackId=track_id, groupBy=groupBy)
            )
            return jsonify(timeSeries=timeSeries, groupBy=groupBy)

        # Lightweight shell, the same two-phase load /charts, /genres, /history
        # and the three Top pages use: this GET renders the hero, the toolbar
        # and the tag panel - all off the one getSong above - and
        # static/js/detail-page.js fetches everything below them right after
        # first paint. Every query past this point (the play log, the bucketed
        # chart aggregates, the skip summary) is work the first paint no longer
        # waits on.
        if ajax not in ("list", DETAIL_BODY_AJAX):
            return render_template(
                "song_detail.html",
                song=song,
                username=username,
                groupBy=groupByParam,
                entity_tags=db.repo.getTagsForEntity(username, "track", track_id),
                success=request.args.get("success"),
                error=request.args.get("error"),
            )

        listCtx = _detailHistoryContext(db, "songDetailPage", {"track_id": track_id},
                                        groupByParam=groupByParam, trackId=track_id,
                                        trackDurationMs=song.get("duration"))
        # The sort toggle / pagination links re-fetch just the play log (see
        # static/js/detail-history.js) - chart/heatmap work is skipped.
        if ajax == "list":
            return jsonify(
                resultsHtml=render_template("_play_log.html", username=username, **listCtx),
                hasMore=listCtx.get("hasMore", False),
                nextOffset=listCtx.get("nextOffset", 0),
                nextBatchSize=listCtx.get("nextBatchSize", 0),
                remainingCount=listCtx.get("remainingCount", 0),
            )

        groupBy = dashboard._resolveGroupBy(
            groupByParam, *dashboard._playRangeSpanDates(username, db.tz, trackId=track_id))
        # One aggregate, two local-time views - same as chartsPage below.
        bucketRows = db.getPlayBuckets(trackId=track_id)
        timeSeries = dashboard._embedTimeSeriesTextElements(
            db.getListeningTimeSeries(trackId=track_id, groupBy=groupBy, bucketRows=bucketRows)
        )

        song = dashboard._embedSongTextElements(song)
        song = dashboard._embedTopSongTextElements(song)
        song = dashboard._attachGenres(db, [song], "track")[0]

        heatmap = dashboard._embedHeatmapTextElements(
            db.getHourOfDayHeatmap(trackId=track_id, bucketRows=bucketRows))

        skipStats = db.getSkipStats(trackId=track_id)

        return jsonify(
            bodyHtml=render_template(
                "_song_detail_body.html",
                song=song,
                username=username,
                skipStats=skipStats,
                **listCtx,
            ),
            timeSeries=timeSeries,
            heatmap=heatmap,
        )
    app.add_url_rule("/song/<track_id>", "songDetailPage", songDetailPage, methods=["GET"])

    @requiresUser
    def artistDetailPage(username, db, artist_id):

        artist = db.getArtist(artist_id)
        if artist is None:
            return redirect(url_for("topArtistsPage"))

        groupByParam = request.args.get("groupBy", "")   #< raw: the select keeps showing Auto
        ajax = request.args.get("ajax", "")
        # Bucket-only AJAX refetch - see songDetailPage's identical branch.
        if ajax == "true":
            groupBy = dashboard._resolveGroupBy(
                groupByParam, *dashboard._playRangeSpanDates(username, db.tz, artistId=artist_id))
            timeSeries = dashboard._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(artistId=artist_id, groupBy=groupBy)
            )
            return jsonify(timeSeries=timeSeries, groupBy=groupBy)

        # Deferred-body shell - see songDetailPage's identical branch. The
        # artist page has the most to gain: the whole songs-by-this-artist
        # aggregate and the Last.fm biography fetch below now happen after the
        # page is already on screen.
        if ajax not in ("list", DETAIL_BODY_AJAX):
            return render_template(
                "artist_detail.html",
                artist=artist,
                username=username,
                groupBy=groupByParam,
                entity_tags=db.repo.getTagsForEntity(username, "artist", artist_id),
                success=request.args.get("success"),
                error=request.args.get("error"),
            )

        listCtx = _detailHistoryContext(db, "artistDetailPage", {"artist_id": artist_id, "view": "history"},
                                        groupByParam=groupByParam, artistId=artist_id)
        listCtx["plays"] = dashboard._attachGenres(db, listCtx["plays"], "track")
        # List-only AJAX refetch - see songDetailPage's identical branch.
        if ajax == "list":
            return jsonify(resultsHtml=render_template(
                "_detail_history_results.html", username=username,
                itemName=artist.get("name", ""), **listCtx))

        groupBy = dashboard._resolveGroupBy(
            groupByParam, *dashboard._playRangeSpanDates(username, db.tz, artistId=artist_id))
        timeSeries = dashboard._embedTimeSeriesTextElements(
            db.getListeningTimeSeries(artistId=artist_id, groupBy=groupBy)
        )

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

        skipStats = db.getSkipStats(artistId=artist_id)

        return jsonify(
            bodyHtml=render_template(
                "_artist_detail_body.html",
                artist=artist,
                songs=songs,
                firstSongName=firstSongName,
                username=username,
                skipStats=skipStats,
                view=dashboard._getDetailViewParam(),
                itemName=artist.get("name", ""),
                **listCtx,
            ),
            timeSeries=timeSeries,
        )
    app.add_url_rule("/artist/<artist_id>", "artistDetailPage", artistDetailPage, methods=["GET"])

    @requiresUser
    def albumDetailPage(username, db, album_id):

        album = db.getAlbum(album_id)
        if album is None:
            return redirect(url_for("topAlbumsPage"))

        groupByParam = request.args.get("groupBy", "")   #< raw: the select keeps showing Auto
        ajax = request.args.get("ajax", "")
        # Bucket-only AJAX refetch - see songDetailPage's identical branch.
        if ajax == "true":
            groupBy = dashboard._resolveGroupBy(
                groupByParam, *dashboard._playRangeSpanDates(username, db.tz, albumId=album_id))
            timeSeries = dashboard._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(albumId=album_id, groupBy=groupBy)
            )
            return jsonify(timeSeries=timeSeries, groupBy=groupBy)

        # Deferred-body shell - see songDetailPage's identical branch.
        if ajax not in ("list", DETAIL_BODY_AJAX):
            return render_template(
                "album_detail.html",
                album=album,
                username=username,
                groupBy=groupByParam,
                entity_tags=db.repo.getTagsForEntity(username, "album", album_id),
                success=request.args.get("success"),
                error=request.args.get("error"),
            )

        listCtx = _detailHistoryContext(db, "albumDetailPage", {"album_id": album_id, "view": "history"},
                                        groupByParam=groupByParam, albumId=album_id)
        listCtx["plays"] = dashboard._attachGenres(db, listCtx["plays"], "track")
        # List-only AJAX refetch - see songDetailPage's identical branch.
        if ajax == "list":
            return jsonify(resultsHtml=render_template(
                "_detail_history_results.html", username=username,
                itemName=album.get("name", ""), **listCtx))

        groupBy = dashboard._resolveGroupBy(
            groupByParam, *dashboard._playRangeSpanDates(username, db.tz, albumId=album_id))
        timeSeries = dashboard._embedTimeSeriesTextElements(
            db.getListeningTimeSeries(albumId=album_id, groupBy=groupBy)
        )

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

        skipStats = db.getSkipStats(albumId=album_id)

        return jsonify(
            bodyHtml=render_template(
                "_album_detail_body.html",
                album=album,
                songs=songs,
                firstSongName=firstSongName,
                username=username,
                skipStats=skipStats,
                view=dashboard._getDetailViewParam(),
                itemName=album.get("name", ""),
                **listCtx,
            ),
            timeSeries=timeSeries,
        )
    app.add_url_rule("/album/<album_id>", "albumDetailPage", albumDetailPage, methods=["GET"])
