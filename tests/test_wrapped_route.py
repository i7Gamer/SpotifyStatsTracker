import datetime
import json
import re
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: like the other route test files, this file deliberately does NOT swap
# Database modules for MagicMocks in sys.modules - it only exercises the route
# with a per-test mock db (via get_user_db).
import app as appModule
from app import SpotifyDashboardApp
from _app_factory import AppTestCase
import Database.utils as utilsModule
from conftest import wrappedCachedRow


def _ts(year, month=6, day=1, hour=12):
    """Unix timestamp (seconds) for a UTC datetime, matching test_chart_stats.py."""
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp()


def _wrappedBootstrap(body):
    """The parsed JSON from wrapped.html's <script id="wrapped-bootstrap"> data
    island - the server-rendered config/timeSeries that static/js/wrapped.js reads."""
    m = re.search(r'<script type="application/json" id="wrapped-bootstrap">(.*?)</script>', body, re.S)
    assert m, "wrapped-bootstrap data island not found in rendered page"
    return json.loads(m.group(1))


def _song(trackId, name, plays, firstListenedAt):
    return {
        "id": trackId, "name": name, "url": "u", "imageId": "i", "duration": 0,
        "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
        "album": {"id": "alb1", "name": "Album", "url": "u", "imageId": "i", "imageUrl": "",
                   "totalTracks": 1, "releaseDate": 0},
        "artists": [], "plays": plays, "totalTimeListened": plays * 1000,
        "firstListenedAt": firstListenedAt,
    }


def _artist(artistId, name, plays, firstListenedAt):
    return {
        "id": artistId, "name": name, "url": "u", "imageUrl": "", "imageId": "i",
        "plays": plays, "totalTimeListened": plays * 1000, "uniqueSongCount": 1,
        "firstListenedAt": firstListenedAt,
    }


def _album(albumId, name, plays, firstListenedAt):
    return {
        "id": albumId, "name": name, "url": "u", "imageId": "i", "imageUrl": "",
        "totalTracks": 1, "releaseDate": 0, "artists": [],
        "plays": plays, "totalTimeListened": plays * 1000, "uniqueSongCount": 1,
        "firstListenedAt": firstListenedAt,
    }


def _bucket(label, totalTimeListened=0, plays=0):
    """A getListeningTimeSeries-shaped bucket - the cached time_series_*
    columns' element shape (see Database.database.getListeningTimeSeries)."""
    return {"label": label, "totalTimeListened": totalTimeListened, "plays": plays, "skips": 0}


class _WrappedRouteTestBase(AppTestCase):
    """All tests fix the app's timezone to UTC (matching test_chart_stats.py) and
    freeze `now()` to 2026-07-11, so year math is deterministic."""

    def setUp(self):
        tzPatcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        tzPatcher.start()
        self.addCleanup(tzPatcher.stop)

        nowPatcher = patch.object(appModule, "now",
                                   return_value=datetime.datetime(2026, 7, 11, tzinfo=datetime.timezone.utc))
        nowPatcher.start()
        self.addCleanup(nowPatcher.stop)

    def _makeDb(self, earliestPlayedAt=None, cachedRow=None):
        """cachedRow feeds db.repo.getCachedWrapped - the only source
        _buildWrappedContext reads lists/totals from since R6 (2026-09-02)
        made the cache the only Wrapped path. Defaults to an all-empty row
        (see wrappedCachedRow) when a test has nothing to seed."""
        db = MagicMock()
        db.getEntriesFromOld.return_value = (
            [{"id": "x", "playedAt": earliestPlayedAt, "timePlayed": 1}] if earliestPlayedAt is not None else []
        )
        db.repo.getCachedWrapped.return_value = cachedRow if cachedRow is not None else wrappedCachedRow()
        return db

    def _getWrapped(self, dash, db, query="", headers=None):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/wrapped{query}", headers=headers or {})


