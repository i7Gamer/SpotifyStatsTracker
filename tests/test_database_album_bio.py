"""lazyFetchAlbumBio: the album-bio feature's lazy, one-shot fetch via
Last.fm's album.getinfo - mirrors lazyFetchArtistBio (test_database_artist_bio.py),
but album.getinfo also needs the album's primary artist name."""
import sys
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database
from Database.repository import Repository
from Database.lastfm import AlbumInfoOutcome, OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_TRANSIENT, OUTCOME_INVALID_KEY


class LazyFetchAlbumBioTestCase(unittest.TestCase):
    def _db(self, lastfmApiKey="test-key"):
        db = Database.__new__(Database)
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: db.repo.connectionManager.close())
        db.repo = Repository(Path(tmpdir) / "test.db")
        db.user = "testuser"
        db.email = "testuser@example.com"
        db.repo.upsertUser(db.user, db.email)
        if lastfmApiKey is not None:
            db.repo.updateUserLastfmApiKey(db.user, lastfmApiKey)
        return db

    def _seedAlbum(self, db, albumId, name="Some Album"):
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES (?, ?, '')", (albumId, name))

    def test_returns_true_without_fetching_if_already_attempted(self):
        db = self._db()
        self._seedAlbum(db, "al1")
        db.repo.setAlbumBio("al1", "Existing bio.")
        with patch("Database.database.LastfmClient") as mockClientClass:
            result = db.lazyFetchAlbumBio("al1", "Some Album", "Some Artist")
        self.assertTrue(result)
        mockClientClass.assert_not_called()

    def test_returns_false_when_id_name_or_artist_missing(self):
        db = self._db()
        with patch("Database.database.LastfmClient") as mockClientClass:
            self.assertFalse(db.lazyFetchAlbumBio("", "Some Album", "Some Artist"))
            self.assertFalse(db.lazyFetchAlbumBio("al1", "", "Some Artist"))
            self.assertFalse(db.lazyFetchAlbumBio("al1", "Some Album", ""))
        mockClientClass.assert_not_called()

    def test_returns_false_when_feature_disabled(self):
        db = self._db()
        db.repo.setAlbumBioEnabled(False)
        with patch("Database.database.LastfmClient") as mockClientClass:
            result = db.lazyFetchAlbumBio("al1", "Some Album", "Some Artist")
        self.assertFalse(result)
        mockClientClass.assert_not_called()
        self.assertIsNone(db.repo.getAlbumBioState("al1")["attempted_at"])

    def test_returns_false_when_no_stored_lastfm_key(self):
        db = self._db(lastfmApiKey=None)
        with patch("Database.database.LastfmClient") as mockClientClass:
            result = db.lazyFetchAlbumBio("al1", "Some Album", "Some Artist")
        self.assertFalse(result)
        mockClientClass.assert_not_called()

    def test_fetches_and_stores_a_real_bio(self):
        db = self._db()
        self._seedAlbum(db, "al1")
        mockClient = MagicMock()
        mockClient.getAlbumInfo.return_value = AlbumInfoOutcome(OUTCOME_OK, "A landmark album.")
        with patch("Database.database.LastfmClient", return_value=mockClient):
            future = db.lazyFetchAlbumBio("al1", "Some Album", "Some Artist")
            result = future.result(timeout=5)

        self.assertTrue(result)
        mockClient.getAlbumInfo.assert_called_once_with("Some Artist", "Some Album")
        state = db.repo.getAlbumBioState("al1")
        self.assertEqual(state["bio"], "A landmark album.")
        self.assertIsNotNone(state["attempted_at"])

    def test_definitive_no_bio_still_stamps_attempted(self):
        db = self._db()
        for outcome in (AlbumInfoOutcome(OUTCOME_OK, None), AlbumInfoOutcome(OUTCOME_NOT_FOUND, None)):
            with self.subTest(status=outcome.status):
                albumId = f"al-{outcome.status}"
                self._seedAlbum(db, albumId)
                mockClient = MagicMock()
                mockClient.getAlbumInfo.return_value = outcome
                with patch("Database.database.LastfmClient", return_value=mockClient):
                    future = db.lazyFetchAlbumBio(albumId, "Some Album", "Some Artist")
                    future.result(timeout=5)
                state = db.repo.getAlbumBioState(albumId)
                self.assertIsNone(state["bio"])
                self.assertIsNotNone(state["attempted_at"])

    def test_transient_and_invalid_key_outcomes_stay_unattempted(self):
        db = self._db()
        for outcome in (AlbumInfoOutcome(OUTCOME_TRANSIENT, None),
                       AlbumInfoOutcome(OUTCOME_INVALID_KEY, None), None):
            with self.subTest(outcome=outcome):
                albumId = f"al-{outcome.status if outcome else 'none'}"
                mockClient = MagicMock()
                mockClient.getAlbumInfo.return_value = outcome
                with patch("Database.database.LastfmClient", return_value=mockClient):
                    future = db.lazyFetchAlbumBio(albumId, "Some Album", "Some Artist")
                    future.result(timeout=5)
                self.assertIsNone(db.repo.getAlbumBioState(albumId)["attempted_at"])

    def test_does_not_retry_after_a_definitive_no_bio(self):
        db = self._db()
        self._seedAlbum(db, "al1")
        mockClient = MagicMock()
        mockClient.getAlbumInfo.return_value = AlbumInfoOutcome(OUTCOME_OK, None)
        with patch("Database.database.LastfmClient", return_value=mockClient):
            first = db.lazyFetchAlbumBio("al1", "Some Album", "Some Artist")
            first.result(timeout=5)
            second = db.lazyFetchAlbumBio("al1", "Some Album", "Some Artist")

        self.assertTrue(second)
        mockClient.getAlbumInfo.assert_called_once()

    def test_concurrent_calls_for_the_same_album_only_fetch_once(self):
        db = self._db()
        self._seedAlbum(db, "al1")
        gate = threading.Event()

        def gatedGetAlbumInfo(artist, album):
            gate.wait(timeout=5)
            return AlbumInfoOutcome(OUTCOME_OK, "Bio text.")

        mockClient = MagicMock()
        mockClient.getAlbumInfo.side_effect = gatedGetAlbumInfo
        with patch("Database.database.LastfmClient", return_value=mockClient):
            firstFuture = db.lazyFetchAlbumBio("al1", "Some Album", "Some Artist")
            secondResult = db.lazyFetchAlbumBio("al1", "Some Album", "Some Artist")   #< claimed already
            gate.set()
            firstFuture.result(timeout=5)

        self.assertFalse(secondResult)
        mockClient.getAlbumInfo.assert_called_once()

    def test_exception_is_swallowed_and_leaves_the_album_unattempted(self):
        db = self._db()
        with patch("Database.database.LastfmClient", side_effect=Exception("boom")):
            future = db.lazyFetchAlbumBio("al1", "Some Album", "Some Artist")
            future.result(timeout=5)   #< must not raise
        self.assertIsNone(db.repo.getAlbumBioState("al1")["attempted_at"])

    def test_dispatch_does_not_block_the_calling_thread(self):
        db = self._db()
        self._seedAlbum(db, "alSlow")
        gate = threading.Event()

        def gatedGetAlbumInfo(artist, album):
            gate.wait(timeout=5)
            return AlbumInfoOutcome(OUTCOME_OK, "Bio text.")

        mockClient = MagicMock()
        mockClient.getAlbumInfo.side_effect = gatedGetAlbumInfo
        with patch("Database.database.LastfmClient", return_value=mockClient):
            future = db.lazyFetchAlbumBio("alSlow", "Some Album", "Some Artist")

            self.assertFalse(future.done())
            gate.set()
            self.assertTrue(future.result(timeout=5))


if __name__ == "__main__":
    unittest.main()
