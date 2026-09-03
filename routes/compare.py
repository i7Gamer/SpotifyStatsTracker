# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The /compare page - two mutually-shared users' stats side by side.

Extracted verbatim from app.py. The taste-match scoring and genre-gate helpers
come from services/; the app-level display constants (COMPARE_*,
WRAPPED_LIMIT_OPTIONS) are aliased from the app module at register() time so the
handler body stays unchanged. Everything else is reached through the dashboard
instance.

The second phase of this page's two-phase load is driven by htmx, like /history
and the Top lists - see tests/test_compare_htmx.py for the transport contract
and templates/compare.html for the attribute wiring.
"""
from datetime import timedelta

from flask import render_template, request, abort, url_for, Response, stream_with_context

from routes._htmx import isHtmxSwap
from routes.tags import FILENAME_UNSAFE_RE

from config import (
    WRAPPED_LIMIT_OPTIONS, COMPARE_TOP_LIST_SIZE, COMPARE_SHARED_POOL_SIZE,
    COMPARE_GENRE_POOL_SIZE, COMPARE_TOP_GENRES_LIMIT, PLAYLIST_EXPORT_FORMATS,
)
from services.export import resolvePlaylistFormat
from Database.utils import convertToDatetime
from services.taste_match import _markLinkExternally, _tasteMatchPercent
from services.genre_gate import (
    emptyGenreCoverage, resolveGenreCoverage, genreGatePasses, resolveGenreDistribution,
)

# The Time Period dropdown's own options (see compare.html). "" is All Time -
# and unlike every other param it stays in a URL even when empty, because an
# ABSENT interval falls back to the user's saved default_dashboard_window:
# dropping it would silently move an All Time view to "Last Month".
COMPARE_INTERVALS = ("", "today", "day", "week", "month", "year", "5years", "custom")
# The Trend buckets dropdown's own options. Auto is expressed by leaving groupBy
# out entirely, which is the same thing _resolveGroupBy does with an empty one.
COMPARE_TREND_BUCKETS = ("day", "week", "month")
# The ?scope= the Sort by control sends, narrowing the refresh to the six
# individual my/their lists (the only regions whose content reads sortBy).
COMPARE_SORTABLE_SCOPE = "sortable"
# What the shell's trend data island holds until the first swap replaces it:
# the shape charts.js expects, so a resize before the data lands draws the
# chart's empty state rather than throwing on a missing key.
EMPTY_COMPARISON_TREND = {"buckets": [], "series": []}


def _compareFilterArgs(withUsername, interval, customStart, customEnd, limit, groupBy, sortBy):
    """The canonical query args for a /compare URL, from VALIDATED values only.

    Every /compare URL the server writes goes through here - the shell's
    first-load hx-get, each counterpart badge's href, and the HX-Replace-Url the
    narrow sort swap answers with - so they cannot disagree with each other or
    echo back something the query itself already coerced. Reflecting a raw
    ?interval=bogus would assert it again in the one place a reader would trust,
    while every link built beside it says otherwise.

    A custom range rides along only while it is actually in effect, which is
    both what _getDateRange means by "in effect" (both dates present beats the
    interval string) and what the shell's disabled date inputs serialize.
    """
    args = {
        #< the identity, not the label: ?with= is matched against the
        #  accepted-share list server-side, so it must survive every refresh
        "with": withUsername,
        "interval": interval if interval in COMPARE_INTERVALS else "",
        "limit": limit,
        "sortBy": sortBy,
    }
    if customStart and customEnd:
        args["startDate"] = customStart
        args["endDate"] = customEnd
    if groupBy in COMPARE_TREND_BUCKETS:
        args["groupBy"] = groupBy
    return args


def register(app, dashboard):
    # App-level display constants this route depends on (defined in app.py and
    # also used by dashboard._gatherCompareStats / the Wrapped page).

    def comparePage():
        if not dashboard.repo.isDataSharingEnabled():
            abort(404)
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return dashboard.unauthenticatedResponse()

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
            # A real page explaining what to do next, not a bare 404 - covers
            # both "never requested a share" and the cookie-less-counterpart
            # edge case above alike, since either way there's nothing to
            # compare against right now.
            if isHtmxSwap():
                # Reached when the counterpart revokes the share while the page
                # is open. A whole page is not a fragment: the swap asks for no
                # primary target (hx-swap="none"), so this would vanish without
                # a trace and leave the comparison of a share that no longer
                # exists on screen. Send the browser to the page instead, the
                # same escape unauthenticatedResponse uses for the same reason.
                return Response(status=204, headers={"HX-Redirect": url_for("comparePage")})
            return render_template("compare_empty.html", username=username, section="compare")

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

        # Every URL this page hands out is built from the values validated
        # above, never echoed from request.full_path - see _compareFilterArgs.
        filterArgs = _compareFilterArgs(withUsername, interval, customStart, customEnd,
                                        limit, groupByParam, sortBy)
        #< the counterpart picker: each badge's href IS the whole next request
        #  now that htmx boosts it, so it carries the current filters rather
        #  than only ?with= (which is all the old click listener read off it)
        userBadges = [
            {"username": candidate, "url": url_for("comparePage", **{**filterArgs, "with": candidate})}
            for candidate in acceptedUsernames
        ]

        # Lightweight shell, same two-phase load as /charts, /genres and
        # /history: the initial GET renders just the filter controls + empty
        # placeholders, and the real comparison arrives in a second request
        # right after first paint, and again on every filter change. Every
        # query below this point only runs for that second request.
        #
        # htmx drives it, so the marker is htmx's own HX-Request header instead
        # of the ?ajax=true convention the remaining shell pages use. Keying on
        # the header rather than a query param is also what keeps the marker out
        # of the URL bar: hx-replace-url writes back the URL that was requested,
        # so a marker living in the query string would become part of the page's
        # shareable address. See tests/test_compare_htmx.py.
        if not isHtmxSwap():
            return render_template(
                "compare.html",
                section="compare",
                username=username,
                withUsername=withUsername,
                acceptedUsernames=acceptedUsernames,
                userBadges=userBadges,
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                limit=limit,
                limitOptions=WRAPPED_LIMIT_OPTIONS,
                #< the raw param, not the resolved bucketing - links that pin
                #  the auto-derived value would freeze auto mode
                groupBy=groupByParam,
                sortBy=sortBy,
                resultsUrl=url_for("comparePage", **filterArgs),
                #< the shell knows neither yet; the first swap fills both in
                tasteMatch=None,
                comparisonTrend=EMPTY_COMPARISON_TREND,
            )

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

        # Shared by both branches below, so a full refresh and a sort change can
        # never disagree about what one of the six lists looks like.
        #< each item's own linkExternally decides internal vs. Spotify (see _markLinkExternally)
        listArgs = dict(my=my, their=their, username=username, compareWith=withUsername,
                        emptyMessage="No plays in this period.",
                        #< the cards' "First Listened" is a MIN over the selected
                        #  range (see _track_card.html); only All Time - the one
                        #  interval _getDateRange leaves open - is a lifetime first
                        rangeScopedFirstListen=startDate is not None)

        # A sortBy change swaps only those six lists, so its request
        # (?scope=sortable, see the Sort by control in compare.html) stops here
        # - the shared lists, similarities, genres, taste match and trend below
        # are the expensive half on long ranges and would render identically
        # anyway. Any other scope value degrades to the full refresh.
        if request.args.get("scope") == COMPARE_SORTABLE_SCOPE:
            # The scope marker is transport, not page state, and hx-replace-url
            # writes back the URL that was REQUESTED - so without this the
            # address bar (and every link copied from it) would carry
            # ?scope=sortable. Answering with the canonical URL overrides that,
            # and is the same validated-values rule _compareFilterArgs exists
            # for. htmx honours this header over the attribute.
            return Response(
                render_template("_compare_sortable_lists.html", **listArgs),
                headers={"HX-Replace-Url": url_for("comparePage", **filterArgs)},
            )

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
        #< series names are a chart legend - the label people go by, not the key
        displayNames = dashboard.repo.getDisplayNames([username, withUsername])
        myDisplayName = displayNames.get(username, username)
        withDisplayName = displayNames.get(withUsername, withUsername)
        comparisonTrend = {
            "buckets": allLabels,
            "series": [
                {"name": myDisplayName, "data": [myByLabel.get(label, 0) for label in allLabels]},
                {"name": withDisplayName, "data": [theirByLabel.get(label, 0) for label in allLabels]},
            ],
        }

        # Reaching here means an htmx request for the full refresh (both the
        # shell and the narrow sort branch return earlier). ONE fragment, whose
        # every element names the region it replaces - see
        # _compare_results.html; a JSON envelope around these would land in the
        # page as literal text, since htmx swaps response bodies.
        return render_template(
            "_compare_results.html",
            #< withUsername stays the identity (?with=, authorization) and the
            #  session-authorization segment of the cover-image URLs; the name
            #  labels are swapped from the | displayName of it
            withUsername=withUsername,
            acceptedUsernames=acceptedUsernames,
            userBadges=userBadges,
            tasteMatch=tasteMatch,
            similarities=similarities,
            myTopGenres=myTopGenres, theirTopGenres=theirTopGenres,
            sharedGenres=sharedGenres, myGenreCoverage=myGenreCoverage,
            theirGenreCoverage=theirGenreCoverage, genresUnlocked=genresUnlocked,
            lastfmEnabled=lastfmEnabled,
            sharedArtists=sharedArtists, sharedSongs=sharedSongs, sharedAlbums=sharedAlbums,
            comparisonTrend=comparisonTrend,
            #< rides the swapped fragment, not the shell, so the download
            #  always carries the filters the visible lists were built from
            blendExportUrl=url_for("compareBlendExport", **filterArgs),
            **listArgs,
        )
    app.add_url_rule("/compare", "comparePage", comparePage, methods=["GET"])

    def compareBlendExport():
        """The two users' shared songs as a playlist file - the Top Common
        Songs list, uncapped.

        Same guards, same range resolution, same pool call and the same
        _buildSharedItems ranking as comparePage itself, so the file can never
        disagree with the page it was downloaded from. The formats are the
        Playlists page's own exporters (see resolvePlaylistFormat)."""
        if not dashboard.repo.isDataSharingEnabled():
            abort(404)
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return dashboard.unauthenticatedResponse()

        #< same cookies guard as comparePage: get_user_db needs stored cookies
        acceptedUsernames = [
            u for u in dashboard.repo.getAcceptedShareUsernames(username)
            if dashboard.repo.getUserCookies(u) is not None
        ]
        if not acceptedUsernames:
            abort(404)   #< the page itself renders the empty state; a file can't
        withUsername = request.args.get("with", acceptedUsernames[0])
        if withUsername not in acceptedUsernames:
            #< same fallback rule as comparePage: ?with= is untrusted input and
            #  must never select data the session user has no mutual share with
            withUsername = acceptedUsernames[0]
        otherDb = dashboard.get_user_db(withUsername, dashboard.repo.getEmailForUsername(withUsername))

        settings = dashboard.repo.getUserSettings(username)
        defaultWindow = settings.get("default_dashboard_window", "day")
        if defaultWindow == "all time":
            defaultWindow = ""
        interval = request.args.get("interval", defaultWindow)
        startDate, endDate = dashboard._getDateRange(
            interval, request.args.get("startDate", ""), request.args.get("endDate", ""),
            default="all time", tz=db.tz)

        #< the deeper shared pool, not the displayed page - the same
        #  COMPARE_SHARED_POOL_SIZE call _gatherCompareStats feeds the page's
        #  Top Common list from, ranked by the same shared score. embedFn is
        #  identity: the exporters read catalog fields the rows already carry,
        #  and the page's text embellishments are display-only
        myPool = db.getTopSongs(startDate, endDate, limit=COMPARE_SHARED_POOL_SIZE)
        theirPool = otherDb.getTopSongs(startDate, endDate, limit=COMPARE_SHARED_POOL_SIZE)
        tracks = dashboard._buildSharedItems(myPool, theirPool, lambda items: items, limit=None)

        fmt = request.args.get("format", "csv").lower()
        if fmt not in PLAYLIST_EXPORT_FORMATS:
            fmt = "csv"
        pairName = FILENAME_UNSAFE_RE.sub("_", f"{username}_{withUsername}").strip("_")
        generator, mimetype = resolvePlaylistFormat(
            tracks, fmt, title=f"Blend: {username} and {withUsername}")
        response = Response(stream_with_context(generator), mimetype=mimetype)
        response.headers["Content-Disposition"] = f'attachment; filename="blend_{pairName}.{fmt}"'
        return response
    app.add_url_rule("/compare/blend", "compareBlendExport", compareBlendExport, methods=["GET"])
