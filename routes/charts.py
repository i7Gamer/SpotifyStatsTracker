# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Main stats pages: the public /overview, the dashboard index (/), the Top
Songs/Albums/Artists lists, the /charts analytics page, and the song/artist/
album detail pages.

Extracted verbatim from app.py. Genre-gate/coverage helpers come from services/;
the PAGE_SIZE / CHART_* constants come from config. Every stats/pagination/embed
helper is reached through the dashboard instance.
"""
import logging

from flask import render_template, redirect, request, url_for, session, jsonify, Response

from config import (
    PAGE_SIZE, CHART_ARTIST_TREND_TOP_N, CHART_TOP_GENRES_LIMIT,
    CHART_MOST_SKIPPED_LIMIT, TOP_LIST_SORT_BY, ON_THIS_DAY_YEARS_LIMIT,
    LISTEN_TIME_HIDE_SECONDS_ABOVE_HOURS, RECOMMENDATION_ARTIST_LIMIT,
    RECOMMENDATION_GENRE_POOL, RECOMMENDATION_EXCLUDE_TOP_N,
)
from routes._auth import makeRequiresUser
from Database.database import Database
from Database.utils import dateToString, msToString
from services.genre_gate import (
    emptyGenreCoverage, resolveGenreCoverage, genreGatePasses, resolveGenreDistribution,
    emptyBiographyCoverage, resolveBiographyCoverage,
)
from services.milestones import buildNextMilestones, formatMilestone, MS_PER_HOUR

logger = logging.getLogger(__name__)

# The detail pages' htmx swap targets: the id of the element the second (third,
# fourth) request is filling, which is what htmx puts in its HX-Target header.
#
# These routes are the only ones in the app with MORE THAN ONE htmx mode, so the
# HX-Request header alone cannot say which fragment is wanted. HX-Target can, and
# it costs nothing extra: htmx already sends it, and unlike the ?ajax= marker it
# replaces, it never becomes part of the URL. That matters here because the play
# log's swaps carry hx-replace-url, which writes the requested URL back to the
# address bar - a marker in the query string would end up in every link a visitor
# copies. The one mode that is NOT htmx keeps ?ajax=true: it answers the
# Trend-buckets select with chart DATA for a canvas (see detail-chart.js), and
# htmx swaps markup, not data.
DETAIL_BODY_TARGET = "detailBody"            #< the whole deferred body
DETAIL_HISTORY_TARGET = "detailHistoryResults"   #< the play log, re-sorted/paged
DETAIL_MORE_TARGET = "timelineActions"       #< "Show more": one appended batch

#< How many pages' worth of rows one ?limit= may ask for. The detail history's
#  "Show more" grows its batch, so limit legitimately exceeds PAGE_SIZE - but it
#  was the one pagination parameter in the codebase with no ceiling at all, so
#  ?limit=500000 fetched and rendered half a million rows. Own data only, so this
#  is a footgun rather than a hole, and every other pager is already clamped.
MAX_DETAIL_HISTORY_PAGES = 10


def _detailSwapTarget():
    """Which region of a detail page an htmx request is asking to fill, or ""
    for a plain page load.

    Falls back to the whole body when htmx sends no HX-Target (it only omits the
    header for a target with no id, which none of ours is): answering an
    HX-Request with the SHELL would swap a whole document into a region, which
    is the one outcome worth ruling out by construction."""
    if not request.headers.get("HX-Request"):
        return ""
    return request.headers.get("HX-Target") or DETAIL_BODY_TARGET


def _positivePageArg():
    """?page= as a string when it names a real page, "" otherwise.

    Only /history's shell needs this, and only to decide whether to carry the
    page into the URL its placeholder loads from. The list request clamps the
    page properly (_calculatePagination), so this is not validation - it is
    deciding what is worth reflecting back into the markup. Junk gets left out
    rather than echoed, same reasoning as the validated interval beside it."""
    raw = request.args.get("page", "")
    return raw if raw.isdigit() and int(raw) > 0 else ""


def register(app, dashboard):
    # Read off the class, not the per-request db instance. A route test's
    # MagicMock db answers `db.SKIP_SORT_BY` with a Mock, which equals no
    # string - so every branch guarded on it silently took the other path and
    # the whole skip-count path went untested.
    SKIP_SORT_BY = Database.SKIP_SORT_BY
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
            # Validated, like sortBy beside it. The DATA was always safe
            # (_getDateRange coerces junk to the default), but the raw value
            # reached _buildPaginationContext, so a stale or truncated URL put
            # ?interval=bogus into every page link - and now also into the URL
            # the shell loads its list from. "" is itself a valid interval here
            # (All Time), which is why it is both the input default and the
            # fallback. Same fix historyPage already carries.
            "interval": dashboard._getValidInterval(request.args.get("interval", ""), default=""),
            "customStart": request.args.get("startDate", ""),
            "customEnd": request.args.get("endDate", ""),
            "tag": request.args.get("tag", "") if tagsOn else "",
            "fullOnly": fullOnly,
            "fullPlaysOnly": fullOnly != "0",
            #< only offered to users who've actually tagged something - see
            #  _page_card.html's {% if tags_enabled and user_tags %} guard
            "userTags": db.repo.getUserTags(username) if tagsOn else [],
        }

    def _topListShell(section, template, endpoint, username, filters):
        """The plain GET half of the two-phase load: the filter card plus an
        empty #topListResults placeholder. htmx then fetches the stat header +
        list + pagination on first paint and on every filter/sort/tag/page
        change - see templates/_page_card.html."""
        # The URL the placeholder loads from, built from the VALIDATED filter
        # values rather than echoed from request.full_path - same rule the
        # pagination links below follow, and the same one historyPage carries.
        # Reflecting the raw query string would assert a junk ?interval= in the
        # one place a reader would trust and disagree with every link beside it.
        #
        # fullOnly rides along unconditionally because it is a real tri-state to
        # the route ("1" default / "0" opt-out) that the filter card always
        # submits, so leaving it out here would make the first load and every
        # subsequent one disagree about a filter the user can see is on.
        # startDate/endDate ride along whenever they are set, rather than only
        # for interval == "custom" - deliberately matching what
        # _buildPaginationContext already puts in every page link, so the first
        # load and the links below it agree. (These pages treat "both dates
        # present" as the custom range being active; see _page_card.html's
        # option, which has always selected Custom on that condition rather
        # than on the interval value.)
        listArgs = {
            "q": filters["searchQuery"],
            "sortBy": filters["sortBy"],
            "interval": filters["interval"],
            "startDate": filters["customStart"],
            "endDate": filters["customEnd"],
            "tag": filters["tag"],
            "fullOnly": filters["fullOnly"],
            "page": _positivePageArg(),
        }
        return render_template(
            template, section=section, username=username,
            sortBy=filters["sortBy"], interval=filters["interval"],
            customStart=filters["customStart"], customEnd=filters["customEnd"],
            tag=filters["tag"], user_tags=filters["userTags"],
            fullPlaysOnly=filters["fullPlaysOnly"],
            listUrl=url_for(endpoint, **{k: v for k, v in listArgs.items() if v}))

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
        # The fragment itself, not a JSON envelope around it: htmx swaps the
        # response body straight into #topListResults, so a {"resultsHtml": ...}
        # wrapper would land in the page as literal JSON text.
        return render_template(
            "_top_list_results.html", tracks=items, statCards=statCards, startIndex=startIndex,
            section=section, username=username, emptyMessage=emptyMessage, **pagination)

    def overviewPage():
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
                                       hideSecondsAboveHours=LISTEN_TIME_HIDE_SECONDS_ABOVE_HOURS)

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
        # milestones are unfiltered, so a filter change re-renders just this one
        # partial and skips every query below entirely.
        #
        # htmx drives that swap, so the marker is its HX-Request header rather
        # than the ?ajax=true this page used to send - which also keeps the
        # marker out of the URL bar, since hx-replace-url writes the requested
        # URL back to the address bar. The fragment itself, not a JSON envelope
        # around it: htmx swaps the response body straight into
        # #dashboardSummary, so a {"summaryHtml": ...} wrapper would land in the
        # page as literal JSON text. See tests/test_dashboard_htmx.py.
        #
        # Unlike /history and the Top pages this is NOT a two-phase shell - the
        # same partial is rendered inline below, so there is no first load to
        # defer and no placeholder to trigger one from.
        if request.headers.get("HX-Request"):
            return render_template("_dashboard_summary.html", **summaryArgs)

        # Unfiltered dashboard cards (independent of the interval/date-range
        # filter above): live streak and "on this day" resurfacing are cheap
        # and rendered inline. The Discover card's genre-coverage gate and
        # recommendations are full-history queries (~700ms combined on a large
        # library - see dashboardDiscover) so they're fetched by the page's
        # own JS after first paint instead of blocking this render.
        currentStreak = db.getCurrentStreak()
        onThisDay = db.getOnThisDay(limit=ON_THIS_DAY_YEARS_LIMIT)
        lastfmGenreEnabled = dashboard.repo.isLastfmGenreBackfillEnabled()
        # Streak calendar: ~1 year of daily play counts, rendered inline below
        # the live cards. Comparable cost to getCurrentStreak above (a similar
        # bounded bucket scan), so it rides along in this render rather than
        # being deferred like the full-history Discover card.
        listeningCalendar = db.getListeningCalendar()

        # The milestones row: what's been earned, beside the progress bars
        # toward what's next. Both are gated on the admin kill switch - an
        # instance with the feature off recorded nothing, so advertising
        # progress toward milestones it will never grant was misleading, and
        # the queries behind it are pure waste there.
        #
        # getMilestonesForUser is an indexed read, measured at ~0.02ms against a
        # real 131k-play db next to the ~15ms getPlayTotals below already costs.
        # The threshold kinds cap out at 21 rows/user (9 plays + 7 listen-time +
        # 5 streak); top_artist appends one row per change of #1 artist, so the
        # table is slow-growing rather than strictly bounded - still nowhere
        # near enough to defer the way the Discover card is deferred.
        milestones = []
        nextMilestones = []
        if dashboard.repo.isMilestonesEnabled():
            milestones = [
                {**formatMilestone(row), "dateText": dateToString(row["achieved_at"], tz=db.tz)}
                for row in dashboard.repo.getMilestonesForUser(username)
            ]
            # Freeze the badge count BEFORE acknowledging, or the topbar badge
            # never renders on this page: context processors run after the view,
            # so _injectMilestoneStatus would count what markMilestonesSeen just
            # cleared. See primeMilestoneBadge.
            dashboard.primeMilestoneBadge(username)
            # Reaching this render means the cards are about to show them -
            # same acknowledgment pattern as the accepted-share notification.
            dashboard.repo.markMilestonesSeen(username)

            # "Next milestones" progress bars: lifetime totals against the same
            # thresholds detection uses. getPlayTotals is a single COUNT+SUM
            # scan; removing the play-history list from this page more than
            # pays for it.
            totalPlays, totalMs = db.getPlayTotals(None, None)
            streakDays = currentStreak.get("days", 0) if isinstance(currentStreak, dict) else 0
            nextMilestones = buildNextMilestones(totalPlays, (totalMs or 0) // MS_PER_HOUR, streakDays)

        return render_template(
            "tracks.html",
            currentStreak=currentStreak,
            onThisDay=onThisDay,
            listeningCalendar=listeningCalendar,
            milestones=milestones,
            nextMilestones=nextMilestones,
            lastfmGenreEnabled=lastfmGenreEnabled,
            friends_now_playing_enabled=dashboard.repo.isFriendsNowPlayingEnabled(),
            section="dashboard",
            interval=interval,
            customStart=customStart,
            customEnd=customEnd,
            #< no defaultWindow: it existed only as the popstate handler's
            #  fallback for a Back navigation onto a bare URL, and nothing here
            #  pushes a history entry any more - every URL update replaces
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
        # then scopes it to any named interval or a custom range. Validated
        # like dashboardIndex's (see the comment there): the DATA was safe
        # either way (_getDateRange coerces junk to the default), but the raw
        # value reached the template and _buildPaginationContext, leaving the
        # select unselected and junk in every page link on a stale URL.
        interval = dashboard._getValidInterval(request.args.get("interval") or "all time",
                                               default="all time")
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
        # placeholder, and the list (plus pagination strip) arrives in a second
        # request right after first paint, and again on every search/filter/page
        # change.
        #
        # This page drives that second request with htmx rather than its own
        # fetch() code, so the marker is htmx's own HX-Request header instead of
        # the ?ajax=true convention the other shell pages still use. Keying on
        # the header rather than a query param is also what keeps ?ajax=true out
        # of the URL bar: hx-replace-url writes back the URL that was requested,
        # so a marker living in the query string would become part of the page's
        # shareable address. See tests/test_history_htmx.py.
        if not request.headers.get("HX-Request"):
            # The URL the shell's placeholder fetches the list from. Built from
            # the VALIDATED values rather than echoed back from
            # request.full_path, which is the same rule the pagination links
            # already follow (see _buildPaginationContext below): ?interval=bogus
            # is coerced to the default for the query itself, so reflecting the
            # raw value into the markup would assert it again in the one place a
            # reader would trust, and disagree with every link built beside it.
            # Pinned by test_an_unrecognized_interval_renders_as_the_default_not_raw.
            #
            # A custom range's dates ride along only when the range is actually
            # in effect, matching the disabled date inputs in the shell - see
            # the note on `disabled` in templates/history.html.
            listArgs = {
                "q": request.args.get("q", ""),
                "interval": interval,
                "startDate": customStart if interval == "custom" else "",
                "endDate": customEnd if interval == "custom" else "",
                "sort": sortOrder if sortOrder == "oldest" else "",
                "tag": tag,
                #< a junk or out-of-range page is clamped by _calculatePagination
                #  on the list request; this only keeps a shared ?page=3 working,
                #  so anything that isn't a page number is simply left out
                "page": _positivePageArg(),
            }
            return render_template(
                "history.html",
                username=username,
                section="history",
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                sort=sortOrder,
                tag=tag,
                user_tags=userTags,
                listUrl=url_for("history", **{k: v for k, v in listArgs.items() if v}),
            )

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

        # The fragment itself, not a JSON envelope around it: htmx swaps the
        # response body straight into #historyResults, so a {"resultsHtml": ...}
        # wrapper would land in the page as literal JSON text.
        return render_template(
            "_history_results.html",
            tracks=tracks,
            startIndex=startIndex,
            interval=interval,
            is_authenticated=is_authenticated,
            username=username,
            **pagination,
        )
    app.add_url_rule("/history", "history", historyPage, methods=["GET"])

    @requiresUser(api=True)
    def dashboardDiscover(username, db):
        """The dashboard's Discover card, loaded by htmx after first paint (see
        tracks.html) rather than computed inline - the genre-coverage gate check
        and recommendation query are full-history scans that noticeably slowed
        the dashboard once added.

        Markup, not JSON: `unlocked` was a flag the client turned into one of
        three pre-rendered-and-hidden paragraphs, and the recommendations were
        rows it built element by element. Both are Jinja's job.

        Still api=True, so an expired session gets a plain 401 rather than
        unauthenticatedResponse's HX-Redirect: htmx reports the error and swaps
        nothing, which leaves this ONE card showing its placeholder. The
        alternative navigates the whole dashboard away because a background card
        load failed."""

        if not dashboard.repo.isLastfmGenreBackfillEnabled():
            return render_template("_dashboard_discover.html", unlocked=False, recommendations=[])

        unlocked = genreGatePasses(resolveGenreCoverage(db, None, None))
        recommendations = []
        if unlocked:
            recommendations = db.getRecommendedArtists(
                # Admin-tunable, read live per request; falls back to the code default.
                limit=dashboard.repo.getDiscoverArtistLimit(RECOMMENDATION_ARTIST_LIMIT),
                genrePool=RECOMMENDATION_GENRE_POOL,
                excludeTopN=RECOMMENDATION_EXCLUDE_TOP_N,
            )
        return render_template("_dashboard_discover.html", username=username,
                               unlocked=unlocked, recommendations=recommendations)
    app.add_url_rule("/api/dashboard-discover", "dashboardDiscover", dashboardDiscover, methods=["GET"])

    @requiresUser(api=True)
    def dashboardTrends(username, db):
        """The dashboard's Obsession, Rediscovery, and Forgotten Favorite trend
        cards, loaded by htmx after first paint.

        The partial was always the whole answer; it just used to travel as a
        string inside {"trendsHtml": ..., "trends": ...}, of which the client
        read one key and ignored the other. See dashboardDiscover for why this
        stays api=True."""

        return render_template("_dashboard_trends.html", username=username,
                               trends=db.getDashboardTrends())
    app.add_url_rule("/api/dashboard-trends", "dashboardTrends", dashboardTrends, methods=["GET"])

    @requiresUser
    def topSongsPage(username, db):
        filters = _topListFilters(db, username)
        if not request.headers.get("HX-Request"):
            return _topListShell("top_songs", "top_songs.html", "topSongsPage", username, filters)

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
        if not request.headers.get("HX-Request"):
            return _topListShell("top_albums", "top_albums.html", "topAlbumsPage", username, filters)

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
        if not request.headers.get("HX-Request"):
            return _topListShell("top_artists", "top_artists.html", "topArtistsPage", username, filters)

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

        # Lightweight shell: the filter card renders immediately and htmx
        # fetches the chart card below after first paint (and on every filter
        # change), so none of the heavy per-range queries block the initial
        # load. Same two-phase shape /history and the Top pages use, and the
        # same marker: htmx's own HX-Request header rather than ?ajax=true,
        # which kept the marker out of the URL bar (hx-replace-url writes the
        # requested URL back to the address bar). See tests/test_charts_htmx.py.
        if not request.headers.get("HX-Request"):
            # Built from the VALIDATED filter values rather than echoed from
            # request.full_path - the rule every migrated shell follows: junk is
            # coerced for the query itself, so reflecting it into the markup
            # would assert it again in the one place a reader would trust. The
            # custom dates ride along only when the range is actually in effect,
            # matching the disabled date inputs in the shell.
            resultsArgs = {
                "interval": interval,
                "groupBy": dashboard._getValidGroupBy(groupByParam, default=""),
                "startDate": customStart if interval == "custom" else "",
                "endDate": customEnd if interval == "custom" else "",
            }
            return render_template(
                "charts.html",
                username=username,
                section="charts",
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                groupBy=groupByParam,
                isSingleDayView=isSingleDayView,
                lastfmEnabled=lastfmEnabled,
                resultsUrl=url_for("chartsPage",
                                   **{k: v for k, v in resultsArgs.items() if v}),
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

        # The chart card as markup, with every series riding inside it as one
        # JSON data island (see _charts_results.html). What used to be fifteen
        # JSON keys is now three kinds of thing, each handled where it belongs:
        # the two headings' text and the Top Genres section's range-scoped
        # locked/unlocked body are rendered here rather than assembled by the
        # client; whether the artist-trend section exists at all is a {% if %}
        # rather than a style.display the client sets afterwards; and the nine
        # datasets - which htmx cannot swap, because they are drawn onto
        # canvases - stay data.
        return render_template(
            "_charts_results.html",
            intervalLabel=intervalLabel,
            lastDayDate=lastDayDate,
            artistTrend=artistTrend,
            lastfmEnabled=lastfmEnabled,
            genreUnlocked=genreUnlocked,
            genreCoverage=genreCoverage,
            #< exactly the window.__chartData charts.js reads - the client used
            #  to rebuild this object key by key from the envelope
            chartData={
                "interval": interval,
                "groupBy": groupBy,
                "timeSeries": timeSeries,
                "heatmap": heatmap,
                "artistTrend": artistTrend,
                "explicitRatio": explicitRatio,
                "decadeDistribution": decadeDistribution,
                "completionStats": completionStats,
                "mostSkippedSongs": mostSkippedSongs,
                "mostSkippedArtists": mostSkippedArtists,
                "genreDistribution": genreDistribution,
            },
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
            # "Show more" asks for the batch AFTER the rows already on screen and
            # appends it, so the URL carries an offset rather than a larger limit
            # - a grown limit would hit the MAX_DETAIL_HISTORY_PAGES ceiling above
            # and silently stop advancing. The batch it fetches renders this same
            # context, so the control it brings back builds its own next URL the
            # same way; there is no client-side offset bookkeeping left.
            showMoreArgs = dict(sharedArgs, offset=nextOffset)

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
                "showMoreUrl": dashboard._buildPageUrl(endpoint, 1, **showMoreArgs),
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

    def _missingEntityResponse(endpoint):
        """The answer for a detail URL whose entity no longer resolves.

        A plain GET redirects, as it always has. A second request must NOT, and
        for the same reason in both flavours: the client follows a 302
        transparently, lands on the top-list page's 200 HTML, and inlines it -
        htmx would swap a whole page into #detailBody, and the old fetch() passed
        resp.ok and then threw in resp.json(), so the visitor got "couldn't load"
        plus a Retry that behaved identically instead of being taken to the list.
        Reachable via a shared or bookmarked URL for an entity an overwrite
        import removed between the shell request and the body request.

        Each client gets the escape it understands natively: HX-Redirect + 204
        for htmx (204 rather than 404 because htmx swaps the body of any 2xx and
        reports a 4xx as an error - No Content leaves it nothing to inject and
        nothing to complain about), and the 404 + redirectUrl body for the one
        mode still on fetch(). Both shaped like unauthenticatedResponse's, so
        there is one convention for "go here instead"."""
        target = url_for(endpoint)
        if request.headers.get("HX-Request"):
            return Response(status=204, headers={"HX-Redirect": target})
        if request.args.get("ajax"):
            return jsonify(redirectUrl=target), 404
        return redirect(target)

    def _detailBodyUrl(endpoint, urlArgs, groupByParam, songDetail):
        """The URL the shell's placeholder loads the deferred body from.

        Built from the VALIDATED page state rather than echoed back from
        request.full_path - the same rule the pagination links follow, and the
        one historyPage and _topListShell already carry: junk is coerced for the
        query itself, so reflecting it into the markup would assert it again in
        the one place a reader would trust. ?groupBy=bogus is the visible case,
        because the Trend-buckets select printed beside this already renders as
        Auto for it.

        Only state that survives a reload rides along. offset/limit are "Show
        more" bookkeeping the address bar has never carried, so a reload has
        always started from the first batch and still does."""
        args = dict(urlArgs,
                    groupBy=dashboard._getValidGroupBy(groupByParam, default=""),
                    #< a junk or out-of-range page is clamped on the body request
                    #  (_calculatePagination) / turned into an offset for the song
                    #  page; this only keeps a shared ?page=3 working
                    page=_positivePageArg())
        sortOrder = dashboard._getHistorySortParam()
        if sortOrder == "oldest":
            args["sort"] = sortOrder
        if songDetail:
            #< the only opt-out worth carrying: the play log shows skips by default
            if request.args.get("skips", "").lower() == "false":
                args["skips"] = "false"
        elif dashboard._getDetailViewParam() == "history":
            args["view"] = "history"
        return url_for(endpoint, **{k: v for k, v in args.items() if v})

    @requiresUser
    def songDetailPage(username, db, track_id):

        song = db.getSong(track_id)
        if song is None:
            return _missingEntityResponse("topSongsPage")

        groupByParam = request.args.get("groupBy", "")   #< raw: the select keeps showing Auto
        # Four modes share this route, and the marker differs by kind. The one
        # that answers with DATA rather than markup keeps its query parameter;
        # the three htmx ones are told apart by which region they are filling
        # (see _detailSwapTarget and the DETAIL_*_TARGET constants).
        #
        # The bucket select re-fetches just the play-history series (see
        # static/js/detail-chart.js) - everything else on the page is
        # bucket-independent, so the full render below is skipped.
        if request.args.get("ajax") == "true":
            groupBy = dashboard._resolveGroupBy(
                groupByParam, *dashboard._playRangeSpanDates(username, db.tz, trackId=track_id))
            timeSeries = dashboard._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(trackId=track_id, groupBy=groupBy)
            )
            return jsonify(timeSeries=timeSeries, groupBy=groupBy)

        # Lightweight shell, the same two-phase load /charts, /genres, /history
        # and the three Top pages use: this GET renders the hero, the toolbar
        # and the tag panel - all off the one getSong above - and htmx fetches
        # everything below them right after first paint. Every query past this
        # point (the play log, the bucketed chart aggregates, the skip summary)
        # is work the first paint no longer waits on.
        swapTarget = _detailSwapTarget()
        if not swapTarget:
            return render_template(
                "song_detail.html",
                song=song,
                username=username,
                groupBy=groupByParam,
                entity_tags=db.repo.getTagsForEntity(username, "track", track_id),
                success=request.args.get("success"),
                error=request.args.get("error"),
                bodyUrl=_detailBodyUrl("songDetailPage", {"track_id": track_id},
                                       groupByParam, songDetail=True),
            )

        listCtx = _detailHistoryContext(db, "songDetailPage", {"track_id": track_id},
                                        groupByParam=groupByParam, trackId=track_id,
                                        trackDurationMs=song.get("duration"))
        # Both play-log swaps skip the chart/heatmap work above: "Show more"
        # replaces only the control at the end of the timeline (with the next
        # batch of rows plus the next control - see _play_log_batch.html), and
        # the sort/skips toggles and pagination links re-render the whole log.
        # Fragments, not a JSON envelope: htmx swaps the response body straight
        # in, so a {"resultsHtml": ...} wrapper would land as literal JSON text.
        if swapTarget == DETAIL_MORE_TARGET:
            return render_template("_play_log_batch.html", username=username, **listCtx)
        if swapTarget == DETAIL_HISTORY_TARGET:
            return render_template("_play_log.html", username=username, **listCtx)

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

        # The body as markup, with the two chart series riding inside it as a
        # JSON data island (see _detail_chart_data.html). They used to travel
        # beside the HTML in one JSON envelope; htmx swaps a response body, so
        # the HTML has to BE the response and the data has to come with it.
        return render_template(
            "_song_detail_body.html",
            song=song,
            username=username,
            skipStats=skipStats,
            chartData={"timeSeries": timeSeries, "heatmap": heatmap},
            **listCtx,
        )
    app.add_url_rule("/song/<track_id>", "songDetailPage", songDetailPage, methods=["GET"])

    def _entityDetailPage(username, db, entityId, *, kind, getter, missingEndpoint,
                          idKwarg, urlIdKwarg, shellTemplate, bodyTemplate,
                          embedEntity, fetchBio):
        """The artist/album detail route body - they were ~85-line twins whose
        six inline comments all read "see songDetailPage's identical branch"
        (the same reason the _topList* helpers above exist: a fix landing in
        one twin and missing the other). songDetailPage stays its own function:
        no songs list, a heatmap, the play-log template and a track duration.

        The shared skeleton, in order: bucket-only chart refetch (?ajax=true),
        the deferred-body shell (a plain GET), the play-log-only swap, then the
        full deferred body. `kind` is the tag/genre entity kind ("artist"/
        "album") and also names the endpoint (f"{kind}DetailPage" - the route
        registration convention below). `embedEntity`/`fetchBio` carry the two
        genuinely different steps; fetchBio(entity) returns the bio to display
        (or None), owning its own lazy-fetch + kill-switch wiring."""
        entity = getter(entityId)
        if entity is None:
            return _missingEntityResponse(missingEndpoint)

        spanKwargs = {idKwarg: entityId}
        groupByParam = request.args.get("groupBy", "")   #< raw: the select keeps showing Auto
        # The bucket select re-fetches just the play-history series (see
        # static/js/detail-chart.js) - everything else is bucket-independent.
        # The one mode that stays JSON: it answers with chart data, not markup.
        if request.args.get("ajax") == "true":
            groupBy = dashboard._resolveGroupBy(
                groupByParam, *dashboard._playRangeSpanDates(username, db.tz, **spanKwargs))
            timeSeries = dashboard._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(groupBy=groupBy, **spanKwargs)
            )
            return jsonify(timeSeries=timeSeries, groupBy=groupBy)

        # Deferred-body shell, the same two-phase load songDetailPage documents.
        # The artist page has the most to gain: the whole songs-by-this-artist
        # aggregate and the Last.fm biography fetch below happen only after the
        # page is already on screen.
        swapTarget = _detailSwapTarget()
        if not swapTarget:
            return render_template(
                shellTemplate,
                username=username,
                groupBy=groupByParam,
                entity_tags=db.repo.getTagsForEntity(username, kind, entityId),
                success=request.args.get("success"),
                error=request.args.get("error"),
                bodyUrl=_detailBodyUrl(f"{kind}DetailPage", {urlIdKwarg: entityId},
                                       groupByParam, songDetail=False),
                **{kind: entity},
            )

        listCtx = _detailHistoryContext(db, f"{kind}DetailPage", {urlIdKwarg: entityId, "view": "history"},
                                        groupByParam=groupByParam, **spanKwargs)
        listCtx["plays"] = dashboard._attachGenres(db, listCtx["plays"], "track")
        # The sort toggle / pagination links re-swap just the play log. These
        # pages page their history rather than growing it, so there is no
        # DETAIL_MORE_TARGET branch here - only songDetailPage has a "Show more".
        if swapTarget == DETAIL_HISTORY_TARGET:
            return render_template(
                "_detail_history_results.html", username=username,
                itemName=entity.get("name", ""), **listCtx)

        groupBy = dashboard._resolveGroupBy(
            groupByParam, *dashboard._playRangeSpanDates(username, db.tz, **spanKwargs))
        timeSeries = dashboard._embedTimeSeriesTextElements(
            db.getListeningTimeSeries(groupBy=groupBy, **spanKwargs)
        )

        songs = db.getSongsStats(sortBy="plays", **spanKwargs)
        firstSong = min(songs, key=lambda s: s.get("firstListenedAt") or float("inf")) if songs else None
        firstSongName = firstSong.get("name") if firstSong else None

        songs = dashboard._embedSongsTextElements(songs)
        songs = dashboard._embedTopSongsTextElements(
            songs, sortBy="plays", totalPlays=entity.get("plays", 0), totalMs=entity.get("totalTimeListened", 0)
        )
        songs = dashboard._attachGenres(db, songs, "track")
        entity = embedEntity(entity)
        entity = dashboard._attachGenres(db, [entity], kind)[0]

        entity["bio"] = fetchBio(entity)

        skipStats = db.getSkipStats(**spanKwargs)

        #< no heatmap key: these pages have no "When You Listen" canvas, and
        #  renderAllCharts skips a canvas that isn't there - see
        #  _detail_chart_data.html
        return render_template(
            bodyTemplate,
            songs=songs,
            firstSongName=firstSongName,
            username=username,
            skipStats=skipStats,
            view=dashboard._getDetailViewParam(),
            itemName=entity.get("name", ""),
            chartData={"timeSeries": timeSeries},
            **{kind: entity},
            **listCtx,
        )

    @requiresUser
    def artistDetailPage(username, db, artist_id):
        def fetchBio(artist):
            # lazyFetchArtistBio no-ops (and skips fetching) when the admin's
            # instance-wide toggle is off, same contract as the Last.fm genre
            # backfill kill switch - but the displayed bio is suppressed here
            # too, so disabling the feature also hides an artist's
            # already-fetched bio, not just new ones.
            db.lazyFetchArtistBio(artist_id, artist.get("name", ""))
            return db.getArtistBio(artist_id) if dashboard.repo.isArtistBioEnabled() else None

        return _entityDetailPage(
            username, db, artist_id,
            kind="artist", getter=db.getArtist, missingEndpoint="topArtistsPage",
            idKwarg="artistId", urlIdKwarg="artist_id",
            shellTemplate="artist_detail.html", bodyTemplate="_artist_detail_body.html",
            embedEntity=dashboard._embedArtistTextElement, fetchBio=fetchBio)
    app.add_url_rule("/artist/<artist_id>", "artistDetailPage", artistDetailPage, methods=["GET"])

    @requiresUser
    def albumDetailPage(username, db, album_id):
        def fetchBio(album):
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
            return db.getAlbumBio(album_id) if dashboard.repo.isAlbumBioEnabled() else None

        return _entityDetailPage(
            username, db, album_id,
            kind="album", getter=db.getAlbum, missingEndpoint="topAlbumsPage",
            idKwarg="albumId", urlIdKwarg="album_id",
            shellTemplate="album_detail.html", bodyTemplate="_album_detail_body.html",
            embedEntity=dashboard._embedAlbumTextElements, fetchBio=fetchBio)
    app.add_url_rule("/album/<album_id>", "albumDetailPage", albumDetailPage, methods=["GET"])
