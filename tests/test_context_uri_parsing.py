"""Database.playlistName / Database.updatePlaylists must tolerate malformed
playedFrom context values (no colon, empty) instead of raising ValueError on
tuple unpacking - a corrupted played_from row would otherwise 500 the history
page via app._embedSongTextElements."""
import sys
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase


class TestPlaylistNameUriParsing(DatabaseTestCase):
    def _db(self):
        return self._makeDb(tracks={}, entries=[])

    def test_valid_playlist_uri_resolves_name(self):
        db = self._db()
        db.repo.upsertPlaylistName("pl1", "playlist", "Road Trip Mix")
        self.assertEqual(db.playlistName("playlist:pl1"), "Road Trip Mix")

    def test_none_and_empty_return_none(self):
        db = self._db()
        self.assertIsNone(db.playlistName(None))
        self.assertIsNone(db.playlistName(""))

    def test_colonless_uri_returns_none_instead_of_raising(self):
        db = self._db()
        self.assertIsNone(db.playlistName("playlist"))
        self.assertIsNone(db.playlistName("garbage-no-colon"))


class TestUpdatePlaylistsUriParsing(DatabaseTestCase):
    def _db(self):
        return self._makeDb(tracks={}, entries=[])

    def test_none_is_a_noop(self):
        db = self._db()
        db.updatePlaylists(None)   #< must not raise

    def test_colonless_uri_is_skipped_without_raising(self):
        db = self._db()
        db.updatePlaylists("playlist")
        db.updatePlaylists("garbage-no-colon")
        self.assertFalse(db.repo.playlistKnown("playlist", "playlist"))

    def test_known_playlist_short_circuits(self):
        db = self._db()
        db.repo.upsertPlaylistName("pl1", "playlist", "Existing")
        db.updatePlaylists("playlist:pl1")   #< must not hit the listener
        self.assertEqual(db.repo.getPlaylistName("pl1", "playlist"), "Existing")


class TestALookupFailureIsNotCachedAsAnAnswer(DatabaseTestCase):
    """playlistKnown tests only that a ROW EXISTS, so whatever the first
    lookup produces is the name forever - there is no second writer and no
    retry anywhere. That makes it matter a great deal which failures are
    allowed to write one."""

    def _db(self, sideEffect=None, name=None):
        db = self._makeDb(tracks={}, entries=[])
        db.listener = MagicMock()
        if sideEffect is not None:
            db.listener.playlistName.side_effect = sideEffect
        else:
            db.listener.playlistName.return_value = name
        return db

    def test_a_rate_limited_lookup_is_retried_rather_than_remembered(self):
        """The shared limiter's backoff window is opened by ANY user's 429, so
        this lands on the first play from a playlist through no fault of its
        own. Cached, that playlist has no name in /history's "played from" for
        good, and /history's search can never match it."""
        db = self._db(sideEffect=Exception("429 Too Many Requests"))

        db.updatePlaylists("playlist:pl1")

        self.assertFalse(db.repo.playlistKnown("pl1", "playlist"))

        db.listener.playlistName.side_effect = None
        db.listener.playlistName.return_value = "Road Trip Mix"
        db.updatePlaylists("playlist:pl1")

        self.assertEqual(db.repo.getPlaylistName("pl1", "playlist"), "Road Trip Mix")

    def test_a_private_playlist_is_still_remembered_after_one_look(self):
        """The other half, and why this is not just "never cache a failure":
        a private playlist raises every time, so re-asking would spend a
        Spotify call on every play the user makes from it."""
        db = self._db(sideEffect=Exception("403 Forbidden"))

        db.updatePlaylists("playlist:pl1")

        self.assertTrue(db.repo.playlistKnown("pl1", "playlist"))
        self.assertIsNone(db.repo.getPlaylistName("pl1", "playlist"))
        db.updatePlaylists("playlist:pl1")
        self.assertEqual(db.listener.playlistName.call_count, 1)

    def test_a_degraded_response_does_not_become_a_playlist_called_Unknown(self):
        """A name is what the page prints, and updatePlaylists stores whatever
        this returns. A literal "Unknown Playlist"/"Unknown Album" is
        indistinguishable from a real title, outlives the outage that produced
        it, and reads as a playlist someone actually named that. None says the
        same thing without inventing a title.

        Called unbound on a stub: these two lines touch nothing but self.sp,
        and building a real Listener would need a live cookie session."""
        from Database.Listeners.spotifyListener import Listener

        stub = SimpleNamespace(sp=MagicMock(playlist=lambda _: None, album=lambda _: None))

        self.assertIsNone(Listener.playlistName(stub, "pl1"))
        self.assertIsNone(Listener.albumName(stub, "al1"))

    def test_a_real_name_is_still_returned(self):
        from Database.Listeners.spotifyListener import Listener

        stub = SimpleNamespace(sp=MagicMock(playlist=lambda _: {"name": "Road Trip Mix"},
                                            album=lambda _: {"name": "Remain in Light"}))

        self.assertEqual(Listener.playlistName(stub, "pl1"), "Road Trip Mix")
        self.assertEqual(Listener.albumName(stub, "al1"), "Remain in Light")


if __name__ == "__main__":
    unittest.main()
