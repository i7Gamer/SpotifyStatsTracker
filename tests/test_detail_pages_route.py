import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: like test_dashboard_pagination.py / test_top_albums_route.py, this file
# deliberately does NOT swap Database modules for MagicMocks in sys.modules -
# it only exercises the routes with a per-test mock db (via get_user_db).
from app import SpotifyDashboardApp


class _DetailRouteTestBase(unittest.TestCase):
    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _getPath(self, dash, db, path):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(path)


class TestSongDetailRoute(_DetailRouteTestBase):
    def _song(self):
        return {
            "id": "t1", "name": "Song One", "url": "http://example.com/t1",
            "imageId": "alb1", "duration": 200000, "explicit": False, "isrc": "",
            "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album One", "url": "http://example.com/alb1",
                      "imageId": "alb1", "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "artists": [{"id": "a1", "name": "Artist A", "url": "u", "imageUrl": "", "imageId": "a1"}],
            "plays": 5, "totalTimeListened": 50000, "firstListenedAt": 100,
        }

    def test_known_song_renders(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Song One", resp.data)
        db.getSong.assert_called_once_with("t1")
        db.getListeningTimeSeries.assert_called_once()
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("trackId"), "t1")
        self.assertEqual(db.getHourOfDayHeatmap.call_args.kwargs.get("trackId"), "t1")

    def test_unknown_song_redirects_to_top_songs(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = None

        resp = self._getPath(dash, db, "/song/missing")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/top-songs", resp.headers["Location"])

    def test_month_groupby_is_passed_through_and_selected(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1?groupBy=month")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")
        self.assertIn(b'<option value="month" selected>Month</option>', resp.data)

    def test_invalid_groupby_falls_back_to_week(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        self._getPath(dash, db, "/song/t1?groupBy=nonsense")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "week")


class TestArtistDetailRoute(_DetailRouteTestBase):
    def _artist(self):
        return {"id": "a1", "name": "Artist A", "url": "http://example.com/a1", "imageUrl": "",
                "imageId": "a1", "plays": 5, "totalTimeListened": 50000, "uniqueSongCount": 2,
                "firstListenedAt": 100}

    def _song(self, trackId, name, firstListenedAt):
        return {
            "id": trackId, "name": name, "url": "u", "imageId": "alb1",
            "duration": 200000, "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1,
            "releaseDate": 0, "album": {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                                        "imageUrl": "", "totalTracks": 2, "releaseDate": 0},
            "artists": [], "plays": 3, "totalTimeListened": 30000, "firstListenedAt": firstListenedAt,
        }

    def test_known_artist_renders_with_their_songs(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Artist A", resp.data)
        db.getArtist.assert_called_once_with("a1")
        self.assertEqual(db.getSongsStats.call_args.kwargs.get("artistId"), "a1")
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("artistId"), "a1")

    def test_unknown_artist_redirects_to_top_artists(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = None

        resp = self._getPath(dash, db, "/artist/missing")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/top-artists", resp.headers["Location"])

    def test_month_groupby_is_passed_through_and_selected(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1?groupBy=month")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")
        self.assertIn(b'<option value="month" selected>Month</option>', resp.data)

    def test_first_song_you_listened_to_is_shown(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getSongsStats.return_value = [
            self._song("t1", "Later Song", firstListenedAt=200),
            self._song("t2", "Earliest Song", firstListenedAt=100),
        ]
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(b"First Song You Listened To", resp.data)
        self.assertIn(b"Earliest Song", resp.data)

    def test_unique_song_count_card_is_shown(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(b"Unique Songs Listened", resp.data)
        self.assertIn(b'<p class="summary-value">2</p>', resp.data)


class TestAlbumDetailRoute(_DetailRouteTestBase):
    def _album(self):
        return {"id": "alb1", "name": "Album One", "url": "http://example.com/alb1", "imageId": "alb1",
                "imageUrl": "", "totalTracks": 2, "releaseDate": 0, "artists": [],
                "plays": 5, "totalTimeListened": 50000, "uniqueSongCount": 2, "firstListenedAt": 100}

    def _song(self, trackId, firstListenedAt):
        return {
            "id": trackId, "name": f"Song {trackId}", "url": "u", "imageId": "alb1",
            "duration": 200000, "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1,
            "releaseDate": 0, "album": {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                                        "imageUrl": "", "totalTracks": 2, "releaseDate": 0},
            "artists": [], "plays": 3, "totalTimeListened": 30000, "firstListenedAt": firstListenedAt,
        }

    def test_known_album_renders_with_its_songs(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = [self._song("t1", 200), self._song("t2", 100)]
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Album One", resp.data)
        db.getAlbum.assert_called_once_with("alb1")
        self.assertEqual(db.getSongsStats.call_args.kwargs.get("albumId"), "alb1")
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("albumId"), "alb1")

    def test_unknown_album_redirects_to_top_albums(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = None

        resp = self._getPath(dash, db, "/album/missing")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/top-albums", resp.headers["Location"])

    def test_month_groupby_is_passed_through_and_selected(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1?groupBy=month")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")
        self.assertIn(b'<option value="month" selected>Month</option>', resp.data)

    def test_unique_song_count_card_is_shown(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn(b"Unique Songs Listened", resp.data)
        self.assertIn(b'<p class="summary-value">2</p>', resp.data)


if __name__ == "__main__":
    unittest.main()
