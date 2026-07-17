import datetime
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
import Database.utils as utilsModule


def _ts(year, month=6, day=1, hour=12):
    """Unix timestamp (seconds) for a UTC datetime, matching test_chart_stats.py."""
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp()


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


class _WrappedRouteTestBase(unittest.TestCase):
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

    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _makeDb(self, earliestPlayedAt=None):
        db = MagicMock()
        db.getEntriesFromOld.return_value = (
            [{"id": "x", "playedAt": earliestPlayedAt, "timePlayed": 1}] if earliestPlayedAt is not None else []
        )
        db.getTopSongs.return_value = []
        db.getTopArtists.return_value = []
        db.getTopAlbums.return_value = []
        db.getPlayTotals.return_value = (0, 0)
        db.getSongsStats.return_value = []
        db.getArtistsStats.return_value = []
        db.getAlbumsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        return db

    def _getWrapped(self, dash, db, query=""):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/wrapped{query}")


class TestWrappedYearSelection(_WrappedRouteTestBase):
    def test_defaults_to_current_year(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2023))

        resp = self._getWrapped(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"2026", resp.data)
        kwargs = db.getTopSongs.call_args.kwargs
        self.assertEqual(kwargs["startDate"].year, 2026)
        self.assertEqual(kwargs["endDate"].year, 2027)

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

        resp = self._getWrapped(dash, db, query="?year=2024")

        kwargs = db.getTopSongs.call_args.kwargs
        self.assertEqual(kwargs["startDate"].year, 2024)
        self.assertEqual(kwargs["endDate"].year, 2025)

    def test_out_of_range_year_falls_back_to_current_year(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2023))

        resp = self._getWrapped(dash, db, query="?year=1999")

        self.assertEqual(resp.status_code, 200)
        kwargs = db.getTopSongs.call_args.kwargs
        self.assertEqual(kwargs["startDate"].year, 2026)

    def test_non_numeric_year_survives_and_falls_back(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2023))

        resp = self._getWrapped(dash, db, query="?year=abc")

        self.assertEqual(resp.status_code, 200)
        kwargs = db.getTopSongs.call_args.kwargs
        self.assertEqual(kwargs["startDate"].year, 2026)

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
    def test_totals_come_from_get_play_totals(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getPlayTotals.return_value = (42, 999000)

        resp = self._getWrapped(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getPlayTotals.assert_called_once()
        self.assertIn(b'<p class="summary-value">42</p>', resp.data)

    def test_top_songs_artists_albums_are_capped_and_year_scoped(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._getWrapped(dash, db)

        songKwargs = db.getTopSongs.call_args.kwargs
        self.assertEqual(songKwargs["limit"], appModule.WRAPPED_LIST_SIZE)
        albumKwargs = db.getTopAlbums.call_args.kwargs
        self.assertEqual(albumKwargs["limit"], appModule.WRAPPED_LIST_SIZE)
        artistKwargs = db.getTopArtists.call_args.kwargs
        self.assertEqual(artistKwargs["startDate"].year, 2026)
        # Artists must be capped via SQL LIMIT like songs/albums, not fetched
        # unbounded and sliced in Python afterward.
        self.assertEqual(artistKwargs["limit"], appModule.WRAPPED_LIST_SIZE)


class TestWrappedGroupBy(_WrappedRouteTestBase):
    def test_month_groupby_is_passed_through_and_selected(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getWrapped(dash, db, query="?groupBy=month")

        kwargs = db.getListeningTimeSeries.call_args.kwargs
        self.assertEqual(kwargs["groupBy"], "month")
        self.assertIn(b'<option value="month" selected>Month</option>', resp.data)

    def test_invalid_groupby_falls_back_to_week(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._getWrapped(dash, db, query="?groupBy=nonsense")

        kwargs = db.getListeningTimeSeries.call_args.kwargs
        self.assertEqual(kwargs["groupBy"], "week")


class TestWrappedLimit(_WrappedRouteTestBase):
    def test_limit_param_is_honored_across_top_lists_and_discoveries(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._getWrapped(dash, db, query="?limit=25")

        self.assertEqual(db.getTopSongs.call_args.kwargs["limit"], 25)
        self.assertEqual(db.getTopArtists.call_args.kwargs["limit"], 25)
        self.assertEqual(db.getTopAlbums.call_args.kwargs["limit"], 25)

    def test_invalid_limit_falls_back_to_default(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._getWrapped(dash, db, query="?limit=999")

        self.assertEqual(db.getTopSongs.call_args.kwargs["limit"], appModule.WRAPPED_LIST_SIZE)

    def test_discoveries_cap_follows_the_limit_param(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2024))
        songs = [_song(f"s{i}", f"Discovery {i}", plays=i, firstListenedAt=_ts(2026, 3)) for i in range(1, 30)]
        db.getSongsStats.return_value = songs

        resp = self._getWrapped(dash, db, query="?limit=25")

        body = resp.data.decode()
        self.assertIn("Discovery 29", body)   #< highest play count, must survive a 25-item cap
        self.assertNotIn("Discovery 1<", body)  #< lowest play count, must be cut by a 25-item cap


class TestWrappedSortBy(_WrappedRouteTestBase):
    def test_sort_by_param_is_passed_through_to_top_lists(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._getWrapped(dash, db, query="?sortBy=totalTimeListened")

        self.assertEqual(db.getTopSongs.call_args.kwargs["by"], "totalTimeListened")
        self.assertEqual(db.getTopArtists.call_args.kwargs["by"], "totalTimeListened")
        self.assertEqual(db.getTopAlbums.call_args.kwargs["by"], "totalTimeListened")

    def test_default_sort_by_is_plays(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._getWrapped(dash, db)

        self.assertEqual(db.getTopSongs.call_args.kwargs["by"], "plays")

    def test_invalid_sort_by_falls_back_to_plays(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._getWrapped(dash, db, query="?sortBy=bogus")

        self.assertEqual(db.getTopSongs.call_args.kwargs["by"], "plays")

    def test_discoveries_are_ranked_by_the_chosen_sort_by(self):
        """Discoveries default to most-played first, but a totalTimeListened
        sort must be able to promote a low-play, long-duration discovery
        ahead of a high-play, short one."""
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2024))
        manyShort = _song("many", "ManyShortPlays", plays=10, firstListenedAt=_ts(2026, 3))
        fewLong = _song("long", "FewLongPlays", plays=2, firstListenedAt=_ts(2026, 3))
        fewLong["totalTimeListened"] = 999999
        db.getSongsStats.return_value = [manyShort, fewLong]

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


class TestWrappedDiscoveries(_WrappedRouteTestBase):
    def test_only_items_first_listened_in_the_selected_year_are_discoveries(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2024))
        db.getSongsStats.return_value = [
            _song("new1", "New Song", plays=5, firstListenedAt=_ts(2026, 3)),
            _song("old1", "Old Favorite", plays=20, firstListenedAt=_ts(2024, 1)),
        ]
        db.getArtistsStats.return_value = [
            _artist("newA", "New Artist", plays=5, firstListenedAt=_ts(2026, 3)),
            _artist("oldA", "Old Artist", plays=20, firstListenedAt=_ts(2024, 1)),
        ]

        resp = self._getWrapped(dash, db)

        self.assertIn(b"New Song", resp.data)
        self.assertNotIn(b"Old Favorite", resp.data)
        self.assertIn(b"New Artist", resp.data)
        self.assertNotIn(b"Old Artist", resp.data)

    def test_discoveries_are_scoped_to_the_selected_year_not_current_year(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2024))
        db.getSongsStats.return_value = [
            _song("s2025", "Discovered 2025", plays=5, firstListenedAt=_ts(2025, 6)),
            _song("s2026", "Discovered 2026", plays=5, firstListenedAt=_ts(2026, 3)),
        ]

        resp = self._getWrapped(dash, db, query="?year=2025")

        self.assertIn(b"Discovered 2025", resp.data)
        self.assertNotIn(b"Discovered 2026", resp.data)

    def test_item_with_no_first_listened_at_is_never_a_discovery(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2024))
        db.getSongsStats.return_value = [_song("noDate", "Unknown Origin", plays=5, firstListenedAt=None)]

        resp = self._getWrapped(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Unknown Origin", resp.data)

    def test_only_albums_first_listened_in_the_selected_year_are_discoveries(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2024))
        db.getAlbumsStats.return_value = [
            _album("newAlb", "New Album", plays=5, firstListenedAt=_ts(2026, 3)),
            _album("oldAlb", "Old Album", plays=20, firstListenedAt=_ts(2024, 1)),
        ]

        resp = self._getWrapped(dash, db)

        self.assertIn(b"New Album", resp.data)
        self.assertNotIn(b"Old Album", resp.data)

    def test_discoveries_are_capped_and_sorted_by_plays(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2024))
        songs = [_song(f"s{i}", f"Discovery {i}", plays=i, firstListenedAt=_ts(2026, 3)) for i in range(1, 15)]
        db.getSongsStats.return_value = songs

        resp = self._getWrapped(dash, db)

        body = resp.data.decode()
        self.assertIn("Discovery 14", body)   #< highest play count, must survive the cap
        self.assertNotIn("Discovery 1<", body)  #< lowest play count, must be cut by the cap


if __name__ == "__main__":
    unittest.main()
