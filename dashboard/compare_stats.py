from __future__ import annotations

from Database.utils import msToString
from services.taste_match import _rankById, _sharedRankScore
from config import COMPARE_OVERLAP_POOL_SIZE, COMPARE_SHARED_POOL_SIZE, COMPARE_TOP_LIST_SIZE, WEEKDAY_NAMES


class CompareStatsMixin:
    """Compare-page per-user stat gathering and shared/common-item building."""

    def _gatherCompareStats(self, db, startDate, endDate, limit=COMPARE_TOP_LIST_SIZE, sortBy="plays") -> dict:
        """One Compare-page side's stats, gathered identically for the viewer
        and the counterpart so the two columns can't drift apart. Runs the
        same _embed*TextElements step every other page feeding
        _track_card.html uses - without it the cards render with blank
        time/first-listened/duration/percent lines.

        Every category is fetched ONCE, at COMPARE_SHARED_POOL_SIZE depth
        (sharedSongsPool/sharedArtistsPool/sharedAlbumsPool) - the query
        that feeds Top Common Songs/Artists/Albums (_buildSharedItems) and
        the similarity counts. topSongsPool/topArtistsPool/topAlbumsPool
        (what taste-match runs over) are DERIVED as that same pool's first
        COMPARE_OVERLAP_POOL_SIZE entries rather than a second DB query: a
        plays-ranked LIMIT 200 query's first 100 rows are, by construction,
        identical to a dedicated LIMIT 100 query (same WHERE/ORDER BY) - so
        there's no need to pay for the full GROUP BY/ORDER BY aggregation
        (the expensive part on a many-year "All Time" range) twice just to
        get two different cutoffs of the same ranking. This also means
        widening the shared-item search can never move the taste-match
        score - it only ever sees the first COMPARE_OVERLAP_POOL_SIZE of
        whatever the shared pool returns, unaffected by anything beyond it.

        The DISPLAYED my/their top lists default to the same pool's first
        `limit` entries (no extra query); other sortBys re-shape them per
        displayList below ("name" alphabetizes that same head, a metric
        re-queries live)."""
        def displayList(pool, queryAtSortBy):
            """The my/their column for one category. "plays" keeps the
            plays-ranked pool's own head (no extra query). "name" means
            "your top `limit` BY PLAYS, shown A-Z for scanning" -
            deliberately NOT the alphabetical head of the whole history the
            paginated standalone pages show: capped at `limit` with no
            pagination here, that would surface mostly number/punctuation-
            prefixed obscurities instead of anything about taste. Any other
            metric re-queries live so membership AND order reflect it -
            that genuinely can't be derived by slicing a plays-ranked
            pool."""
            if sortBy == "plays":
                return pool[:limit]
            if sortBy == "name":
                return self._resortByMetric(pool[:limit], "name")
            return queryAtSortBy()

        totalPlays, totalMs = db.getPlayTotals(startDate, endDate)
        sharedSongsPool = db.getTopSongs(startDate, endDate, limit=COMPARE_SHARED_POOL_SIZE)
        topSongsPool = sharedSongsPool[:COMPARE_OVERLAP_POOL_SIZE]
        topSongsDisplay = displayList(
            topSongsPool, lambda: db.getTopSongs(startDate, endDate, limit=limit, by=sortBy))
        topSongs = self._embedTopSongsTextElements(
            self._embedSongsTextElements(topSongsDisplay),
            sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        sharedAlbumsPool = db.getTopAlbums(startDate, endDate, limit=COMPARE_SHARED_POOL_SIZE)
        topAlbumsPool = sharedAlbumsPool[:COMPARE_OVERLAP_POOL_SIZE]
        topAlbumsDisplay = displayList(
            topAlbumsPool, lambda: db.getTopAlbums(startDate, endDate, limit=limit, by=sortBy))
        topAlbums = self._embedAlbumsTextElements(
            topAlbumsDisplay,
            sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        sharedArtistsPool = db.getTopArtists(startDate, endDate, limit=COMPARE_SHARED_POOL_SIZE)
        topArtistsPool = sharedArtistsPool[:COMPARE_OVERLAP_POOL_SIZE]
        topArtistsDisplay = displayList(
            topArtistsPool, lambda: db.getTopArtists(startDate, endDate, limit=limit, by=sortBy))
        topArtists = self._embedArtistsTextElements(
            topArtistsDisplay,
            sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        topSongs = self._attachGenres(db, topSongs, "track")
        topArtists = self._attachGenres(db, topArtists, "artist")
        topAlbums = self._attachGenres(db, topAlbums, "album")

        completion = db.getCompletionStats(startDate, endDate)
        completionTotal = completion["skips"] + completion["completes"] + completion["partials"]
        explicitRatio = db.getExplicitRatio(startDate, endDate)
        explicitTotal = explicitRatio["explicit"] + explicitRatio["clean"]
        heatmap = db.getHourOfDayHeatmap(startDate, endDate)
        hourTotals = [sum(day[hour]["totalTimeListened"] for day in heatmap) for hour in range(24)]
        dayTotals = [sum(cell["totalTimeListened"] for cell in day) for day in heatmap]

        return {
            "totalPlays": totalPlays,
            "totalMs": totalMs,
            "totalTimeText": msToString(totalMs),
            "topSongs": topSongs,
            "topArtists": topArtists,
            "topAlbums": topAlbums,
            "topSongsPool": topSongsPool,
            "topArtistsPool": topArtistsPool,
            "topAlbumsPool": topAlbumsPool,
            "sharedSongsPool": sharedSongsPool,
            "sharedArtistsPool": sharedArtistsPool,
            "sharedAlbumsPool": sharedAlbumsPool,
            "uniqueSongs": db.getSongsCount(startDate, endDate),
            "uniqueArtists": db.getArtistsCount(startDate, endDate),
            "avgPlayTimeText": msToString(totalMs // totalPlays) if totalPlays else "—",
            "skipRateText": f"{completion['skips'] / completionTotal * 100:.0f}%" if completionTotal else "—",
            "explicitShareText": f"{explicitRatio['explicit'] / explicitTotal * 100:.0f}%" if explicitTotal else "—",
            "peakHourText": f"{hourTotals.index(max(hourTotals)):02d}:00" if any(hourTotals) else "—",
            "peakDayText": WEEKDAY_NAMES[dayTotals.index(max(dayTotals))] if any(dayTotals) else "—",
        }

    def _buildSharedItems(self, myPool, theirPool, embedFn, limit) -> list[dict]:
        """Shared entries of one category, ranked by the SUM of both users'
        rank discounts (see _sharedRankScore) - not either side's raw
        combined totals, and independent of the page's sortBy control
        (which only reorders the individual my/their lists, see
        _gatherCompareStats) - and sliced to `limit`, with the per-user
        versus data the Top Common Songs/Artists/Albums cards render.
        Rank-weighted so one user's #1 with a decent counterpart rank still
        outranks an item both users only rank moderately even when the
        moderate item's combined plays are higher - but summed rather than
        taste-match's min() shape, so an item the counterpart barely plays
        can't claim the top "common" spot (see _sharedRankScore).

        Rank is derived by re-sorting each pool by plays (see
        _resortByMetric) rather than trusting the incoming pool's own order -
        ranking by the viewer's own order used to silently cut different
        overlapping items depending on whose pool was walked (see
        test_shared_artists_ranked_by_combined_plays_not_the_viewers_own_order).
        Combined ranking - not either side's own pool order - so the same
        mutual-share pair sees the same Top Common list regardless of who's
        viewing.
        Copied dicts: the pool entries also feed the viewer's own top-list
        column, and the versus block / combined totals must only show on the
        shared cards. The unique-song counts are only attached where the
        aggregates carry them (artists/albums) - a song card has nothing to
        count."""
        myRanks = _rankById(self._resortByMetric(myPool, "plays"))
        theirRanks = _rankById(self._resortByMetric(theirPool, "plays"))
        theirById = {item["id"]: item for item in theirPool}
        sharedItems = [dict(item) for item in myPool if item["id"] in theirById]

        def sortKey(item):
            theirItem = theirById[item["id"]]
            sharedScore = _sharedRankScore(myRanks[item["id"]], theirRanks[item["id"]])
            combinedPlays = item.get("plays", 0) + theirItem.get("plays", 0)
            combinedTime = item.get("totalTimeListened", 0) + theirItem.get("totalTimeListened", 0)
            #< descending sharedScore/combinedPlays/combinedTime via negation,
            #  ascending name/id - the same plays -> totalTimeListened ->
            #  name -> id tiebreak chain the rank maps above were sorted by
            #  (_resortByMetric), which in turn mirrors Repository's
            #  plays-ranked ORDER BY, so ties render the same way here as
            #  everywhere else "plays" is ranked.
            return (-sharedScore, -combinedPlays, -combinedTime, (item.get("name") or "").lower(), item["id"])

        sharedItems.sort(key=sortKey)
        shared = embedFn(sharedItems[:limit])
        for item in shared:
            theirItem = theirById[item["id"]]
            myPlays = item.get("plays", 0)
            myMs = item.get("totalTimeListened", 0)
            theirMs = theirItem.get("totalTimeListened", 0)
            combinedMs = myMs + theirMs
            compareData = {
                "myPlays": myPlays,
                "theirPlays": theirItem.get("plays", 0),
                #< each side's own plays-rank - the versus block shows them
                #  because the list order is rank-driven (_sharedRankScore),
                #  and without them the order looks arbitrary whenever it
                #  disagrees with raw combined plays.
                "myRank": myRanks[item["id"]],
                "theirRank": theirRanks[item["id"]],
                "myTimeText": msToString(myMs),
                "theirTimeText": msToString(theirMs),
                #< an even split when neither side has recorded time - a
                #  bar of two zero-width halves would just look broken
                "myTimePercent": round(myMs / combinedMs * 100) if combinedMs else 50,
            }
            if "uniqueSongCount" in item or "uniqueSongCount" in theirItem:
                compareData["myUniqueSongs"] = item.get("uniqueSongCount", 0)
                compareData["theirUniqueSongs"] = theirItem.get("uniqueSongCount", 0)
            item["compareData"] = compareData
            # The card's top stat line shows the COMBINED totals - the
            # per-user numbers live in the versus block right below it.
            # Overwritten after embedFn so the embedded text matches.
            item["plays"] = myPlays + compareData["theirPlays"]
            item["totalTimeListened"] = combinedMs
            item["totalTimeListenedText"] = msToString(combinedMs)
        return shared
