"""Now Playing: the dashboard shows what's playing right now.

Read entirely from the connect player_state dict spotapi's websocket tick
already keeps refreshed (the same zero-extra-network source the missed-track
cross-check uses) - polling this must never add Spotify API calls.
"""
import sys
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from conftest import DatabaseTestCase
from Database.database import Database
from Database.Listeners.spotifyListener import Listener
from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'

_NOW_MS = None  #< set per test from time.time()


def _playingState(trackId="t1", isPaused=False, positionMs=5000, durationMs=200000,
                  ageSeconds=10, metadata=None):
    """A connect player_state dict as spotapi caches it - numeric fields are
    strings there, matching the real feed."""
    return {
        "is_playing": True,
        "is_paused": isPaused,
        "timestamp": str(int((time.time() - ageSeconds) * 1000)),
        "position_as_of_timestamp": str(positionMs),
        "duration": str(durationMs),
        "track": {"uri": f"spotify:track:{trackId}", "metadata": metadata or {}},
    }


class TestListenerGetConnectPlayerState(unittest.TestCase):
    def _bareListener(self):
        listener = Listener.__new__(Listener)
        listener.sp = MagicMock()
        return listener

    def test_returns_the_cached_state_dict(self):
        listener = self._bareListener()
        listener.sp.lastPlayedManager.manager._state = {"is_playing": True}
        self.assertEqual(listener.getConnectPlayerState(), {"is_playing": True})

    def test_returns_none_without_a_manager(self):
        listener = self._bareListener()
        listener.sp.lastPlayedManager = None
        self.assertIsNone(listener.getConnectPlayerState())

    def test_returns_none_before_the_first_websocket_tick(self):
        listener = self._bareListener()
        listener.sp.lastPlayedManager.manager._state = None
        self.assertIsNone(listener.getConnectPlayerState())


class TestGetNowPlaying(DatabaseTestCase):
    def _makeDbWithState(self, state, tracks=None):
        db = self._makeDb(tracks or {}, [])
        db.listener = SimpleNamespace(getConnectPlayerState=lambda: state)
        return db

    def test_no_listener_returns_none(self):
        db = self._makeDb({}, [])
        db.listener = None
        self.assertIsNone(db.getNowPlaying())

    def test_no_state_returns_none(self):
        self.assertIsNone(self._makeDbWithState(None).getNowPlaying())

    def test_not_playing_returns_none(self):
        state = _playingState()
        state["is_playing"] = False
        self.assertIsNone(self._makeDbWithState(state).getNowPlaying())

    def test_non_track_uri_returns_none(self):
        """Ads/episodes in the connect state aren't tracks we can show."""
        state = _playingState()
        state["track"]["uri"] = "spotify:ad:12345"
        self.assertIsNone(self._makeDbWithState(state).getNowPlaying())

    def test_playing_track_resolves_metadata_from_the_catalog(self):
        tracks = {"t1": {"id": "t1", "name": "Live Song",
                         "artists": [{"id": "a1", "name": "Artist One"}], "imageId": "img1"}}
        db = self._makeDbWithState(_playingState("t1"), tracks=tracks)

        nowPlaying = db.getNowPlaying()

        self.assertEqual(nowPlaying["trackId"], "t1")
        self.assertEqual(nowPlaying["name"], "Live Song")
        self.assertEqual(nowPlaying["artistsText"], "Artist One")
        self.assertEqual(nowPlaying["imageId"], "img1")
        self.assertFalse(nowPlaying["isPaused"])
        self.assertEqual(nowPlaying["durationMs"], 200000)

    _T1_CATALOG = {"t1": {"id": "t1", "name": "Live Song", "artists": []}}

    def test_position_advances_with_elapsed_time_while_playing(self):
        db = self._makeDbWithState(_playingState("t1", positionMs=5000, ageSeconds=10),
                                   tracks=self._T1_CATALOG)
        nowPlaying = db.getNowPlaying()
        #< 5s position + ~10s elapsed since the state snapshot
        self.assertGreaterEqual(nowPlaying["positionMs"], 14000)
        self.assertLessEqual(nowPlaying["positionMs"], 17000)

    def test_position_is_frozen_while_paused(self):
        db = self._makeDbWithState(_playingState("t1", isPaused=True, positionMs=5000, ageSeconds=3600),
                                   tracks=self._T1_CATALOG)
        nowPlaying = db.getNowPlaying()
        self.assertTrue(nowPlaying["isPaused"])
        self.assertEqual(nowPlaying["positionMs"], 5000)

    def test_track_that_should_have_ended_long_ago_is_treated_as_stale(self):
        """A frozen websocket leaves the state saying 'playing' forever - once
        the track's own duration (plus grace) has elapsed, report nothing."""
        state = _playingState("t1", positionMs=0, durationMs=180000, ageSeconds=600)
        self.assertIsNone(self._makeDbWithState(state).getNowPlaying())

    def test_unknown_track_falls_back_to_connect_state_metadata(self):
        """A track being heard for the very first time isn't in the catalog
        yet (metadata is only fetched when the play completes)."""
        state = _playingState("brandnew", metadata={"title": "Fresh Track", "artist_name": "New Artist"})
        nowPlaying = self._makeDbWithState(state).getNowPlaying()
        self.assertEqual(nowPlaying["name"], "Fresh Track")
        self.assertEqual(nowPlaying["artistsText"], "New Artist")
        self.assertIsNone(nowPlaying["imageId"])

    def test_unknown_track_without_any_metadata_reports_nothing(self):
        nowPlaying = self._makeDbWithState(_playingState("brandnew")).getNowPlaying()
        self.assertIsNone(nowPlaying)

    def test_metadata_as_dataclass_object_does_not_crash(self):
        """Regression: spotapi sometimes stores metadata as an already-hydrated
        object (truthy, but has no .get()), causing AttributeError in the
        connect-state fallback branch. The fix must handle both dicts and
        any attribute-bearing object."""
        from types import SimpleNamespace
        metaNs = SimpleNamespace(title="Dataclass Track", artist_name="NS Artist")
        state = _playingState("brandnew")
        state["track"]["metadata"] = metaNs
        nowPlaying = self._makeDbWithState(state).getNowPlaying()
        self.assertEqual(nowPlaying["name"], "Dataclass Track")
        self.assertEqual(nowPlaying["artistsText"], "NS Artist")
        self.assertIsNone(nowPlaying["imageId"])


class TestNowPlayingRoute(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _get(self, dash, db):
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get('/api/now-playing')

    def test_requires_login(self):
        dash = self._makeApp()
        resp = dash.app.test_client().get('/api/now-playing')
        self.assertEqual(resp.status_code, 401)

    def test_returns_the_now_playing_payload(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getNowPlaying.return_value = {"trackId": "t1", "name": "Live Song"}

        resp = self._get(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["nowPlaying"]["name"], "Live Song")

    def test_returns_null_when_nothing_is_playing(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getNowPlaying.return_value = None

        resp = self._get(dash, db)

        self.assertIsNone(resp.get_json()["nowPlaying"])


if __name__ == "__main__":
    unittest.main()
