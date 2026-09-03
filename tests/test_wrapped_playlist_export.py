"""The Wrapped Top 100 playlist download - routes/tags.py's playlistExport()
`year` branch, which reuses dashboard._buildWrappedContext (same cached
top-100 pool wrapped.html itself renders from) instead of a tag filter.
Fixture stubs db.repo.getCachedWrapped, the only source _buildWrappedContext
reads from since R6 (2026-09-02) made the cache the only Wrapped path."""
import datetime
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
from _app_factory import AppTestCase
import Database.utils as utilsModule
from conftest import wrappedCachedRow


def _ts(year, month=6, day=1, hour=12):
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp()


def _song(trackId, name):
    return {
        "id": trackId, "name": name, "url": f"http://example.com/{trackId}",
        "imageId": "i", "duration": 0, "explicit": False, "isrc": "US1234567890",
        "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
        "album": {"id": "alb1", "name": "Album", "url": "u", "imageId": "i", "imageUrl": "",
                   "totalTracks": 1, "releaseDate": 0},
        "artists": [{"id": "a1", "name": "Artist", "url": "u", "imageUrl": "", "imageId": "a1"}],
        "plays": 5, "totalTimeListened": 5000, "firstListenedAt": 100,
    }


class TestWrappedPlaylistExport(AppTestCase):
    """All tests fix the app's timezone to UTC and freeze `now()` to
    2026-07-11 (matching test_wrapped_route.py) so year math is deterministic."""

    def setUp(self):
        tzPatcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        tzPatcher.start()
        self.addCleanup(tzPatcher.stop)

        nowPatcher = patch.object(appModule, "now",
                                   return_value=datetime.datetime(2026, 7, 11, tzinfo=datetime.timezone.utc))
        nowPatcher.start()
        self.addCleanup(nowPatcher.stop)

    def _makeDb(self, songs=None):
        db = MagicMock()
        # A play in 2026 so _computeAvailableYears includes it.
        db.getEntriesFromOld.return_value = [{"id": "x", "playedAt": _ts(2026), "timePlayed": 1}]
        db.repo.getCachedWrapped.return_value = wrappedCachedRow(topSongs=songs or [])
        db.getTaggedTracks.return_value = []   #< the tag-based export branch, unrelated to the year branch
        return db

    def _get(self, dash, db, query):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(query)

    def test_exports_the_years_top_songs_as_csv(self):
        dash = self._makeApp()
        db = self._makeDb(songs=[_song("t1", "Wrapped Song One")])

        resp = self._get(dash, db, "/playlist/export?year=2026&format=csv")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/csv")
        self.assertIn("Wrapped Song One", resp.get_data(as_text=True))
        self.assertEqual(resp.headers["Content-Disposition"], 'attachment; filename="wrapped_top100_2026.csv"')

    def test_exports_as_m3u(self):
        dash = self._makeApp()
        db = self._makeDb(songs=[_song("t1", "Wrapped Song One")])

        resp = self._get(dash, db, "/playlist/export?year=2026&format=m3u")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("audio/x-mpegurl", resp.mimetype)
        self.assertIn("spotify:track:t1", resp.get_data(as_text=True))

    def test_exports_as_xspf(self):
        dash = self._makeApp()
        db = self._makeDb(songs=[_song("t1", "Wrapped Song One")])

        resp = self._get(dash, db, "/playlist/export?year=2026&format=xspf")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/xspf+xml", resp.mimetype)
        self.assertIn("Wrapped 2026 Top 100", resp.get_data(as_text=True))

    def test_requests_up_to_100_songs_regardless_of_wrapped_page_limit(self):
        """The export hardcodes limit=WRAPPED_TOP_SONGS_EXPORT_LIMIT (100),
        not the page's own default (WRAPPED_LIST_SIZE=10) - an 11-song
        cached pool proves it: a 10-item cap would cut the lowest-played
        song, a 100-item one would not."""
        dash = self._makeApp()
        songs = [_song(f"hi{i}", f"HighPlaySong{i}") for i in range(10)]
        for i, song in enumerate(songs):
            song["plays"] = 100 - i   #< ranks 1-10 by descending plays
        lowest = _song("lo1", "LowestPlayedSong")
        lowest["plays"] = 1   #< rank 11 - cut by a 10-item cap, kept by a 100-item one
        db = self._makeDb(songs=songs + [lowest])

        resp = self._get(dash, db, "/playlist/export?year=2026&format=csv")

        self.assertIn("LowestPlayedSong", resp.get_data(as_text=True))

    def test_year_outside_available_years_is_rejected(self):
        dash = self._makeApp()
        db = self._makeDb(songs=[_song("t1", "Wrapped Song One")])

        resp = self._get(dash, db, "/playlist/export?year=1999&format=csv")

        self.assertEqual(resp.status_code, 400)

    def test_non_integer_year_is_rejected(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._get(dash, db, "/playlist/export?year=not-a-year&format=csv")

        self.assertEqual(resp.status_code, 400)

    def test_year_absent_still_uses_tag_based_export(self):
        """Existing tag-based behavior must be untouched when `year` isn't
        passed at all - only its presence switches the branch."""
        dash = self._makeApp()
        db = self._makeDb()
        db.getTaggedTracks.return_value = [_song("t1", "Tag Song")]

        resp = self._get(dash, db, "/playlist/export?tags=workout&format=csv")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("Tag Song", resp.get_data(as_text=True))
        db.repo.getCachedWrapped.assert_not_called()   #< the year/cache branch was never touched


class TestWrappedDownloadButtonVisibility(AppTestCase):
    """The Download Top 100 control (templates/wrapped.html) must only appear
    on the authenticated owner's view of a year that actually has songs -
    not on the public /shared/<token> page, and not on an empty year."""

    def setUp(self):
        tzPatcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        tzPatcher.start()
        self.addCleanup(tzPatcher.stop)

        nowPatcher = patch.object(appModule, "now",
                                   return_value=datetime.datetime(2026, 7, 11, tzinfo=datetime.timezone.utc))
        nowPatcher.start()
        self.addCleanup(nowPatcher.stop)

        self.dash = self._makeApp()

    def _makeDb(self, songs=None):
        db = MagicMock()
        db.tz = datetime.timezone.utc
        db.getEntriesFromOld.return_value = [{"id": "x", "playedAt": _ts(2026), "timePlayed": 1}]
        db.repo.getCachedWrapped.return_value = wrappedCachedRow(topSongs=songs or [])
        return db

    def _getWrapped(self, db, query=""):
        client = self.dash.app.test_client()
        with patch.object(self.dash, 'is_user_logged_in', return_value=True), \
             patch.object(self.dash, 'get_username_for_email', return_value='alice'), \
             patch.object(self.dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/wrapped{query}")

    def test_button_present_when_top_songs_exist(self):
        db = self._makeDb(songs=[_song("t1", "Wrapped Song One")])

        resp = self._getWrapped(db)

        #< the shared _playlist_download.html control's button class - the
        #  bespoke #downloadWrappedPlaylistBtn id went with the bespoke handler
        self.assertIn(b'js-playlist-download', resp.data)

    def test_button_absent_when_year_has_no_songs(self):
        db = self._makeDb(songs=[])

        resp = self._getWrapped(db)

        self.assertNotIn(b'js-playlist-download', resp.data)

    def test_button_absent_on_public_shared_view(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        db = self._makeDb(songs=[_song("t1", "Wrapped Song One")])

        client = self.dash.app.test_client()
        with patch.object(self.dash, '_getReadOnlyUserDb', return_value=db):
            resp = client.get(f"/shared/{token}")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'js-playlist-download', resp.data)


if __name__ == "__main__":
    import unittest
    unittest.main()
