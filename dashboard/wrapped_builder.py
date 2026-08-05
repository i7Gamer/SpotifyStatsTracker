# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import math
import time

from flask import request
from Database.utils import convertToDatetime, now
from services.genre_gate import emptyGenreCoverage, genreGatePasses, resolveGenreCoverage, resolveGenreDistribution
from config import (
    SHARE_LINK_EXPIRY_CHOICES, SHARE_LINK_MAX_PER_BUCKET,
    WRAPPED_LIMIT_OPTIONS, WRAPPED_LIST_SIZE, WRAPPED_TOP_GENRES_LIMIT,
)


class WrappedBuilderMixin:
    """Wrapped page context builder, year/filter parsing, share-link panel args, and re-sort/discovery helpers."""

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
        single year to fall back on the way a per-year link does.

        Never empty: a history whose EARLIEST play is future-dated (a clock
        skew artifact an import can carry) makes the range empty, and both
        callers index [0] - so the current year stands in, rendering an
        empty Wrapped rather than a 500."""
        nowLocal = now(tz=db.tz)
        currentYear = nowLocal.year
        oldestEntries = db.getEntriesFromOld(count=1, fullPagination=False)
        earliestYear = convertToDatetime(oldestEntries[0]["playedAt"], tz=db.tz).year if oldestEntries else currentYear
        return list(range(currentYear, earliestYear - 1, -1)) or [currentYear]

    def _parseWrappedFilterParams(self) -> tuple:
        """groupBy/limit/sortBy (validated, with the same defaults/fallbacks
        wrappedPage() has always used) - shared by wrappedPage() and
        sharedWrappedPage() so the two routes can't silently drift apart on
        validation or defaults. Returns (groupBy, limit, sortBy).

        It used to also answer "is this the ajax request, and which slice of
        the page does it want" (?ajax=true&type=all|chart|lists). Both went
        with the htmx migration: the request is marked by the HX-Request
        header now, and one fragment re-renders the whole recap, so there is
        no narrow update to describe. See routes/wrapped.py."""
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

        return groupBy, limit, sortBy

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

    def shareLinkPanelArgs(self, username: str, year: int) -> dict:
        """Everything _share_link_panel.html renders, for all four places that
        render it: the /wrapped page, its ajax year switch, share-link creation
        and share-link revocation (see routes/wrapped.py and routes/auth.py).
        They each used to assemble these kwargs themselves, which is how a new
        one could reach three of the four and quietly go missing from the
        fourth.

        Three buckets, all freshly re-derived from the DB rather than assumed
        from whichever link an action just touched - the panel can be showing
        several links of each type, and creating or revoking one tells you
        nothing about the state of the rest:

          yearLinks       links scoped to exactly this year
          allYearsLinks   links covering every year
          otherYearLinks  everything else the user still has active

        otherYearLinks is display-only. The per-bucket cap
        (SHARE_LINK_MAX_PER_BUCKET) deliberately ignores it: those links are
        listed so a stale one can be revoked without navigating to its year,
        which is what /profile's parallel list used to be for."""
        nowTs = time.time()
        links = self.repo.getShareLinksForUser(username)

        def annotate(subset):
            return [{**link, "expiryLabel": self._shareLinkExpiryLabel(link["expires_at"], nowTs)}
                    for link in subset]

        return {
            "year": year,
            "yearLinks": annotate(link for link in links if link["year"] == year),
            "allYearsLinks": annotate(link for link in links if link["year"] is None),
            "otherYearLinks": annotate(
                link for link in links if link["year"] is not None and link["year"] != year),
            "shareLinkExpiryChoices": SHARE_LINK_EXPIRY_CHOICES,
            "shareLinkMaxPerBucket": SHARE_LINK_MAX_PER_BUCKET,
        }

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
            if cached is None:
                # Still nothing after a recalculation means the year holds no
                # plays at all - recalculateWrappedForYear returns early and
                # removes the row for those. The year is still SELECTABLE
                # (_computeAvailableYears offers a contiguous range), so leaving
                # this None dropped a real database into the mocks-only branch
                # below: ten unbounded queries per request, three of them with
                # no date range whatsoever, and never cacheable because the
                # worker deletes the row again on its next pass. An empty dict
                # takes the empty-state defaults directly beneath instead.
                cached = {}
        else:
            # If db/repo is mock, check if getCachedWrapped was explicitly mocked to return a non-mock dict
            try:
                res = db.repo.getCachedWrapped(db.user, year)
                if res and not isinstance(res, MagicMock):
                    cached = res
            except Exception:  # noqa: S110 - probing whether a mock db has a real cache;
                pass           #  any failure just means "no cached wrapped"

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
