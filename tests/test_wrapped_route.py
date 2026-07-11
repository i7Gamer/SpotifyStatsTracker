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
