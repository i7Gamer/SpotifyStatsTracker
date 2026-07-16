"""The per-user Last.fm genre backfill worker: lifecycle, the artists->albums->
tracks cycle with genre inheritance, own-queue -> global-queue fallback,
definitive-vs-transient marking and cross-user in-flight dedup. The Last.fm
client is always mocked (conftest blocks real sockets anyway)."""
import sys
import os
import threading
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.database import Database
from Database.lastfm import FetchOutcome, OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_TRANSIENT, OUTCOME_INVALID_KEY

OK_EMPTY = FetchOutcome(OUTCOME_OK, [])
ROCK_TAGS = FetchOutcome(OUTCOME_OK, [{"name": "rock", "count": 100},
                                      {"name": "seen live", "count": 90},
                                      {"name": "indie rock", "count": 80}])


def _album(albumId, name=None):
    return {"id": albumId, "name": name or albumId, "url": "http://example.com/album",
            "imageId": albumId, "imageUrl": "", "totalTracks": 1, "releaseDate": 0}


def _oneShotStopEvent():
    """Stand-in stop event for driving the loop exactly once: is_set() stays
    False, the first wait() (the startup delay) passes, any later wait (an
    idle/backoff wait) stops the loop - robust against how often the loop
    checks is_set() internally."""
    event = MagicMock()
    event.is_set.return_value = False
    calls = {"count": 0}

    def wait(timeout=None):
        calls["count"] += 1
        return calls["count"] > 1

    event.wait.side_effect = wait
    return event