class TestWrappedSwapCardsStayAuthenticated(_WrappedRouteTestBase):
    """Counterpart to the public share page's swapped cards: the owner's own
    htmx swaps must keep the session-authorized image prefix and the internal
    detail links, i.e. the public-view card options must not leak into this
    path."""

    def test_swapped_list_html_keeps_the_username_image_route_and_detail_links(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2023),
                          cachedRow=wrappedCachedRow(topSongs=[
                              _song("song1", "Song", plays=5, firstListenedAt=_ts(2026, 3))]))

        body = self._getWrapped(dash, db, headers={"HX-Request": "true"}).get_data(as_text=True)

        self.assertIn('src="/img/alice/tracks/i.jpeg"', body)
        self.assertIn('href="/song/song1"', body)


class TestWrappedYearSelection(_WrappedRouteTestBase):
    def test_the_data_island_carries_only_the_chart_series(self):
        """It used to also carry fetchUrl/isPublicView/shareOwnerName/year/
        groupBy/limit/sortBy, all of them for the hand-written fetch loader:
        htmx builds the request from the form's own attributes now, and every
        "who is this page about" string is server-rendered. What is left is
        the one thing a <canvas> genuinely cannot get from the markup."""
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2023))

        resp = self._getWrapped(dash, db)
        bootstrap = _wrappedBootstrap(resp.data.decode())

        self.assertEqual(list(bootstrap), ["timeSeries"])

    def test_defaults_to_current_year(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2023))

        resp = self._getWrapped(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"2026", resp.data)
        db.repo.getCachedWrapped.assert_called_with(db.user, 2026)

    def test_badges_list_every_year_with_data_most_recent_first(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2023))

        resp = self._getWrapped(dash, db)

        body = resp.data.decode()
        positions = [body.index(f"/wrapped?year={y}") for y in (2026, 2025, 2024, 2023)]
        self.assertEqual(positions, sorted(positions))   #< appear in that (descending) order

    def test_explicit_valid_year_is_used(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2023))

        self._getWrapped(dash, db, query="?year=2024")

        db.repo.getCachedWrapped.assert_called_with(db.user, 2024)

    def test_out_of_range_year_falls_back_to_current_year(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2023))

        resp = self._getWrapped(dash, db, query="?year=1999")

        self.assertEqual(resp.status_code, 200)
        db.repo.getCachedWrapped.assert_called_with(db.user, 2026)

    def test_non_numeric_year_survives_and_falls_back(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2023))

        resp = self._getWrapped(dash, db, query="?year=abc")

        self.assertEqual(resp.status_code, 200)
        db.repo.getCachedWrapped.assert_called_with(db.user, 2026)

    def test_no_history_still_renders_current_year_only(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=None)

        resp = self._getWrapped(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"/wrapped?year=2026", resp.data)
        self.assertNotIn(b"/wrapped?year=2025", resp.data)


class TestWrappedSuccessErrorMessagesAreEscaped(_WrappedRouteTestBase):
    """?success=/?error= are attacker-controlled query params (e.g. a crafted
    link to /wrapped?success=...) - they must be HTML-escaped like every other
    template's error/success message, not rendered with `| safe`."""

    def test_success_message_html_is_escaped_not_executed(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getWrapped(dash, db, query="?success=<script>alert(1)</script>")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"<script>alert(1)</script>", resp.data)
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt;", resp.data)

    def test_error_message_html_is_escaped(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getWrapped(dash, db, query="?error=<script>alert(1)</script>")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"<script>alert(1)</script>", resp.data)
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt;", resp.data)


