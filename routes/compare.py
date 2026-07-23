"""The /compare page - two mutually-shared users' stats side by side.

Extracted verbatim from app.py. The taste-match scoring and genre-gate helpers
come from services/; the app-level display constants (COMPARE_*,
WRAPPED_LIMIT_OPTIONS) are aliased from the app module at register() time so the
handler body stays unchanged. Everything else is reached through the dashboard
instance.
"""
from datetime import timedelta

from flask import render_template, redirect, request, url_for, abort, jsonify

import app as appmod
from Database.utils import convertToDatetime
from services.taste_match import _markLinkExternally, _tasteMatchPercent
from services.genre_gate import (
    emptyGenreCoverage, resolveGenreCoverage, genreGatePasses, resolveGenreDistribution,
)


def register(app, dashboard):
    # App-level display constants this route depends on (defined in app.py and
    # also used by dashboard._gatherCompareStats / the Wrapped page).
    WRAPPED_LIMIT_OPTIONS = appmod.WRAPPED_LIMIT_OPTIONS
    COMPARE_TOP_LIST_SIZE = appmod.COMPARE_TOP_LIST_SIZE
    COMPARE_SHARED_POOL_SIZE = appmod.COMPARE_SHARED_POOL_SIZE
    COMPARE_GENRE_POOL_SIZE = appmod.COMPARE_GENRE_POOL_SIZE
    COMPARE_TOP_GENRES_LIMIT = appmod.COMPARE_TOP_GENRES_LIMIT

    def comparePage():
        if not dashboard.repo.isDataSharingEnabled():
            abort(404)
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        # Mirrors /overview's cookies_json guard: get_user_db starts a
        # live listener, which needs stored cookies - a share counterpart
        # without them (only creatable by seeding user_shares directly;
        # the UI can't accept a share while logged out) must be skipped,
        # not crash the page.
        acceptedUsernames = [
            u for u in dashboard.repo.getAcceptedShareUsernames(username)
            if dashboard.repo.getUserCookies(u) is not None
        ]
        if not acceptedUsernames:
            abort(404)

        withUsername = request.args.get("with", acceptedUsernames[0])
        if withUsername not in acceptedUsernames:
            # ?with= is untrusted input - never let it select a user's data
            # the session user hasn't mutually accepted a share with. Fall
            # back to a real choice rather than 404ing, which would leak
            # whether an unrelated username exists at all.
            withUsername = acceptedUsernames[0]

        otherEmail = dashboard.repo.getEmailForUsername(withUsername)
        otherDb = dashboard.get_user_db(withUsername, otherEmail)

        # Same default-window setting the dashboard route reads - "all
        # time" is that setting's own stored spelling, but Compare's own
        # dropdown represents All Time as "" (see compare.html), so it's
        # normalized before feeding either the resolver or the template.
        settings = dashboard.repo.getUserSettings(username)
        defaultWindow = settings.get("default_dashboard_window", "day")
        if defaultWindow == "all time":
            defaultWindow = ""
        interval = request.args.get("interval", defaultWindow)
        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")
        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd, default="all time", tz=db.tz)
        groupByParam = request.args.get("groupBy", "")
        limit = request.args.get("limit", type=int)
        if limit not in WRAPPED_LIMIT_OPTIONS:
            limit = COMPARE_TOP_LIST_SIZE
        # Default stays "plays" (not DEFAULT_SORT_BY) so nobody's view
        # changes unless they touch the control - matches every other
        # value on this page defaulting to the pre-existing behavior.
        # Same whitelist as the standalone Top pages (VALID_SORT_BY),
        # default "plays". sortBy only reorders the individual my/their
        # lists (see _gatherCompareStats) - the Top Common lists and
        # taste-match never read it (see _buildSharedItems).
        sortBy = dashboard._getSortByParam(default="plays")

        my = dashboard._gatherCompareStats(db, startDate, endDate, limit=limit, sortBy=sortBy)
        their = dashboard._gatherCompareStats(otherDb, startDate, endDate, limit=limit, sortBy=sortBy)

        # A counterpart item links to Spotify only when the viewer has NO
        # plays of that exact song/artist/album - the viewer's own detail
        # page has nothing to show them then. Otherwise it links there
        # like any other item, since the viewer genuinely does have data
        # for it (a real play-history lookup, not "is in the viewer's own
        # top list" - a track can be true for the former and not the
        # latter). Batched to one query per category rather than one per
        # displayed item.
        _markLinkExternally(their["topSongs"], db.getPlayedTrackIds([s["id"] for s in their["topSongs"]]))
        _markLinkExternally(their["topArtists"], db.getPlayedArtistIds([a["id"] for a in their["topArtists"]]))
        _markLinkExternally(their["topAlbums"], db.getPlayedAlbumIds([a["id"] for a in their["topAlbums"]]))

        listArgs = dict(username=username, compareWith=withUsername,
                        emptyMessage="No plays in this period.")

        def sortableListsJson():
            """The six individual my/their list chunks - the only part of
            the ajax payload a sortBy change swaps (see compare.html's
            SORT_BY_LIST_SWAPS)."""
            return {
                "myTopSongsHtml": render_template(
                    "_wrapped_list.html", items=my["topSongs"], section="top_songs", **listArgs),
                #< each item's own linkExternally decides internal vs. Spotify (see _markLinkExternally)
                "theirTopSongsHtml": render_template(
                    "_wrapped_list.html", items=their["topSongs"], section="top_songs", **listArgs),
                "myTopArtistsHtml": render_template(
                    "_wrapped_list.html", items=my["topArtists"], section="top_artists", **listArgs),
                "theirTopArtistsHtml": render_template(
                    "_wrapped_list.html", items=their["topArtists"], section="top_artists", **listArgs),
                "myTopAlbumsHtml": render_template(
                    "_wrapped_list.html", items=my["topAlbums"], section="top_albums", **listArgs),
                "theirTopAlbumsHtml": render_template(
                    "_wrapped_list.html", items=their["topAlbums"], section="top_albums", **listArgs),
            }

        # A sortBy change swaps only those six lists, so its fetch
        # (scope=sortable, see loadCompareData) stops here - the shared
        # lists, similarities, genres, taste match and trend below are
        # the expensive half on long ranges and would render identically
        # anyway. Any other scope value degrades to the full payload.
        if request.args.get("ajax") == "true" and request.args.get("scope") == "sortable":
            return jsonify(sortableListsJson())

        # Sliced like every other list on the page. No percent text here -
        # it would mix two different users' totals. Searches the deeper
        # sharedXPool (COMPARE_SHARED_POOL_SIZE), not the shallower
        # topXPool taste-match uses - see _gatherCompareStats. Ranked by
        # _buildSharedItems's own shared-rank-weighted score, independent
        # of sortBy - only the individual my/their lists above read it.
        sharedArtists = dashboard._buildSharedItems(
            my["sharedArtistsPool"], their["sharedArtistsPool"],
            dashboard._embedArtistsTextElements, limit)
        sharedSongs = dashboard._buildSharedItems(
            my["sharedSongsPool"], their["sharedSongsPool"],
            lambda items: dashboard._embedTopSongsTextElements(dashboard._embedSongsTextElements(items)),
            limit)
        sharedAlbums = dashboard._buildSharedItems(
            my["sharedAlbumsPool"], their["sharedAlbumsPool"],
            dashboard._embedAlbumsTextElements, limit)
        # Genre tables are entity-keyed, not user-scoped, so either
        # side's db returns the same result here - db (the viewer's) is
        # just what's already in scope.
        sharedArtists = dashboard._attachGenres(db, sharedArtists, "artist")
        sharedSongs = dashboard._attachGenres(db, sharedSongs, "track")
        sharedAlbums = dashboard._attachGenres(db, sharedAlbums, "album")

        # Similarities run over the deeper sharedXPool, not the displayed
        # top ten (nor the shallower topXPool taste-match uses) - a #200-
        # ranked common favorite is still a common favorite. The
        # "common top X" is shared-rank-weighted (see _buildSharedItems/
        # _sharedRankScore), so it's the same regardless of who's viewing
        # OR what sortBy they picked, and its detail link resolves
        # because the viewer played it.
        theirArtistIds = {a["id"] for a in their["sharedArtistsPool"]}
        theirSongIds = {s["id"] for s in their["sharedSongsPool"]}
        theirAlbumIds = {a["id"] for a in their["sharedAlbumsPool"]}
        similarities = {
            "commonTopArtist": sharedArtists[0] if sharedArtists else None,
            "commonTopSong": sharedSongs[0] if sharedSongs else None,
            "commonTopAlbum": sharedAlbums[0] if sharedAlbums else None,
            "sharedArtistCount": sum(1 for a in my["sharedArtistsPool"] if a["id"] in theirArtistIds),
            "sharedSongCount": sum(1 for s in my["sharedSongsPool"] if s["id"] in theirSongIds),
            "sharedAlbumCount": sum(1 for a in my["sharedAlbumsPool"] if a["id"] in theirAlbumIds),
            "poolSize": COMPARE_SHARED_POOL_SIZE,
        }
        # Genre comparison (and the genre category folded into taste
        # match below) requires BOTH sides past the unlock gate -
        # comparing a complete genre profile against a half-backfilled
        # one would misrepresent the half-backfilled user's taste. See
        # chartsPage's identical kill-switch comment for why this is
        # checked before any genre query.
        lastfmEnabled = dashboard.repo.isLastfmGenreBackfillEnabled()
        myGenreCoverage = emptyGenreCoverage()
        theirGenreCoverage = emptyGenreCoverage()
        genresUnlocked = False
        myGenrePool = None
        theirGenrePool = None
        myTopGenres = None
        theirTopGenres = None
        sharedGenres = None
        if lastfmEnabled:
            myGenreCoverage = resolveGenreCoverage(db, startDate, endDate)
            theirGenreCoverage = resolveGenreCoverage(otherDb, startDate, endDate)
            genresUnlocked = genreGatePasses(myGenreCoverage) and genreGatePasses(theirGenreCoverage)
        if genresUnlocked:
            myGenrePool = resolveGenreDistribution(db, startDate, endDate,
                                                   COMPARE_GENRE_POOL_SIZE)
            theirGenrePool = resolveGenreDistribution(otherDb, startDate, endDate,
                                                      COMPARE_GENRE_POOL_SIZE)
            myTopGenres = dict(list(myGenrePool.items())[:COMPARE_TOP_GENRES_LIMIT])
            theirTopGenres = dict(list(theirGenrePool.items())[:COMPARE_TOP_GENRES_LIMIT])
            # Shared genres: the pools' intersection, ordered by combined
            # plays (name breaks ties) - like the shared-item lists, the
            # overlap runs over the deeper pools, not just the displayed top.
            sharedGenres = [
                {"genre": genre, "myPlays": myGenrePool[genre],
                 "theirPlays": theirGenrePool[genre],
                 "combinedPlays": myGenrePool[genre] + theirGenrePool[genre]}
                for genre in set(myGenrePool) & set(theirGenrePool)
            ]
            sharedGenres.sort(key=lambda item: (-item["combinedPlays"], item["genre"]))
            sharedGenres = sharedGenres[:COMPARE_TOP_GENRES_LIMIT]

        tasteMatch = _tasteMatchPercent(my, their, myGenrePool, theirGenrePool)

        trendStartDate, trendEndDate = startDate, endDate
        if trendStartDate is None or trendEndDate is None:
            # "All Time" passes no explicit range, and getListeningTimeSeries
            # then gap-fills each user only across their own first-to-last
            # play - two users with disjoint listening eras would union into
            # an axis with the years between them missing entirely. Pin both
            # series to one combined range instead.
            playRanges = [r for r in (dashboard.repo.getPlayTimeRange(username),
                                      dashboard.repo.getPlayTimeRange(withUsername)) if r]
            if playRanges:
                trendStartDate = convertToDatetime(min(r[0] for r in playRanges), tz=db.tz)
                trendEndDate = convertToDatetime(max(r[1] for r in playRanges), tz=db.tz) + timedelta(seconds=1)

        # Auto ("") buckets from the range span - the shared resolver every
        # trend-bucket control goes through now (see _resolveGroupBy).
        groupBy = dashboard._resolveGroupBy(groupByParam, trendStartDate, trendEndDate)
        # Single-day ranges bucket by hour, mirroring chartsPage's
        # isSingleDayView - one 'day' bucket would collapse the whole
        # trend into a single point.
        trendGroupBy = "hour" if interval in ("day", "today") else groupBy

        # Each Database instance buckets time-series labels in its own
        # user's timezone, so the two series can still have edge buckets
        # the other lacks - built from the union of both sides' labels
        # rather than assumed to line up positionally.
        myByLabel = {b["label"]: b["totalTimeListened"] for b in db.getListeningTimeSeries(trendStartDate, trendEndDate, groupBy=trendGroupBy)}
        theirByLabel = {b["label"]: b["totalTimeListened"] for b in otherDb.getListeningTimeSeries(trendStartDate, trendEndDate, groupBy=trendGroupBy)}
        allLabels = sorted(set(myByLabel) | set(theirByLabel))
        comparisonTrend = {
            "buckets": allLabels,
            "series": [
                {"name": username, "data": [myByLabel.get(label, 0) for label in allLabels]},
                {"name": withUsername, "data": [theirByLabel.get(label, 0) for label in allLabels]},
            ],
        }

        if request.args.get("ajax") == "true":
            # Same fade-and-swap partial updates as the Wrapped page: the
            # filter controls fetch these chunks and swap them in place
            # instead of a full page reload.
            return jsonify({
                **sortableListsJson(),
                "withUsername": withUsername,
                "tasteMatch": tasteMatch,
                "statsTableHtml": render_template(
                    "_compare_stats_table.html", my=my, their=their,
                    username=username, withUsername=withUsername),
                "similaritiesHtml": render_template(
                    "_compare_similarities.html", similarities=similarities,
                    username=username),   #< the cover-image URLs' session-authorization segment
                "genresHtml": render_template(
                    "_compare_genres.html", username=username, withUsername=withUsername,
                    myTopGenres=myTopGenres, theirTopGenres=theirTopGenres,
                    sharedGenres=sharedGenres, myGenreCoverage=myGenreCoverage,
                    theirGenreCoverage=theirGenreCoverage, genresUnlocked=genresUnlocked,
                    lastfmEnabled=lastfmEnabled),
                "sharedArtistsHtml": render_template(
                    "_wrapped_list.html", items=sharedArtists, section="top_artists",
                    username=username, compareWith=withUsername,
                    emptyMessage="No shared top artists in this period yet."),
                "sharedSongsHtml": render_template(
                    "_wrapped_list.html", items=sharedSongs, section="top_songs",
                    username=username, compareWith=withUsername,
                    emptyMessage="No shared top songs in this period yet."),
                "sharedAlbumsHtml": render_template(
                    "_wrapped_list.html", items=sharedAlbums, section="top_albums",
                    username=username, compareWith=withUsername,
                    emptyMessage="No shared top albums in this period yet."),
                "comparisonTrend": comparisonTrend,
            })

        return render_template(
            "compare.html",
            section="compare",
            username=username,
            withUsername=withUsername,
            acceptedUsernames=acceptedUsernames,
            my=my,
            their=their,
            sharedArtists=sharedArtists,
            sharedSongs=sharedSongs,
            sharedAlbums=sharedAlbums,
            similarities=similarities,
            tasteMatch=tasteMatch,
            myTopGenres=myTopGenres,
            theirTopGenres=theirTopGenres,
            sharedGenres=sharedGenres,
            myGenreCoverage=myGenreCoverage,
            theirGenreCoverage=theirGenreCoverage,
            genresUnlocked=genresUnlocked,
            lastfmEnabled=lastfmEnabled,
            comparisonTrend=comparisonTrend,
            interval=interval,
            customStart=customStart,
            customEnd=customEnd,
            limit=limit,
            limitOptions=WRAPPED_LIMIT_OPTIONS,
            #< the raw param, not the resolved bucketing - links that pin
            #  the auto-derived value would freeze auto mode
            groupBy=groupByParam,
            sortBy=sortBy,
        )
    app.add_url_rule("/compare", "comparePage", comparePage, methods=["GET"])