class LastfmWorkerBase(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        Database._lastfm_active.clear()
        self.addCleanup(Database._lastfm_active.clear)

    def _makeDbWithPlays(self, username="user1"):
        tracks = {
            "tA": {"id": "tA", "name": "Song A",
                   "artists": [{"id": "aX", "name": "Artist X"}], "album": _album("alP", "Album P")},
            "tB": {"id": "tB", "name": "Song B",
                   "artists": [{"id": "aY", "name": "Artist Y"}], "album": _album("alQ", "Album Q")},
        }
        entries = [
            {"id": "tA", "playedAt": 1000, "timePlayed": 5000},
            {"id": "tA", "playedAt": 2000, "timePlayed": 5000},
            {"id": "tB", "playedAt": 3000, "timePlayed": 5000},
        ]
        return self._makeDb(tracks, entries, username=username)


class WorkerLifecycleTestCase(LastfmWorkerBase):
    def test_without_a_key_start_is_a_noop(self):
        db = self._makeDbWithPlays()
        db.startLastfmGenreBackfiller()
        self.assertIsNone(db.lastfm_thread)

    def test_with_a_key_the_thread_starts_and_stop_joins_it(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmGenreBackfiller()
        self.assertIsNotNone(db.lastfm_thread)
        self.assertTrue(db.lastfm_thread.is_alive())   #< sits in its random startup delay
        db.stopLastfmGenreBackfiller()
        self.assertIsNone(db.lastfm_thread)

    def test_duplicate_start_keeps_the_running_thread(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmGenreBackfiller()
        firstThread = db.lastfm_thread
        db.startLastfmGenreBackfiller()
        self.assertIs(db.lastfm_thread, firstThread)
        db.stopLastfmGenreBackfiller()

    def test_database_stop_stops_the_worker(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmGenreBackfiller()
        runningThread = db.lastfm_thread
        db.stop()
        self.assertFalse(runningThread.is_alive())
        self.assertIsNone(db.lastfm_thread)

    def test_init_autostarts_only_with_a_stored_key(self):
        withoutKey = self._makeDbWithPlays()
        self.assertIsNone(withoutKey.lastfm_thread)

        # A second instance over the same shared DB file sees the stored key.
        withoutKey.repo.updateUserLastfmApiKey("user1", "key123")
        dbPath = withoutKey.repo.connectionManager.dbPath
        withKey = Database("user1", dbPath=dbPath)
        self.addCleanup(withKey.repo.connectionManager.close)
        self.addCleanup(withKey.stop)
        self.assertIsNotNone(withKey.lastfm_thread)
        self.assertTrue(withKey.lastfm_thread.is_alive())

    def test_worker_status_reflects_key_and_thread(self):
        db = self._makeDbWithPlays()
        self.assertEqual(db.getLastfmWorkerStatus(), {"configured": False, "running": False})
        db.repo.updateUserLastfmApiKey("user1", "key123")
        self.assertEqual(db.getLastfmWorkerStatus(), {"configured": True, "running": False})
        db.startLastfmGenreBackfiller()
        self.assertEqual(db.getLastfmWorkerStatus(), {"configured": True, "running": True})
        db.stopLastfmGenreBackfiller()
        self.assertEqual(db.getLastfmWorkerStatus(), {"configured": True, "running": False})


class WorkerLoopTestCase(LastfmWorkerBase):
    @patch("Database.database.LastfmClient")
    def test_loop_without_a_key_makes_no_client(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.lastfm_stop_event = _oneShotStopEvent()
        db._lastfmGenreBackfillLoop()
        mockClientClass.assert_not_called()

    @patch("Database.database.LastfmClient")
    def test_one_cycle_processes_artists_albums_and_tracks(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        client = MagicMock()
        client.getArtistTopTags.return_value = ROCK_TAGS
        client.getAlbumTopTags.return_value = ROCK_TAGS
        client.getTrackTopTags.return_value = ROCK_TAGS
        mockClientClass.return_value = client

        db.lastfm_stop_event = _oneShotStopEvent()
        db._lastfmGenreBackfillLoop()

        mockClientClass.assert_called_with("key123")
        self.assertEqual(db.repo.getArtistGenres("aX"), ["rock", "indie rock"])
        self.assertEqual(db.repo.getArtistGenres("aY"), ["rock", "indie rock"])
        self.assertEqual([g["genre"] for g in db.repo.getAlbumGenres("alP")], ["rock", "indie rock"])
        self.assertEqual([g["genre"] for g in db.repo.getTrackGenres("tA")], ["rock", "indie rock"])
        self.assertFalse(any(g["inherited"] for g in db.repo.getTrackGenres("tA")))

        conn = db.repo._conn()
        for table, entityId in (("artists", "aX"), ("albums", "alP"), ("tracks", "tA")):
            stamp = conn.execute(f"SELECT lastfm_attempted_at FROM {table} WHERE id=?",
                                 (entityId,)).fetchone()["lastfm_attempted_at"]
            self.assertIsNotNone(stamp)

        # Priority order: most-played artist looked up first.
        firstArtistCall = client.getArtistTopTags.call_args_list[0]
        self.assertEqual(firstArtistCall.args[0], "Artist X")

    @patch("Database.database.LastfmClient")
    def test_own_queue_is_drained_before_the_global_queue(self, mockClientClass):
        db = self._makeDbWithPlays(username="user1")
        db.repo.updateUserLastfmApiKey("user1", "key123")
        # Another user's played entities exist and are missing genres too.
        db.repo.upsertUser("user2", "user2@example.com")
        db.repo.upsertTrack(normalizeTrackForTest(
            {"id": "tC", "name": "Song C", "artists": [{"id": "aZ", "name": "Artist Z"}],
             "album": _album("alR", "Album R")}))
        db.repo.insertPlay("user2", "tC", 5000, 5000, None)
        db.repo.commit()
        # user1's own entities are all definitively attempted already.
        db.repo.markArtistsLastfmAttempted(["aX", "aY"])
        db.repo.markAlbumsLastfmAttempted(["alP", "alQ"])
        db.repo.markTracksLastfmAttempted(["tA", "tB"])

        client = MagicMock()
        client.getArtistTopTags.return_value = ROCK_TAGS
        client.getAlbumTopTags.return_value = ROCK_TAGS
        client.getTrackTopTags.return_value = ROCK_TAGS
        mockClientClass.return_value = client

        db.lastfm_stop_event = _oneShotStopEvent()
        db._lastfmGenreBackfillLoop()

        # The global fallback fetched user2's artist even though user1 is done.
        self.assertEqual(db.repo.getArtistGenres("aZ"), ["rock", "indie rock"])

    @patch("Database.database.LastfmClient")
    def test_invalid_key_idles_the_loop_instead_of_hammering(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        client = MagicMock()
        client.getArtistTopTags.return_value = FetchOutcome(OUTCOME_INVALID_KEY, [])
        mockClientClass.return_value = client

        db.lastfm_stop_event = _oneShotStopEvent()
        db._lastfmGenreBackfillLoop()   #< must terminate cleanly via the idle wait

        client.getArtistTopTags.assert_called_once()   #< first invalid response stops the batch
        self.assertEqual(db.repo.getArtistGenres("aX"), [])
        stamp = db.repo._conn().execute(
            "SELECT lastfm_attempted_at FROM artists WHERE id='aX'").fetchone()[0]
        self.assertIsNone(stamp)   #< nothing marked - a fixed key retries everything


class WorkerBatchTestCase(LastfmWorkerBase):
    """_processLastfm*Batch details, driven directly with a real (unset)
    stop event and a crafted client."""

    def _clientReturning(self, **methodOutcomes):
        client = MagicMock()
        for method, outcome in methodOutcomes.items():
            getattr(client, method).return_value = outcome
        return client

    def test_track_with_own_tags_stores_them_uninherited(self):
        db = self._makeDbWithPlays()
        client = self._clientReturning(getTrackTopTags=ROCK_TAGS)
        db._processLastfmTrackBatch(client, "user1")
        self.assertEqual(db.repo.getTrackGenres("tA"),
                         [{"genre": "rock", "inherited": False},
                          {"genre": "indie rock", "inherited": False}])
        client.getTrackTopTags.assert_any_call("Artist X", "Song A", stop_event=db.lastfm_stop_event)

    def test_tagless_track_inherits_from_a_finished_artist(self):
        db = self._makeDbWithPlays()
        db.repo.replaceArtistGenres("aX", ["shoegaze", "dream pop"])
        db.repo.markArtistsLastfmAttempted(["aX", "aY"])
        client = self._clientReturning(getTrackTopTags=FetchOutcome(OUTCOME_NOT_FOUND, []))

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(db.repo.getTrackGenres("tA"),
                         [{"genre": "shoegaze", "inherited": True},
                          {"genre": "dream pop", "inherited": True}])
        # tB's artist aY was attempted but has no genres -> marked bare.
        self.assertEqual(db.repo.getTrackGenres("tB"), [])
        conn = db.repo._conn()
        self.assertIsNotNone(conn.execute(
            "SELECT lastfm_attempted_at FROM tracks WHERE id='tB'").fetchone()[0])

    def test_tagless_track_of_a_pending_artist_stays_unmarked(self):
        db = self._makeDbWithPlays()   #< artists never attempted
        client = self._clientReturning(getTrackTopTags=OK_EMPTY)

        db._processLastfmTrackBatch(client, "user1")

        conn = db.repo._conn()
        self.assertIsNone(conn.execute(
            "SELECT lastfm_attempted_at FROM tracks WHERE id='tA'").fetchone()[0])
        self.assertEqual(db.repo.getTrackGenres("tA"), [])   #< requeues next cycle

    def test_transient_outcomes_leave_entities_unmarked_and_report_no_progress(self):
        db = self._makeDbWithPlays()
        client = self._clientReturning(getArtistTopTags=FetchOutcome(OUTCOME_TRANSIENT, []))

        processed = db._processLastfmArtistBatch(client, "user1")

        self.assertFalse(processed)   #< transient-only batches idle the loop instead of spinning
        conn = db.repo._conn()
        self.assertIsNone(conn.execute(
            "SELECT lastfm_attempted_at FROM artists WHERE id='aX'").fetchone()[0])

    def test_album_lookup_uses_the_derived_primary_artist(self):
        db = self._makeDbWithPlays()
        client = self._clientReturning(getAlbumTopTags=ROCK_TAGS)
        db._processLastfmAlbumBatch(client, "user1")
        client.getAlbumTopTags.assert_any_call("Artist X", "Album P", stop_event=db.lastfm_stop_event)
        self.assertEqual([g["genre"] for g in db.repo.getAlbumGenres("alP")], ["rock", "indie rock"])

    def test_album_without_a_derivable_artist_is_marked_without_a_lookup(self):
        tracks = {"tOrphan": {"id": "tOrphan", "name": "No Artist", "artists": [],
                              "album": _album("alOrphan", "Orphan Album")}}
        entries = [{"id": "tOrphan", "playedAt": 1000, "timePlayed": 5000}]
        db = self._makeDb(tracks, entries, username="user1")
        client = self._clientReturning(getAlbumTopTags=ROCK_TAGS)

        db._processLastfmAlbumBatch(client, "user1")

        client.getAlbumTopTags.assert_not_called()
        stamp = db.repo._conn().execute(
            "SELECT lastfm_attempted_at FROM albums WHERE id='alOrphan'").fetchone()[0]
        self.assertIsNotNone(stamp)

    def test_entities_claimed_by_another_worker_are_skipped_and_kept(self):
        db = self._makeDbWithPlays()
        Database._lastfm_active.add(("artist", "aX"))
        client = self._clientReturning(getArtistTopTags=ROCK_TAGS)

        db._processLastfmArtistBatch(client, "user1")

        self.assertEqual(db.repo.getArtistGenres("aX"), [])          #< skipped
        self.assertEqual(db.repo.getArtistGenres("aY"), ["rock", "indie rock"])
        self.assertIn(("artist", "aX"), Database._lastfm_active)     #< other worker's claim intact
        self.assertNotIn(("artist", "aY"), Database._lastfm_active)  #< own claim released

    def test_claims_are_released_even_when_processing_raises(self):
        db = self._makeDbWithPlays()
        client = MagicMock()
        client.getArtistTopTags.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            db._processLastfmArtistBatch(client, "user1")

        self.assertNotIn(("artist", "aX"), Database._lastfm_active)
        self.assertNotIn(("artist", "aY"), Database._lastfm_active)

    def test_aborted_rate_limit_slot_ends_the_batch(self):
        db = self._makeDbWithPlays()
        client = self._clientReturning(getArtistTopTags=None)   #< acquire() aborted
        processed = db._processLastfmArtistBatch(client, "user1")
        self.assertFalse(processed)


if __name__ == "__main__":
    unittest.main()
