# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The dedicated /genres page: a genre-profile overview (distribution bars,
share donut, genre-mix-over-time) plus a per-genre drill-down (stat strip,
bucketed trend, listening clock, top artists, top tracks). All genre surfaces
gate behind the same Last.fm coverage unlock as Charts/Wrapped/Compare. Both
trend charts follow the shared Trend-buckets control (Auto default, resolved
server-side like every other page - see _resolveGroupBy).

The page loads in two phases, like Charts: the initial GET renders a lightweight
shell (filter controls + an empty placeholder), and the data arrives in a second
request right after first paint, and again on every filter/genre change. A
time-period filter (defaulting to the user's profile default window) scopes
every displayed chart/stat; the unlock GATE, however, stays all-time (unlock is
a library-wide achievement, like the Overview coverage card), so a narrow window
never hides the whole page.

That second request is driven by htmx rather than the page's own fetch() code,
so it is marked by htmx's HX-Request header rather than the ?ajax=true
convention the remaining shell pages use, and answered with an HTML FRAGMENT
rather than a JSON envelope - htmx swaps response bodies, so {"detailHtml": ...}
would land in the page verbatim. Keying on a header rather than a query param is
also what keeps the marker out of the address bar: hx-replace-url writes back
the URL that was requested.

Two fragment shapes, told apart by HX-Target - htmx names the region it is
replacing, so "which region" IS "which fragment", and nothing about the request
shape leaks into the shareable URL:

  #genresResults (default) - the whole data zone: overview datasets + chip row
      + the selected genre's detail. A filter change, and the first load.
  #genreExplore            - the chip row + detail only, for the chip-click
      drill-down swap. The overview is unchanged by a genre switch, so its
      three queries are skipped entirely.

Chart datasets are not markup, so each fragment carries its own inside a
<script type="application/json"> island that static/js/genres.js reads back out
in htmx:afterSwap. See tests/test_genres_htmx.py.

Follows the project's route convention: register(app, dashboard), handler
closures, add_url_rule. app-level constants are aliased at register() time.
"""
import logging

from flask import Response, render_template, request, url_for

from routes._htmx import isHtmxSwap

from config import (
    GENRE_PAGE_LIST_LIMIT, GENRE_MIX_TREND_TOP_N, GENRE_PAGE_TOP_ARTISTS_LIMIT,
    GENRE_PAGE_TOP_TRACKS_LIMIT, LISTEN_TIME_HIDE_SECONDS_ABOVE_HOURS,
)
from dashboard.date_ranges import GROUP_BY_APPROX_BUCKET_DAYS, isSingleDayInterval
from Database.utils import msToString, convertToDatetime
from services.genre_gate import (
    emptyGenreCoverage, resolveGenreCoverage, genreGatePasses, resolveGenreDistribution,
    resolveGenreTrends, resolveGenreStats, resolveTopArtistsForGenre, resolveTopTracksForGenre,
    resolveGenreHeatmap, emptyHeatmapGrid, resolveGenreArtistCounts,
)

logger = logging.getLogger(__name__)

# The two htmx swap targets, by element id. htmx sends the target's id in the
# HX-Target header, and that is what picks the fragment below - so these have to
# agree with templates/genres.html and templates/_genres_results.html, which
# tests/test_genres_htmx.py asserts. A renamed id would silently send the full
# payload (and re-run the three overview queries) on every chip click.
GENRES_RESULTS_ID = "genresResults"
GENRE_EXPLORE_ID = "genreExplore"

def emptyTrend():
    """The trend dataset for a range with no genres at all, so the fragment
    still carries a well-formed island for genres.js to read. A function, not a
    module constant, for the same reason emptyHeatmapGrid() is one: a shared
    mutable default is one careless caller away from leaking between requests."""
    return {"buckets": [], "series": []}


def register(app, dashboard):

    def _buildGenreDetail(db, username, selectedGenre, startDate, endDate, intervalLabel, trendGroupBy):
        """The per-genre drill-down context (stat strip + top lists) plus its
        two chart datasets (bucketed trend line, listening-clock heatmap), all
        scoped to the selected date range. `trendGroupBy` is the resolved
        Trend-buckets size (hour on single-day views - see genresPage).
        Shared by the full fragment and the chip-click detail swap so they
        can't drift."""
        selectedTrend = resolveGenreTrends(db, [selectedGenre], startDate, endDate, groupBy=trendGroupBy)
        clock = dashboard._embedHeatmapTextElements(resolveGenreHeatmap(db, selectedGenre, startDate, endDate))
        topArtists = resolveTopArtistsForGenre(db, selectedGenre, GENRE_PAGE_TOP_ARTISTS_LIMIT, startDate, endDate)
        topTracks = resolveTopTracksForGenre(db, selectedGenre, GENRE_PAGE_TOP_TRACKS_LIMIT, startDate, endDate)
        stats = resolveGenreStats(db, selectedGenre, startDate, endDate)
        firstTs = stats.get("firstPlayedTs")
        genreStatsView = {
            "plays": stats.get("plays", 0),
            "listenText": msToString(stats.get("listenMs", 0),
                                     hideSecondsAboveHours=LISTEN_TIME_HIDE_SECONDS_ABOVE_HOURS),
            "sharePercent": stats.get("sharePercent", 0.0),
            "firstPlayedText": convertToDatetime(firstTs, tz=db.tz).strftime("%b %Y") if firstTs else "—",
        }
        return {
            # username is supplied by the caller (the fragment branch re-adds it)
            # so it never collides with genres.html's own. intervalLabel lets the
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
            return dashboard.unauthenticatedResponse()

        settings = db.repo.getUserSettings(username)
        defaultWindow = settings.get("default_dashboard_window", "day")

        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")
        #< resolved like /charts and the dashboard (see
        #  DateRangeMixin._resolveIntervalParam) - same reason they carry it:
        #  ?interval= present and empty must not fall through unvalidated,
        #  or the All Time <option> below (which only matches the "all time"
        #  spelling) selects nothing and the control shows its first option
        #  over default-window numbers. Both defaults are defaultWindow here;
        #  this page has no second default the way the Top pages do.
        interval = dashboard._resolveIntervalParam(defaultWindow, defaultWindow, customStart, customEnd)
        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd, default=defaultWindow, tz=db.tz)
        #< same default as _getDateRange above (see charts.py)
        intervalLabel = dashboard._getIntervalLabel(interval, customStart, customEnd,
                                                    default=defaultWindow)

        #< the raw param, not the resolved bucketing - the template's select
        #  must keep showing Auto rather than pinning the derived value
        groupByParam = request.args.get("groupBy", "")
        spanStart, spanEnd = startDate, endDate
        if spanStart is None or spanEnd is None:
            spanStart, spanEnd = dashboard._playRangeSpanDates(username, db.tz)   #< "All Time" has no explicit range
        groupBy = dashboard._resolveGroupBy(groupByParam, spanStart, spanEnd)
        # Single-day views bucket by hour, mirroring chartsPage - one 'day'
        # bucket would collapse both trend charts into a single point (the
        # template hides the Trend-buckets control there too).
        trendGroupBy = "hour" if interval in ("day", "today") else groupBy

        # The filter state as MARKUP is allowed to see it: validated, so the
        # shell's first-load URL, the chip hrefs and the queries can never
        # disagree about what is on screen. Echoing request.args instead would
        # put a junk ?interval=bogus back into the one place a reader trusts,
        # after the query had already coerced it away - the same rule
        # historyPage's listUrl follows.
        #
        # A custom range's dates ride along only while that range is in effect,
        # matching the shell's disabled date inputs; an unrecognised bucket size
        # is dropped, because _resolveGroupBy has already replaced it with a
        # span-derived one and the template's select shows Auto for it.
        filterArgs = {
            "interval": interval,
            "startDate": customStart if interval == "custom" else "",
            "endDate": customEnd if interval == "custom" else "",
            "groupBy": groupByParam if groupByParam in GROUP_BY_APPROX_BUCKET_DAYS else "",
        }
        #< url_for renders an empty value as a bare `?startDate=`, so drop them
        chipArgs = {key: value for key, value in filterArgs.items() if value}

        # The genre is the one filter this phase cannot validate: deciding
        # whether it exists needs the distribution query the shell deliberately
        # defers. Passing it through is safe - the data request resolves it, and
        # falls back to the range's top genre when it no longer has plays.
        requestedGenre = request.args.get("genre", "")

        def viewUrl(genre=""):
            """The canonical full-page URL for the current filter state. Used
            for the shell's first-load request, and as the recovery navigation
            when a drill-down swap is declined."""
            args = dict(chipArgs, genre=genre) if genre else dict(chipArgs)
            return url_for("genresPage", **args)

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
        # data query is deferred to the fragment below, which htmx requests
        # after first paint and on each filter/genre change. A locked or
        # disabled shell carries no htmx wiring at all, so it never asks.
        #
        # intervalLabel and defaultWindow no longer ride along: the mix chart's
        # heading moved into the fragment, and defaultWindow only ever fed the
        # popstate handler, which every-update-is-a-replace made unnecessary.
        if not isHtmxSwap():
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
                groupBy=groupByParam,
                #< the first paint's half of the Trend-buckets hide rule; every
                #  change after it goes through htmx-filters' hidesTrendBuckets
                isSingleDayView=isSingleDayInterval(interval),
                #< the raw genre, for the form's hidden field: genres.js replaces
                #  it with the resolved one as soon as the first fragment lands
                selectedGenre=requestedGenre,
                dataUrl=viewUrl(genre=requestedGenre),
            )

        # ---- htmx fragment (scoped to the selected window) ----
        #< which region htmx is replacing, and so which fragment answers it
        detailOnly = request.headers.get("HX-Target") == GENRE_EXPLORE_ID

        if not genreUnlocked:
            # The gate is all-time and stable, so a live page whose shell
            # rendered unlocked should never land here. The two shapes recover
            # differently, exactly as the old ok:false branches did:
            #  - a drill-down swap navigates to the full page render, which is
            #    the one that can explain why (the old detailFallbackUrl);
            #  - a full swap does nothing at all. 204 is htmx's "no swap", so
            #    the placeholder stays rather than being replaced by something
            #    that could ask again and loop.
            if detailOnly:
                return Response(status=204, headers={"HX-Redirect": viewUrl(genre=requestedGenre)})
            return Response(status=204)

        distribution = resolveGenreDistribution(db, startDate, endDate, GENRE_PAGE_LIST_LIMIT)
        genreNames = list(distribution.keys())
        selectedGenre = requestedGenre if requestedGenre in distribution else (genreNames[0] if genreNames else None)

        def exploreContext(detail):
            """Everything the chip row + drill-down need. Built once and used by
            both fragments (the chip-click one renders it alone, the full one
            includes it), so a genre switch and a filter change can never render
            the pair differently."""
            context = {
                "username": username,
                "genreNames": genreNames,
                "chipArgs": chipArgs,
                "selectedTrend": detail["selectedTrend"] if detail else emptyTrend(),
                "clock": detail["clock"] if detail else emptyHeatmapGrid(),
            }
            #< no detail at all means the range has no genres; the partial's own
            #  `if selectedGenre` guard renders nothing, and the chip row is empty
            context.update(detail["context"] if detail else
                           {"selectedGenre": None, "intervalLabel": intervalLabel})
            return context

        def buildDetail():
            return (_buildGenreDetail(db, username, selectedGenre, startDate, endDate,
                                      intervalLabel, trendGroupBy)
                    if selectedGenre else None)

        # Chip-click drill-down: the chips (so the selected one moves) and the
        # re-rendered detail. The distribution above is needed either way - to
        # resolve the genre and label the chips - but the mix trend and the
        # breadth counts are not, and skipping them is the whole point of having
        # two shapes.
        if detailOnly:
            return render_template("_genre_explore.html", **exploreContext(buildDetail()))

        # Full fragment: overview datasets + chip row + the selected detail. The
        # chip row rides along because a range change changes which genres have
        # plays (and thus appear). The mix-over-time trend is computed before the
        # per-genre detail so its top-N query runs first.
        mixTrend = resolveGenreTrends(db, genreNames[:GENRE_MIX_TREND_TOP_N], startDate, endDate, groupBy=trendGroupBy)
        # Breadth (distinct artists per genre) stays all-time - the underlying
        # count has no date-range variant - so it reads as overall taste breadth
        # for the genres present in the range. Ranked most-artists-first.
        breadth = resolveGenreArtistCounts(db, genreNames)
        breadthPairs = sorted(breadth.items(), key=lambda kv: (-kv[1], kv[0]))

        return render_template(
            "_genres_results.html",
            distributionPairs=list(distribution.items()),
            breadthPairs=breadthPairs,
            mixTrend=mixTrend,
            **exploreContext(buildDetail()),
        )
    app.add_url_rule("/genres", "genresPage", genresPage, methods=["GET"])
