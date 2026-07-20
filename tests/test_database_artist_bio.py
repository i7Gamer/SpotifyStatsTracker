"""lazyFetchArtistBio: the artist-bio feature's lazy, one-shot fetch via
Last.fm's artist.getinfo - mirrors lazyFetchArtistImage's dispatch-to-a-
shared-executor shape, but for text instead of a downloaded file."""
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
from Database.lastfm import ArtistInfoOutcome, OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_TRANSIENT, OUTCOME_INVALID_KEY


class LazyFetchArtistBioTestCase(unittest.TestCase):
    """Bare Database instances (like test_database_images.py's
    _bareDatabase()) - __init__'s autoimporter/listener/worker-thread setup
    isn't relevant here and, on Windows, leaves the temp db file locked past
    test teardown if left running."""

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

    def _seedArtist(self, db, artistId, name="Some Artist"):
        """setArtistBio is a plain UPDATE (see its docstring: the artist row
        always already exists by the time lazyFetchArtistBio is called, from
        the artist detail page route after getArtist() has already found
        it) - tests that exercise a real fetch-and-store need the row to
        exist first, same as production."""
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO artists (id, name, url) VALUES (?, ?, '')", (artistId, name))

    def test_returns_true_without_fetching_if_already_attempted(self):
        db = self._db()
        self._seedArtist(db, "art1")
        db.repo.setArtistBio("art1", "Existing bio.")
        with patch("Database.database.LastfmClient") as mockClientClass:
            result = db.lazyFetchArtistBio("art1", "Some Artist")
        self.assertTrue(result)
        mockClientClass.assert_not_called()

    def test_returns_false_when_artist_id_or_name_missing(self):
        db = self._db()
        with patch("Database.database.LastfmClient") as mockClientClass:
            self.assertFalse(db.lazyFetchArtistBio("", "Some Artist"))
            self.assertFalse(db.lazyFetchArtistBio("art1", ""))
        mockClientClass.assert_not_called()

    def test_returns_false_when_feature_disabled(self):
        db = self._db()
        db.repo.setArtistBioEnabled(False)
        with patch("Database.database.LastfmClient") as mockClientClass:
            result = db.lazyFetchArtistBio("art1", "Some Artist")
        self.assertFalse(result)
        mockClientClass.assert_not_called()
        self.assertIsNone(db.repo.getArtistBioState("art1")["attempted_at"])

    def test_returns_false_when_no_stored_lastfm_key(self):
        db = self._db(lastfmApiKey=None)
        with patch("Database.database.LastfmClient") as mockClientClass:
            result = db.lazyFetchArtistBio("art1", "Some Artist")
        self.assertFalse(result)
        mockClientClass.assert_not_called()

    def test_fetches_and_stores_a_real_bio(self):
        db = self._db()
        self._seedArtist(db, "art1")
        mockClient = MagicMock()
        mockClient.getArtistInfo.return_value = ArtistInfoOutcome(OUTCOME_OK, "A great band.")
        with patch("Database.database.LastfmClient", return_value=mockClient):
            future = db.lazyFetchArtistBio("art1", "Some Artist")
            result = future.result(timeout=5)

        self.assertTrue(result)
        mockClient.getArtistInfo.assert_called_once_with("Some Artist")
        state = db.repo.getArtistBioState("art1")
        self.assertEqual(state["bio"], "A great band.")
        self.assertIsNotNone(state["attempted_at"])

    def test_definitive_no_bio_still_stamps_attempted(self):
        """OUTCOME_OK with no bio, or OUTCOME_NOT_FOUND, are both definitive
        "nothing available" results - same contract as the genre workers."""
        db = self._db()
        for outcome in (ArtistInfoOutcome(OUTCOME_OK, None), ArtistInfoOutcome(OUTCOME_NOT_FOUND, None)):
            with self.subTest(status=outcome.status):
                artistId = f"art-{outcome.status}"
                self._seedArtist(db, artistId)
                mockClient = MagicMock()
                mockClient.getArtistInfo.return_value = outcome
                with patch("Database.database.LastfmClient", return_value=mockClient):
                    future = db.lazyFetchArtistBio(artistId, "Some Artist")
                    future.result(timeout=5)
                state = db.repo.getArtistBioState(artistId)
                self.assertIsNone(state["bio"])
                self.assertIsNotNone(state["attempted_at"])

    def test_transient_and_invalid_key_outcomes_stay_unattempted(self):
        """A network hiccup or a bad API key must not be recorded as a
        definitive "no bio" - a later page view should retry."""
        db = self._db()
        for outcome in (ArtistInfoOutcome(OUTCOME_TRANSIENT, None),
                       ArtistInfoOutcome(OUTCOME_INVALID_KEY, None), None):
            with self.subTest(outcome=outcome):
                artistId = f"art-{outcome.status if outcome else 'none'}"
                mockClient = MagicMock()
                mockClient.getArtistInfo.return_value = outcome
                with patch("Database.database.LastfmClient", return_value=mockClient):
                    future = db.lazyFetchArtistBio(artistId, "Some Artist")
                    future.result(timeout=5)
                self.assertIsNone(db.repo.getArtistBioState(artistId)["attempted_at"])

    def test_does_not_retry_after_a_definitive_no_bio(self):
        db = self._db()
        self._seedArtist(db, "art1")
        mockClient = MagicMock()
        mockClient.getArtistInfo.return_value = ArtistInfoOutcome(OUTCOME_OK, None)
        with patch("Database.database.LastfmClient", return_value=mockClient):
            first = db.lazyFetchArtistBio("art1", "Some Artist")
            first.result(timeout=5)
            second = db.lazyFetchArtistBio("art1", "Some Artist")

        #< dedup path returns True directly ("already attempted" - see
        #  lazyFetchArtistBio's docstring), not a new Future/fetch
        self.assertTrue(second)
        mockClient.getArtistInfo.assert_called_once()

    def test_concurrent_calls_for_the_same_artist_only_fetch_once(self):
        """The in-flight claim (shared with the genre workers' Database._lastfm_active)
        prevents a second concurrent lazy-fetch for the same artist id."""
        db = self._db()
        self._seedArtist(db, "art1")
        gate = threading.Event()

        def gatedGetArtistInfo(name):
            gate.wait(timeout=5)
            return ArtistInfoOutcome(OUTCOME_OK, "Bio text.")

        mockClient = MagicMock()
        mockClient.getArtistInfo.side_effect = gatedGetArtistInfo
        with patch("Database.database.LastfmClient", return_value=mockClient):
            firstFuture = db.lazyFetchArtistBio("art1", "Some Artist")
            secondResult = db.lazyFetchArtistBio("art1", "Some Artist")   #< claimed already, no fetch
            gate.set()
            firstFuture.result(timeout=5)

        self.assertFalse(secondResult)
        mockClient.getArtistInfo.assert_called_once()

    def test_exception_is_swallowed_and_leaves_the_artist_unattempted(self):
        db = self._db()
        with patch("Database.database.LastfmClient", side_effect=Exception("boom")):
            future = db.lazyFetchArtistBio("art1", "Some Artist")
            future.result(timeout=5)   #< must not raise
        self.assertIsNone(db.repo.getArtistBioState("art1")["attempted_at"])

    def test_dispatch_does_not_block_the_calling_thread(self):
        db = self._db()
        self._seedArtist(db, "artSlow")
        gate = threading.Event()

        def gatedGetArtistInfo(name):
            gate.wait(timeout=5)
            return ArtistInfoOutcome(OUTCOME_OK, "Bio text.")

        mockClient = MagicMock()
        mockClient.getArtistInfo.side_effect = gatedGetArtistInfo
        with patch("Database.database.LastfmClient", return_value=mockClient):
            future = db.lazyFetchArtistBio("artSlow", "Some Artist")

            self.assertFalse(future.done())   #< fetch is parked on the gate, dispatch already returned
            gate.set()
            self.assertTrue(future.result(timeout=5))


if __name__ == "__main__":
    unittest.main()
