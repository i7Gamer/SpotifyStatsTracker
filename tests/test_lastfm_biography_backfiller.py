"""The per-user Last.fm biography backfill worker: lifecycle, own-queue ->
global-queue fallback, definitive-vs-transient marking and cross-user
in-flight dedup (shared with lazyFetchArtistBio's "bio" claim kind). Runs
independently of the genre backfiller, on its own thread and stop event. The
Last.fm client is always mocked (conftest blocks real sockets anyway)."""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.database import Database
from Database.lastfm import ArtistInfoOutcome, OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_TRANSIENT, OUTCOME_INVALID_KEY


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


class BiographyWorkerBase(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        Database._lastfm_active.clear()
        self.addCleanup(Database._lastfm_active.clear)

    def _makeDbWithPlays(self, username="user1"):
        tracks = {
            "tA": {"id": "tA", "name": "Song A", "artists": [{"id": "aX", "name": "Artist X"}]},
            "tB": {"id": "tB", "name": "Song B", "artists": [{"id": "aY", "name": "Artist Y"}]},
        }
        entries = [
            {"id": "tA", "playedAt": 1000, "timePlayed": 5000},
            {"id": "tA", "playedAt": 2000, "timePlayed": 5000},
            {"id": "tB", "playedAt": 3000, "timePlayed": 5000},
        ]
        return self._makeDb(tracks, entries, username=username)


class WorkerLifecycleTestCase(BiographyWorkerBase):
    def test_without_a_key_start_is_a_noop(self):
        db = self._makeDbWithPlays()
        db.startLastfmBiographyBackfiller()
        self.assertIsNone(db.lastfm_biography_thread)

    def test_with_a_key_the_thread_starts_and_stop_joins_it(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmBiographyBackfiller()
        self.assertIsNotNone(db.lastfm_biography_thread)
        self.assertTrue(db.lastfm_biography_thread.is_alive())   #< sits in its random startup delay
        db.stopLastfmBiographyBackfiller()
        self.assertIsNone(db.lastfm_biography_thread)

    def test_duplicate_start_keeps_the_running_thread(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmBiographyBackfiller()
        firstThread = db.lastfm_biography_thread
        db.startLastfmBiographyBackfiller()
        self.assertIs(db.lastfm_biography_thread, firstThread)
        db.stopLastfmBiographyBackfiller()

    def test_restart_uses_a_fresh_stop_event_so_a_lingering_thread_cannot_revive(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmBiographyBackfiller()
        firstEvent = db.lastfm_biography_stop_event
        db.stopLastfmBiographyBackfiller()

        db.startLastfmBiographyBackfiller()
        self.assertIsNot(db.lastfm_biography_stop_event, firstEvent)
        self.assertTrue(firstEvent.is_set())            #< the old thread's signal stays set
        self.assertFalse(db.lastfm_biography_stop_event.is_set())
        db.stopLastfmBiographyBackfiller()

    def test_autostart_survives_a_pre_migration_schema(self):
        import sqlite3 as sqlite3Module
        db = self._makeDbWithPlays()
        with patch.object(db.repo, "getUserLastfmApiKey",
                          side_effect=sqlite3Module.OperationalError("no such column: lastfm_api_key")):
            db.startLastfmBiographyBackfiller()   #< must not raise
        self.assertIsNone(db.lastfm_biography_thread)

    def test_database_stop_stops_the_worker(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmBiographyBackfiller()
        runningThread = db.lastfm_biography_thread
        db.stop()
        self.assertFalse(runningThread.is_alive())
        self.assertIsNone(db.lastfm_biography_thread)

    def test_init_autostarts_only_with_a_stored_key(self):
        withoutKey = self._makeDbWithPlays()
        self.assertIsNone(withoutKey.lastfm_biography_thread)

        # A second instance over the same shared DB file sees the stored key.
        withoutKey.repo.updateUserLastfmApiKey("user1", "key123")
        dbPath = withoutKey.repo.connectionManager.dbPath
        withKey = Database("user1", dbPath=dbPath)
        self.addCleanup(withKey.repo.connectionManager.close)
        self.addCleanup(withKey.stop)
        self.assertIsNotNone(withKey.lastfm_biography_thread)
        self.assertTrue(withKey.lastfm_biography_thread.is_alive())


class WorkerLoopTestCase(BiographyWorkerBase):
    @patch("Database.database.LastfmClient")
    def test_loop_without_a_key_makes_no_client(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.lastfm_biography_stop_event = _oneShotStopEvent()
        db._lastfmBiographyBackfillLoop()
        mockClientClass.assert_not_called()

    @patch("Database.database.LastfmClient")
    def test_one_cycle_fetches_and_stores_bios_most_played_first(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        client = MagicMock()
        client.getArtistInfo.return_value = ArtistInfoOutcome(OUTCOME_OK, "A great band.")
        mockClientClass.return_value = client

        db.lastfm_biography_stop_event = _oneShotStopEvent()
        db._lastfmBiographyBackfillLoop()

        mockClientClass.assert_called_with("key123")
        self.assertEqual(db.repo.getArtistBioState("aX")["bio"], "A great band.")
        self.assertEqual(db.repo.getArtistBioState("aY")["bio"], "A great band.")
        # Priority order: most-played artist looked up first.
        firstCall = client.getArtistInfo.call_args_list[0]
        self.assertEqual(firstCall.args[0], "Artist X")

    @patch("Database.database.LastfmClient")
    def test_definitive_no_bio_still_stamps_attempted(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        client = MagicMock()
        client.getArtistInfo.return_value = ArtistInfoOutcome(OUTCOME_NOT_FOUND, None)
        mockClientClass.return_value = client

        db.lastfm_biography_stop_event = _oneShotStopEvent()
        db._lastfmBiographyBackfillLoop()

        state = db.repo.getArtistBioState("aX")
        self.assertIsNone(state["bio"])
        self.assertIsNotNone(state["attempted_at"])

    @patch("Database.database.LastfmClient")
    def test_own_queue_is_drained_before_the_global_queue(self, mockClientClass):
        db = self._makeDbWithPlays(username="user1")
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.repo.upsertUser("user2", "user2@example.com")
        db.repo.upsertTrack(normalizeTrackForTest(
            {"id": "tC", "name": "Song C", "artists": [{"id": "aZ", "name": "Artist Z"}]}))
        db.repo.insertPlay("user2", "tC", 5000, 5000, None)
        db.repo.commit()
        # user1's own artists are already definitively attempted.
        db.repo.setArtistBio("aX", "Bio X")
        db.repo.setArtistBio("aY", "Bio Y")

        client = MagicMock()
        client.getArtistInfo.return_value = ArtistInfoOutcome(OUTCOME_OK, "Bio Z")
        mockClientClass.return_value = client

        db.lastfm_biography_stop_event = _oneShotStopEvent()
        db._lastfmBiographyBackfillLoop()

        # The global fallback fetched user2's artist even though user1 is done.
        self.assertEqual(db.repo.getArtistBioState("aZ")["bio"], "Bio Z")

    @patch("Database.database.LastfmClient")
    def test_disabled_kill_switch_idles_the_loop_without_reading_the_key(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.repo.setArtistBioEnabled(False)

        db.lastfm_biography_stop_event = _oneShotStopEvent()
        db._lastfmBiographyBackfillLoop()

        mockClientClass.assert_not_called()
        self.assertIsNone(db.repo.getArtistBioState("aX")["attempted_at"])

    @patch("Database.database.LastfmClient")
    def test_invalid_key_idles_the_loop_instead_of_hammering(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        client = MagicMock()
        client.getArtistInfo.return_value = ArtistInfoOutcome(OUTCOME_INVALID_KEY, None)
        mockClientClass.return_value = client

        db.lastfm_biography_stop_event = _oneShotStopEvent()
        db._lastfmBiographyBackfillLoop()   #< must terminate cleanly via the idle wait

        client.getArtistInfo.assert_called_once()   #< first invalid response stops the batch
        self.assertIsNone(db.repo.getArtistBioState("aX")["attempted_at"])

    @patch("Database.database.LastfmClient")
    def test_transient_outcome_leaves_the_artist_unattempted(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        client = MagicMock()
        client.getArtistInfo.return_value = ArtistInfoOutcome(OUTCOME_TRANSIENT, None)
        mockClientClass.return_value = client

        db.lastfm_biography_stop_event = _oneShotStopEvent()
        db._lastfmBiographyBackfillLoop()

        self.assertIsNone(db.repo.getArtistBioState("aX")["attempted_at"])

    @patch("Database.database.LastfmClient")
    def test_loop_failure_then_success_updates_telemetry(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        failingClient = MagicMock()
        failingClient.getArtistInfo.side_effect = RuntimeError("Last.fm unreachable")
        mockClientClass.return_value = failingClient

        db.lastfm_biography_stop_event = _oneShotStopEvent()
        db._lastfmBiographyBackfillLoop()

        telemetry = db._getWorkerTelemetry("lastfm_artist_bio")
        self.assertEqual(telemetry["consecutive_failures"], 1)
        self.assertIn("Last.fm unreachable", telemetry["last_error"])

        succeedingClient = MagicMock()
        succeedingClient.getArtistInfo.return_value = ArtistInfoOutcome(OUTCOME_OK, "A great band.")
        mockClientClass.return_value = succeedingClient

        db.lastfm_biography_stop_event = _oneShotStopEvent()
        db._lastfmBiographyBackfillLoop()

        telemetry = db._getWorkerTelemetry("lastfm_artist_bio")
        self.assertEqual(telemetry["consecutive_failures"], 0)


class WorkerBatchTestCase(BiographyWorkerBase):
    """_processLastfmBiographyBatch details, driven directly with a real
    (unset) stop event and a crafted client."""

    def test_entities_claimed_by_another_worker_are_skipped_and_kept(self):
        db = self._makeDbWithPlays()
        Database._lastfm_active.add(("bio", "aX"))
        client = MagicMock()
        client.getArtistInfo.return_value = ArtistInfoOutcome(OUTCOME_OK, "Bio text.")

        db._processLastfmBiographyBatch(client, "user1")

        self.assertIsNone(db.repo.getArtistBioState("aX")["bio"])           #< skipped
        self.assertEqual(db.repo.getArtistBioState("aY")["bio"], "Bio text.")
        self.assertIn(("bio", "aX"), Database._lastfm_active)               #< other worker's claim intact
        self.assertNotIn(("bio", "aY"), Database._lastfm_active)            #< own claim released

    def test_claims_are_released_even_when_processing_raises(self):
        db = self._makeDbWithPlays()
        client = MagicMock()
        client.getArtistInfo.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            db._processLastfmBiographyBatch(client, "user1")

        self.assertNotIn(("bio", "aX"), Database._lastfm_active)
        self.assertNotIn(("bio", "aY"), Database._lastfm_active)

    def test_aborted_rate_limit_slot_ends_the_batch(self):
        db = self._makeDbWithPlays()
        client = MagicMock()
        client.getArtistInfo.return_value = None   #< acquire() aborted
        processed = db._processLastfmBiographyBatch(client, "user1")
        self.assertFalse(processed)

    def test_shares_the_in_flight_claim_with_the_on_demand_lazy_fetch(self):
        """The background worker and lazyFetchArtistBio's on-demand path
        both claim under the same "bio" kind, so a page view mid-cycle can't
        double-fetch the artist the worker is already handling."""
        db = self._makeDbWithPlays()
        claimed = db._claimLastfmEntities("bio", [{"id": "aX"}])
        self.assertEqual(claimed, [{"id": "aX"}])

        client = MagicMock()
        client.getArtistInfo.return_value = ArtistInfoOutcome(OUTCOME_OK, "Bio text.")
        db._processLastfmBiographyBatch(client, "user1")

        self.assertIsNone(db.repo.getArtistBioState("aX")["bio"])   #< still claimed, skipped
        db._releaseLastfmEntities("bio", [{"id": "aX"}])


if __name__ == "__main__":
    unittest.main()
