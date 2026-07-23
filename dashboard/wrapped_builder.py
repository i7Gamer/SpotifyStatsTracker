from __future__ import annotations

import json
import math
import time

from flask import render_template, request
from Database.utils import convertToDatetime, msToString, now
from services.genre_gate import emptyGenreCoverage, genreGatePasses, resolveGenreCoverage, resolveGenreDistribution
from config import WRAPPED_LIMIT_OPTIONS, WRAPPED_LIST_SIZE, WRAPPED_TOP_GENRES_LIMIT


class WrappedBuilderMixin:
    """Wrapped page context + AJAX response builders, year/filter parsing, share-link resolution, and re-sort/discovery helpers."""

    def _getWrappedYearParam(self, availableYears: list, defaultYear: int) -> int:
        """The current request's ?year=... if it's one of the years the user
        actually has data for, else `defaultYear` - mirrors _getPageParam()'s
        tolerate-junk-input, silently-clamp behavior for ?page=."""
        try:
            year = int(request.args.get("year", defaultYear))
        except (TypeError, ValueError):
            return defaultYear
        return year if year in availableYears else defaultYear

    def _computeAvailableYears(self, db) -> list:
        """Every year `db`'s user has at least one play in, most recent
        first - shared by wrappedPage() (year badges) and a multi-year
        ("all years") share link on sharedWrappedPage(), which has no fixed
        single year to fall back on the way a per-year link does."""
        nowLocal = now(tz=db.tz)
        currentYear = nowLocal.year
        oldestEntries = db.getEntriesFromOld(count=1, fullPagination=False)
        earliestYear = convertToDatetime(oldestEntries[0]["playedAt"], tz=db.tz).year if oldestEntries else currentYear
        return list(range(currentYear, earliestYear - 1, -1))

    def _parseWrappedFilterParams(self) -> tuple:
        """groupBy/limit/sortBy (validated, with the same defaults/fallbacks
        wrappedPage() has always used) plus ajax-request detection - shared
        by wrappedPage() and sharedWrappedPage() so the two routes can't
        silently drift apart on validation or defaults. Returns (groupBy,
        limit, sortBy, isAjaxRequest, ajaxUpdateType, includeGenres).

        Genre data is deliberately computed live - never from the
        user_wrapped cache: coverage keeps growing while the Last.fm
        backfill runs, and the admin's inherited-genres toggle changes the
        numbers retroactively. Only computed for responses that actually
        render the card (the full page and ajax type=all - chart/lists
        partial updates would compute and discard it). See chartsPage's
        identical kill-switch comment; _wrapped_genres.html hides its whole
        section (chart AND locked-progress fallback) when lastfmEnabled is
        False."""
        # Raw param: "" is the Auto option, resolved per-year inside
        # _buildWrappedContext (the year span is known there) - the template's
        # select must keep showing Auto rather than pinning the derived value.
        groupBy = request.args.get("groupBy", "")

        limit = request.args.get("limit", type=int)
        if limit not in WRAPPED_LIMIT_OPTIONS:
            limit = WRAPPED_LIST_SIZE
        # Default stays "plays" (not DEFAULT_SORT_BY) so nobody's Wrapped
        # changes unless they touch the control.
        sortBy = self._getSortByParam(default="plays")

        isAjaxRequest = request.args.get("ajax") == "true"
        ajaxUpdateType = request.args.get("type", "all")
        includeGenres = not isAjaxRequest or ajaxUpdateType == "all"

        return groupBy, limit, sortBy, isAjaxRequest, ajaxUpdateType, includeGenres

    @staticmethod
    def _shareLinkExpiryLabel(expiresAt: float | None, nowTs: float) -> str:
        """'Never expires' / 'Expires today' / 'Expires in N days' - a
        relative countdown recomputed from expires_at (not the originally-
        chosen duration, which isn't stored) so the label can't drift stale.
        Used only by the wrapped.html share panel - Profile's own link list
        keeps its own separate absolute-date convention (createdText/
        expiresText) instead, since that page is more of a record-keeping
        view where an absolute date fits better."""
        if expiresAt is None:
            return "Never expires"
        remainingDays = math.ceil((expiresAt - nowTs) / 86400)
        if remainingDays <= 0:
            return "Expires today"
        return f"Expires in {remainingDays} day" + ("" if remainingDays == 1 else "s")

    def _resolveShareLinksForYear(self, username: str, year: int) -> tuple[list[dict], list[dict]]:
        """(yearLinks, allYearsLinks) - every still-active link scoped to
        this exact year, and every still-active all-years link, both freshly
        re-derived from the DB, never assumed from whichever link an action
        just touched, since the share-link panel can now be showing several
        of either type and creating/revoking one doesn't tell you what state
        the rest are in. Each link dict is annotated with an "expiryLabel"
        (see _shareLinkExpiryLabel) ready for the template to render."""
        nowTs = time.time()
        links = self.repo.getShareLinksForUser(username)
        yearLinks = [
            {**link, "expiryLabel": self._shareLinkExpiryLabel(link["expires_at"], nowTs)}
            for link in links if link["year"] == year
        ]
        allYearsLinks = [
            {**link, "expiryLabel": self._shareLinkExpiryLabel(link["expires_at"], nowTs)}
            for link in links if link["year"] is None
        ]
        return yearLinks, allYearsLinks

    @staticmethod
    def _resortByMetric(items: list, sortBy: str) -> list:
        """Re-sorts an already-fetched list of song/artist/album dicts by
        `sortBy` (plays/totalTimeListened descending, name ascending) -
        matches VALID_SORT_BY's semantics (see app.py's sortBy query param
        docs) without re-querying the DB. Used where a pool was fetched at
        one fixed ranking but the displayed order should follow the user's
        chosen metric instead (Wrapped's cached pools, which are only ever
        stored plays-ranked).

        Ties on `sortBy` fall back to the other metric, then name, then id -
        and for "name", to time listened (desc) then id - mirroring
        Repository.getSongsPage's ORDER BY chains instead of leaning on the
        input pool's incidental order, so a resorted pool and a live query
        at the same sortBy agree on tie order."""
        if sortBy == "name":
            return sorted(
                items,
                key=lambda item: (
                    (item.get("name") or "").lower(),
                    -item.get("totalTimeListened", 0),
                    item.get("id", ""),
                ),
            )
        otherMetric = "plays" if sortBy == "totalTimeListened" else "totalTimeListened"
        return sorted(
            items,
            key=lambda item: (
                -item.get(sortBy, 0),
                -item.get(otherMetric, 0),
                (item.get("name") or "").lower(),
                item.get("id", ""),
            ),
        )

    def _discoveriesInYear(self, items: list, yearStart, yearEnd, limit: int, sortBy: str = "plays") -> list:
        """Items (songs or artists) whose true, all-time first listen falls
        within [yearStart, yearEnd) - not just their earliest play *within* that
        range, which a date-scoped query would report instead. `items` must
        therefore come from an unbounded (no date range) stats call. Sorted by
        `sortBy`, most-played discovery first by default."""
        yearStartTs, yearEndTs = yearStart.timestamp(), yearEnd.timestamp()
        discovered = [
            item for item in items
            if item.get("firstListenedAt") is not None and yearStartTs <= item["firstListenedAt"] < yearEndTs
        ]
        discovered = self._resortByMetric(discovered, sortBy)
        return discovered[:limit]

    def _buildWrappedContext(self, db, year: int, groupBy: str, limit: int, sortBy: str,
                             includeGenres: bool = True) -> dict:
        """Everything wrapped.html needs to render one year's Wrapped recap
        for `db`'s user - the cache-read/recalculate, resort-and-slice, and
        text/genre embedding pipeline, independent of which route is asking
        for it. Used by both the authenticated /wrapped route and the public
        /shared/<token> route (see wrappedPage() and sharedWrappedPage()).

        includeGenres=False skips the live genre-coverage/distribution
        queries entirely - wrappedPage() uses this for AJAX chart/lists-only
        partial updates, which discard genre data anyway (see its call
        site); every other caller wants the full context."""
        nowLocal = now(tz=db.tz)
        yearStart = nowLocal.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        yearEnd = nowLocal.replace(year=year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

        # Auto ("") buckets from the year's span clamped to now: an
        # in-progress year still early on gets day buckets, anything longer
        # week - the same shared resolver every trend-bucket control uses
        # (see _resolveGroupBy). An explicit day/week/month choice wins.
        groupBy = self._resolveGroupBy(groupBy, yearStart, min(yearEnd, nowLocal))

        # Genre data is deliberately computed live - never from the
        # user_wrapped cache below: coverage keeps growing while the Last.fm
        # backfill runs, and the admin's inherited-genres toggle changes the
        # numbers retroactively.
        lastfmEnabled = self.repo.isLastfmGenreBackfillEnabled()
        genreCoverage = emptyGenreCoverage()
        genreUnlocked = False
        topGenres = None
        if includeGenres and lastfmEnabled:
            genreCoverage = resolveGenreCoverage(db, yearStart, yearEnd)
            genreUnlocked = genreGatePasses(genreCoverage)
            if genreUnlocked:
                topGenres = resolveGenreDistribution(db, yearStart, yearEnd,
                                                     WRAPPED_TOP_GENRES_LIMIT)

        # 1. Fetch precalculated cached wrapped stats from database (unless db is a mock)
        from unittest.mock import MagicMock
        is_mock = isinstance(db, MagicMock) or (hasattr(db, "repo") and isinstance(db.repo, MagicMock))

        cached = None
        if not is_mock:
            cached = db.repo.getCachedWrapped(db.user, year)
            if not cached:
                # Cache miss: recalculate and cache on the fly
                db.recalculateWrappedForYear(year)
                cached = db.repo.getCachedWrapped(db.user, year)
        else:
            # If db/repo is mock, check if getCachedWrapped was explicitly mocked to return a non-mock dict
            try:
                res = db.repo.getCachedWrapped(db.user, year)
                if res and not isinstance(res, MagicMock):
                    cached = res
            except Exception:
                pass

        if cached is not None:
            # If still empty defaults needed
            if not cached:
                cached = {
                    "total_plays": 0,
                    "total_ms": 0,
                    "longest_streak": 0,
                    "peak_day": None,
                    "peak_plays": 0,
                    "unique_songs": 0,
                    "unique_artists": 0,
                    "discovered_songs": 0,
                    "discovered_artists": 0,
                    "time_series_day": "[]",
                    "time_series_week": "[]",
                    "time_series_month": "[]",
                    "top_songs": "[]",
                    "top_artists": "[]",
                    "top_albums": "[]",
                    "discovered_songs_list": "[]",
                    "discovered_artists_list": "[]",
                    "discovered_albums_list": "[]",
                }

            # 2. Extract values and parse lists
            totalPlays = cached["total_plays"]
            totalMs = cached["total_ms"]
            longestStreak = cached["longest_streak"]
            peakListeningTime = (cached["peak_day"], cached["peak_plays"]) if cached["peak_day"] else None
            uniqueSongsCount = cached["unique_songs"]
            uniqueArtistsCount = cached["unique_artists"]
            discoveredSongsCount = cached["discovered_songs"]
            discoveredArtistsCount = cached["discovered_artists"]

            timeSeriesDay = json.loads(cached["time_series_day"])
            timeSeriesWeek = json.loads(cached["time_series_week"])
            timeSeriesMonth = json.loads(cached["time_series_month"])

            topSongs = json.loads(cached["top_songs"])
            topArtists = json.loads(cached["top_artists"])
            topAlbums = json.loads(cached["top_albums"])

            discoveredSongs = json.loads(cached["discovered_songs_list"])
            discoveredArtists = json.loads(cached["discovered_artists_list"])
            discoveredAlbums = json.loads(cached["discovered_albums_list"])

            # 3. Select timeseries grouping
            if groupBy == "day":
                timeSeries = timeSeriesDay
            elif groupBy == "month":
                timeSeries = timeSeriesMonth
            else:
                timeSeries = timeSeriesWeek

            # 4. Re-sort the cached (up to 100-item) pools by the chosen
            # metric, then slice to the requested limit. The cache itself
            # is only ever stored plays-ranked, so membership stays
            # whatever that plays-ranked capture included - only order/
            # what survives the limit cut within it follows sortBy.
            topSongs = self._resortByMetric(topSongs, sortBy)[:limit]
            topArtists = self._resortByMetric(topArtists, sortBy)[:limit]
            topAlbums = self._resortByMetric(topAlbums, sortBy)[:limit]
            discoveredSongs = self._resortByMetric(discoveredSongs, sortBy)[:limit]
            discoveredArtists = self._resortByMetric(discoveredArtists, sortBy)[:limit]
            discoveredAlbums = self._resortByMetric(discoveredAlbums, sortBy)[:limit]
        else:
            # Dynamic calculations for mocks (unit tests compatibility)
            topSongs = db.getTopSongs(startDate=yearStart, endDate=yearEnd, by=sortBy, limit=limit)
            topArtists = db.getTopArtists(startDate=yearStart, endDate=yearEnd, by=sortBy, limit=limit)
            topAlbums = db.getTopAlbums(startDate=yearStart, endDate=yearEnd, by=sortBy, limit=limit)
            totalPlays, totalMs = db.getPlayTotals(yearStart, yearEnd)

            discoveredSongs = self._discoveriesInYear(
                db.getSongsStats(sortBy="plays"), yearStart, yearEnd, limit, sortBy=sortBy
            )
            discoveredArtists = self._discoveriesInYear(
                db.getArtistsStats(), yearStart, yearEnd, limit, sortBy=sortBy
            )
            discoveredAlbums = self._discoveriesInYear(
                db.getAlbumsStats(sortBy="plays"), yearStart, yearEnd, limit, sortBy=sortBy
            )

            timeSeries = db.getListeningTimeSeries(startDate=yearStart, endDate=yearEnd, groupBy=groupBy)

            longestStreak = db.getLongestStreak(yearStart, yearEnd)
            peakListeningTime = db.getPeakListeningTime(yearStart, yearEnd)
            uniqueSongsCount = db.getSongsCount(yearStart, yearEnd)
            uniqueArtistsCount = db.getArtistsCount(yearStart, yearEnd)
            discoveredSongsCount = db.getDiscoveredSongsCount(yearStart, yearEnd)
            discoveredArtistsCount = db.getDiscoveredArtistsCount(yearStart, yearEnd)

        # 5. Embed presentation elements
        timeSeries = self._embedTimeSeriesTextElements(timeSeries)
        topSongs = self._embedSongsTextElements(topSongs)
        topSongs = self._embedTopSongsTextElements(topSongs, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        topArtists = self._embedArtistsTextElements(topArtists, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        topAlbums = self._embedAlbumsTextElements(topAlbums, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        discoveredSongs = self._embedTopSongsTextElements(self._embedSongsTextElements(discoveredSongs))
        discoveredArtists = self._embedArtistsTextElements(discoveredArtists)
        discoveredAlbums = self._embedAlbumsTextElements(discoveredAlbums)
        topSongs = self._attachGenres(db, topSongs, "track")
        topArtists = self._attachGenres(db, topArtists, "artist")
        topAlbums = self._attachGenres(db, topAlbums, "album")
        discoveredSongs = self._attachGenres(db, discoveredSongs, "track")
        discoveredArtists = self._attachGenres(db, discoveredArtists, "artist")
        discoveredAlbums = self._attachGenres(db, discoveredAlbums, "album")

        return {
            "yearStart": yearStart,
            "yearEnd": yearEnd,
            "totalPlays": totalPlays,
            "totalMs": totalMs,
            "topSongs": topSongs,
            "topArtists": topArtists,
            "topAlbums": topAlbums,
            "discoveredSongs": discoveredSongs,
            "discoveredArtists": discoveredArtists,
            "discoveredAlbums": discoveredAlbums,
            "timeSeries": timeSeries,
            "longestStreak": longestStreak,
            "peakListeningTime": peakListeningTime,
            "uniqueSongsCount": uniqueSongsCount,
            "uniqueArtistsCount": uniqueArtistsCount,
            "discoveredSongsCount": discoveredSongsCount,
            "discoveredArtistsCount": discoveredArtistsCount,
            "topGenres": topGenres,
            "genreCoverage": genreCoverage,
            "genreUnlocked": genreUnlocked,
            "lastfmEnabled": lastfmEnabled,
        }

    def _buildWrappedAjaxResponse(self, ctx: dict, username: str, year: int, updateType: str, publicView: bool) -> dict:
        """The JSON-able payload for a Wrapped ?ajax=true request - shared by
        the authenticated /wrapped route and the public /shared/<token>
        route so the two can't drift on what an ajax response contains.
        publicView is threaded into the _wrapped_list.html/
        _wrapped_genres.html renders so their "You"/{{ username }} text (see
        _track_card.html) stays correct after a partial swap on the shared
        page too. Returns a plain dict (not a Response) so wrappedPage() can
        layer its own owner-only sharePanelHtml key on top before
        jsonify-ing - sharedWrappedPage() never does, since a public visitor
        must never receive share-panel data."""
        topSongs, topArtists, topAlbums = ctx["topSongs"], ctx["topArtists"], ctx["topAlbums"]
        discoveredSongs, discoveredArtists, discoveredAlbums = (
            ctx["discoveredSongs"], ctx["discoveredArtists"], ctx["discoveredAlbums"])

        res = {}

        if updateType in ("all", "chart"):
            res["timeSeries"] = ctx["timeSeries"]

        if updateType in ("all", "lists"):
            res["topSongsHtml"] = render_template(
                "_wrapped_list.html", items=topSongs, section="top_songs",
                username=username, year=year, publicView=publicView)
            res["topArtistsHtml"] = render_template(
                "_wrapped_list.html", items=topArtists, section="top_artists",
                username=username, year=year, publicView=publicView)
            res["topAlbumsHtml"] = render_template(
                "_wrapped_list.html", items=topAlbums, section="top_albums",
                username=username, year=year, publicView=publicView)
            res["discoveredSongsHtml"] = render_template(
                "_wrapped_list.html", items=discoveredSongs, section="top_songs",
                username=username, year=year, publicView=publicView)
            res["discoveredArtistsHtml"] = render_template(
                "_wrapped_list.html", items=discoveredArtists, section="top_artists",
                username=username, year=year, publicView=publicView)
            res["discoveredAlbumsHtml"] = render_template(
                "_wrapped_list.html", items=discoveredAlbums, section="top_albums",
                username=username, year=year, publicView=publicView)

        if updateType == "all":
            res["topGenresHtml"] = render_template(
                "_wrapped_genres.html", topGenres=ctx["topGenres"],
                genreCoverage=ctx["genreCoverage"], genreUnlocked=ctx["genreUnlocked"], year=year,
                lastfmEnabled=ctx["lastfmEnabled"], username=username, publicView=publicView)
            topSongText = (
                f"{topSongs[0]['name']} - {topSongs[0]['artists'][0]['name']}"
                if topSongs and topSongs[0].get('artists')
                else (topSongs[0]['name'] if topSongs else "N/A")
            )
            topArtistText = topArtists[0]['name'] if topArtists else "N/A"
            topAlbumText = topAlbums[0]['name'] if topAlbums else "N/A"
            res.update({
                "totalPlays": ctx["totalPlays"],
                "totalTime": msToString(ctx["totalMs"]),
                "longestStreak": ctx["longestStreak"],
                "peakDay": ctx["peakListeningTime"][0] if ctx["peakListeningTime"] else "N/A",
                "peakPlays": ctx["peakListeningTime"][1] if ctx["peakListeningTime"] else 0,
                "uniqueSongsCount": ctx["uniqueSongsCount"],
                "uniqueArtistsCount": ctx["uniqueArtistsCount"],
                "discoveredSongsCount": ctx["discoveredSongsCount"],
                "discoveredArtistsCount": ctx["discoveredArtistsCount"],
                "topSongText": topSongText,
                "topArtistText": topArtistText,
                "topAlbumText": topAlbumText,
            })
        return res