class TestWrappedTotals(_WrappedRouteTestBase):
    def test_totals_come_from_the_cached_row(self):
        dash = self._makeApp()
        db = self._makeDb(cachedRow=wrappedCachedRow(totalPlays=42, totalMs=999000))

        resp = self._getWrapped(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'<p class="summary-value">42</p>', resp.data)

    def test_top_songs_artists_albums_are_capped_by_the_default_limit(self):
        """The cached pool (up to 100 items) is sliced in Python by the
        request's limit, not re-queried with a SQL LIMIT - built here as 15
        items > WRAPPED_LIST_SIZE (10) so the default cap is visible in what
        survives."""
        dash = self._makeApp()
        songs = [_song(f"s{i}", f"Song {i}", plays=i, firstListenedAt=0) for i in range(1, 16)]
        artists = [_artist(f"a{i}", f"Artist {i}", plays=i, firstListenedAt=0) for i in range(1, 16)]
        albums = [_album(f"al{i}", f"Album {i}", plays=i, firstListenedAt=0) for i in range(1, 16)]
        db = self._makeDb(cachedRow=wrappedCachedRow(topSongs=songs, topArtists=artists, topAlbums=albums))

        resp = self._getWrapped(dash, db)

        body = resp.data.decode()
        self.assertIn("Song 15", body)       #< highest play count, must survive the default cap
        self.assertNotIn("Song 1<", body)    #< lowest play count, must be cut by the default cap
        self.assertIn("Artist 15", body)
        self.assertNotIn("Artist 1<", body)
        self.assertIn("Album 15", body)
        self.assertNotIn("Album 1<", body)


class TestWrappedGroupBy(_WrappedRouteTestBase):
    def test_month_groupby_is_passed_through_and_selected(self):
        dash = self._makeApp()
        db = self._makeDb(cachedRow=wrappedCachedRow(timeSeriesMonth=[_bucket("MONTH_BUCKET")]))

        resp = self._getWrapped(dash, db, query="?groupBy=month")

        bootstrap = _wrappedBootstrap(resp.data.decode())
        self.assertEqual(bootstrap["timeSeries"][0]["label"], "MONTH_BUCKET")
        self.assertIn(b'<option value="month" selected>Month</option>', resp.data)

    def test_invalid_groupby_falls_back_to_week(self):
        dash = self._makeApp()
        db = self._makeDb(cachedRow=wrappedCachedRow(timeSeriesWeek=[_bucket("WEEK_BUCKET")]))

        resp = self._getWrapped(dash, db, query="?groupBy=nonsense")

        bootstrap = _wrappedBootstrap(resp.data.decode())
        self.assertEqual(bootstrap["timeSeries"][0]["label"], "WEEK_BUCKET")


class TestWrappedLimit(_WrappedRouteTestBase):
    def test_limit_param_is_honored_across_top_lists_and_discoveries(self):
        dash = self._makeApp()
        songs = [_song(f"s{i}", f"Song {i}", plays=i, firstListenedAt=0) for i in range(1, 30)]
        artists = [_artist(f"a{i}", f"Artist {i}", plays=i, firstListenedAt=0) for i in range(1, 30)]
        albums = [_album(f"al{i}", f"Album {i}", plays=i, firstListenedAt=0) for i in range(1, 30)]
        discoveries = [_song(f"d{i}", f"Discovery {i}", plays=i, firstListenedAt=0) for i in range(1, 30)]
        db = self._makeDb(cachedRow=wrappedCachedRow(topSongs=songs, topArtists=artists, topAlbums=albums,
                                                      discoveredSongs=discoveries))

        resp = self._getWrapped(dash, db, query="?limit=25")

        body = resp.data.decode()
        self.assertIn("Song 29", body)
        self.assertNotIn("Song 1<", body)
        self.assertIn("Artist 29", body)
        self.assertNotIn("Artist 1<", body)
        self.assertIn("Album 29", body)
        self.assertNotIn("Album 1<", body)
        self.assertIn("Discovery 29", body)
        self.assertNotIn("Discovery 1<", body)

    def test_invalid_limit_falls_back_to_default(self):
        dash = self._makeApp()
        songs = [_song(f"s{i}", f"Song {i}", plays=i, firstListenedAt=0) for i in range(1, 16)]
        db = self._makeDb(cachedRow=wrappedCachedRow(topSongs=songs))

        resp = self._getWrapped(dash, db, query="?limit=999")

        body = resp.data.decode()
        self.assertIn("Song 15", body)      #< within the default (WRAPPED_LIST_SIZE=10) cap
        self.assertNotIn("Song 1<", body)   #< would only survive an (invalid) limit=999

    def test_discoveries_cap_follows_the_limit_param(self):
        dash = self._makeApp()
        songs = [_song(f"s{i}", f"Discovery {i}", plays=i, firstListenedAt=0) for i in range(1, 30)]
        db = self._makeDb(cachedRow=wrappedCachedRow(discoveredSongs=songs))

        resp = self._getWrapped(dash, db, query="?limit=25")

        body = resp.data.decode()
        self.assertIn("Discovery 29", body)   #< highest play count, must survive a 25-item cap
        self.assertNotIn("Discovery 1<", body)  #< lowest play count, must be cut by a 25-item cap


class TestWrappedSortBy(_WrappedRouteTestBase):
    def test_sort_by_param_is_passed_through_to_top_lists(self):
        """sortBy re-sorts the cached pool in Python (see _resortByMetric) -
        a low-play, long-duration item must be promoted ahead of a high-play,
        short one under sortBy=totalTimeListened."""
        dash = self._makeApp()
        manyPlaysShortTime = _song("many", "ManyPlaysShortTime", plays=10, firstListenedAt=0)
        fewPlaysLongTime = _song("long", "FewPlaysLongTime", plays=2, firstListenedAt=0)
        fewPlaysLongTime["totalTimeListened"] = 999999
        db = self._makeDb(cachedRow=wrappedCachedRow(topSongs=[manyPlaysShortTime, fewPlaysLongTime]))

        resp = self._getWrapped(dash, db, query="?sortBy=totalTimeListened")

        body = resp.data.decode()
        # Scoped to the results list itself: the export button (outside
        # #wrappedResults) carries "ManyPlaysShortTime" in its data-topsong
        # regardless of sortBy (it's always the plays-ranked item, see
        # TestWrappedExportTopItems) - unscoped, that would appear before the
        # sortBy-ordered list and break this ordering check.
        resultsStart = body.index('id="wrappedResults"')
        self.assertLess(body.index("FewPlaysLongTime", resultsStart), body.index("ManyPlaysShortTime", resultsStart))

    def test_default_sort_by_is_plays(self):
        dash = self._makeApp()
        lowPlaysFirst = _song("low", "LowPlaysFirst", plays=1, firstListenedAt=0)
        highPlaysSecond = _song("high", "HighPlaysSecond", plays=99, firstListenedAt=0)
        # Cached pool deliberately NOT plays-sorted, to prove the default resorts it.
        db = self._makeDb(cachedRow=wrappedCachedRow(topSongs=[lowPlaysFirst, highPlaysSecond]))

        resp = self._getWrapped(dash, db)

        body = resp.data.decode()
        self.assertLess(body.index("HighPlaysSecond"), body.index("LowPlaysFirst"))

    def test_invalid_sort_by_falls_back_to_plays(self):
        dash = self._makeApp()
        lowPlaysFirst = _song("low", "LowPlaysFirst", plays=1, firstListenedAt=0)
        highPlaysSecond = _song("high", "HighPlaysSecond", plays=99, firstListenedAt=0)
        db = self._makeDb(cachedRow=wrappedCachedRow(topSongs=[lowPlaysFirst, highPlaysSecond]))

        resp = self._getWrapped(dash, db, query="?sortBy=bogus")

        body = resp.data.decode()
        self.assertLess(body.index("HighPlaysSecond"), body.index("LowPlaysFirst"))

    def test_discoveries_are_ranked_by_the_chosen_sort_by(self):
        """Discoveries default to most-played first, but a totalTimeListened
        sort must be able to promote a low-play, long-duration discovery
        ahead of a high-play, short one."""
        dash = self._makeApp()
        manyShort = _song("many", "ManyShortPlays", plays=10, firstListenedAt=0)
        fewLong = _song("long", "FewLongPlays", plays=2, firstListenedAt=0)
        fewLong["totalTimeListened"] = 999999
        db = self._makeDb(cachedRow=wrappedCachedRow(discoveredSongs=[manyShort, fewLong]))

        resp = self._getWrapped(dash, db, query="?sortBy=totalTimeListened")

        body = resp.data.decode()
        self.assertLess(body.index("FewLongPlays"), body.index("ManyShortPlays"))

    def test_sort_by_dropdown_renders_and_preselects(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getWrapped(dash, db, query="?sortBy=name")

        self.assertIn(b'id="sortBy"', resp.data)
        self.assertIn(b'<option value="name" selected>Name (A-Z)</option>', resp.data)

    def test_sort_by_dropdown_defaults_to_plays(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getWrapped(dash, db)

        self.assertIn(b'<option value="plays" selected>Number of Plays</option>', resp.data)


class TestWrappedExportTopItems(_WrappedRouteTestBase):
    """UT-5 (review 2026-09-02): the export button's data-topsong/
    data-topalbum/data-topartist are drawn from _buildWrappedContext's
    exportTopSong/exportTopAlbum/exportTopArtist, which must stay the
    most-PLAYED song/album/artist regardless of ?sortBy - not whatever
    sortBy happened to rank first in the on-screen list (that list is what
    topSongs[0]/topAlbums[0]/topArtists[0] used to feed the template with).
    A sortBy=name request whose alphabetically-first item is NOT the most
    played pins the distinction. Since R6 (2026-09-02) the cached path
    computes this by re-sorting the SAME cached pool by "plays" - no second
    live query, unlike the deleted dynamic-calculation branch."""

    def test_export_top_song_is_most_played_not_sort_order_first(self):
        dash = self._makeApp()
        alphaFirst = _song("alpha", "Aardvark Song", plays=1, firstListenedAt=0)
        mostPlayed = _song("loud", "Zebra Anthem", plays=99, firstListenedAt=0)
        db = self._makeDb(cachedRow=wrappedCachedRow(topSongs=[alphaFirst, mostPlayed]))

        resp = self._getWrapped(dash, db, query="?sortBy=name")

        body = resp.data.decode()
        # sortBy=name still shows the alphabetical list on the page itself...
        self.assertIn("Aardvark Song", body)
        # ...but the export card's hidden data must carry the top-PLAYED song.
        self.assertIn('data-topsong="Zebra Anthem"', body)
        self.assertNotIn('data-topsong="Aardvark Song"', body)

    def test_export_top_album_is_most_played_not_sort_order_first(self):
        dash = self._makeApp()
        alphaFirst = _album("alpha", "Aardvark Album", plays=1, firstListenedAt=0)
        mostPlayed = _album("loud", "Zebra Album", plays=99, firstListenedAt=0)
        db = self._makeDb(cachedRow=wrappedCachedRow(topAlbums=[alphaFirst, mostPlayed]))

        resp = self._getWrapped(dash, db, query="?sortBy=name")

        body = resp.data.decode()
        self.assertIn('data-topalbum="Zebra Album"', body)
        self.assertNotIn('data-topalbum="Aardvark Album"', body)

    def test_export_top_song_matches_the_list_when_sort_by_is_plays(self):
        """No bug when sortBy is already plays (the default) - the on-screen
        top item and the exported one must agree, and this must not cost a
        second cache read (the cached path derives it from the SAME pool)."""
        dash = self._makeApp()
        db = self._makeDb(cachedRow=wrappedCachedRow(
            topSongs=[_song("s1", "Only Song", plays=10, firstListenedAt=0)]))

        resp = self._getWrapped(dash, db)

        self.assertIn('data-topsong="Only Song"', resp.data.decode())
        db.repo.getCachedWrapped.assert_called_once()

    def test_export_top_artist_is_most_played_not_sort_order_first(self):
        dash = self._makeApp()
        alphaFirst = _artist("alpha", "Aardvark Artist", plays=1, firstListenedAt=0)
        mostPlayed = _artist("loud", "Zebra Artist", plays=99, firstListenedAt=0)
        db = self._makeDb(cachedRow=wrappedCachedRow(topArtists=[alphaFirst, mostPlayed]))

        resp = self._getWrapped(dash, db, query="?sortBy=name")

        body = resp.data.decode()
        self.assertIn('data-topartist="Zebra Artist"', body)
        self.assertNotIn('data-topartist="Aardvark Artist"', body)

    def test_export_top_artist_matches_the_list_when_sort_by_is_plays(self):
        dash = self._makeApp()
        db = self._makeDb(cachedRow=wrappedCachedRow(
            topArtists=[_artist("a1", "Only Artist", plays=10, firstListenedAt=0)]))

        resp = self._getWrapped(dash, db)

        self.assertIn('data-topartist="Only Artist"', resp.data.decode())
        db.repo.getCachedWrapped.assert_called_once()


class TestWrappedDiscoveries(_WrappedRouteTestBase):
    """Which entries first-listened-in-year-X count as discoveries is decided
    when the cache is WRITTEN (Database/workers/wrapped_worker.py's
    _calculateAndSaveWrapped, via getSongsStats(firstListenedStart=...,
    firstListenedEnd=...)), not by _buildWrappedContext - since R6
    (2026-09-02) the cached path trusts discovered_*_list as already
    year-scoped and only re-sorts/caps it (see test_wrapped_cache.py for the
    cache-write-side year-filtering coverage). What is still this layer's
    job, and what these tests cover, is that the cached discovery lists
    render and are capped/sorted like the top lists."""

    def test_discovered_songs_and_artists_from_the_cache_render(self):
        dash = self._makeApp()
        db = self._makeDb(cachedRow=wrappedCachedRow(
            discoveredSongs=[_song("new1", "New Song", plays=5, firstListenedAt=_ts(2026, 3))],
            discoveredArtists=[_artist("newA", "New Artist", plays=5, firstListenedAt=_ts(2026, 3))],
        ))

        resp = self._getWrapped(dash, db)

        self.assertIn(b"New Song", resp.data)
        self.assertIn(b"New Artist", resp.data)

    def test_discovered_albums_from_the_cache_render(self):
        dash = self._makeApp()
        db = self._makeDb(cachedRow=wrappedCachedRow(
            discoveredAlbums=[_album("newAlb", "New Album", plays=5, firstListenedAt=_ts(2026, 3))]))

        resp = self._getWrapped(dash, db)

        self.assertIn(b"New Album", resp.data)

    def test_an_empty_discoveries_list_renders_nothing(self):
        dash = self._makeApp()
        db = self._makeDb()   #< default cached row's discovered_*_list are all "[]"

        resp = self._getWrapped(dash, db)

        self.assertEqual(resp.status_code, 200)

    def test_discoveries_are_capped_and_sorted_by_plays(self):
        dash = self._makeApp()
        songs = [_song(f"s{i}", f"Discovery {i}", plays=i, firstListenedAt=_ts(2026, 3)) for i in range(1, 15)]
        db = self._makeDb(cachedRow=wrappedCachedRow(discoveredSongs=songs))

        resp = self._getWrapped(dash, db)

        body = resp.data.decode()
        self.assertIn("Discovery 14", body)     #< highest play count, must survive the cap
        self.assertNotIn("Discovery 1<", body)  #< lowest play count, must be cut by the cap


if __name__ == "__main__":
    unittest.main()
