"""The per-user Last.fm album biography backfill worker: lifecycle, own-queue
-> global-queue fallback, definitive-vs-transient marking, primary-artist
resolution and cross-user in-flight dedup (shared "album_bio" claim kind with
lazyFetchAlbumBio). Runs independently of the artist biography backfiller, on
its own thread and stop event. The Last.fm client is always mocked (conftest
blocks real sockets anyway)."""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.database import Database
from Database.lastfm import AlbumInfoOutcome, OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_TRANSIENT, OUTCOME_INVALID_KEY


def _oneShotStopEvent():
    """Stand-in stop event for driving the loop exactly once - see
    test_lastfm_biography_backfiller.py's identical helper."""
    event = MagicMock()
    event.is_set.return_value = False
    calls = {"count": 0}

    def wait(timeout=None):
        calls["count"] += 1
        return calls["count"] > 1

    event.wait.side_effect = wait
    return event


class AlbumBiographyWorkerBase(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        Database._lastfm_active.clear()
        self.addCleanup(Database._lastfm_active.clear)

    @staticmethod
    def _album(albumId, name):
        return {"id": albumId, "name": name, "url": "http://example.com/album",
                "imageId": albumId, "imageUrl": "", "totalTracks": 1, "releaseDate": 0}

    def _makeDbWithPlays(self, username="user1"):
        tracks = {
            "tA": {"id": "tA", "name": "Song A", "artists": [{"id": "aX", "name": "Artist X"}],
                   "album": self._album("alP", "Album P")},
            "tB": {"id": "tB", "name": "Song B", "artists": [{"id": "aY", "name": "Artist Y"}],
                   "album": self._album("alQ", "Album Q")},
        }
        entries = [
            {"id": "tA", "playedAt": 1000, "timePlayed": 5000},
            {"id": "tA", "playedAt": 2000, "timePlayed": 5000},
            {"id": "tB", "playedAt": 3000, "timePlayed": 5000},
        ]
        return self._makeDb(tracks, entries, username=username)


class WorkerLifecycleTestCase(AlbumBiographyWorkerBase):
    def test_without_a_key_start_is_a_noop(self):
        db = self._makeDbWithPlays()
        db.startLastfmAlbumBiographyBackfiller()
        self.assertIsNone(db.lastfm_album_biography_thread)

    def test_with_a_key_the_thread_starts_and_stop_joins_it(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmAlbumBiographyBackfiller()
        self.assertIsNotNone(db.lastfm_album_biography_thread)
        self.assertTrue(db.lastfm_album_biography_thread.is_alive())
        db.stopLastfmAlbumBiographyBackfiller()
        self.assertIsNone(db.lastfm_album_biography_thread)

    def test_duplicate_start_keeps_the_running_thread(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmAlbumBiographyBackfiller()
        firstThread = db.lastfm_album_biography_thread
        db.startLastfmAlbumBiographyBackfiller()
        self.assertIs(db.lastfm_album_biography_thread, firstThread)
        db.stopLastfmAlbumBiographyBackfiller()

    def test_restart_uses_a_fresh_stop_event_so_a_lingering_thread_cannot_revive(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmAlbumBiographyBackfiller()
        firstEvent = db.lastfm_album_biography_stop_event
        db.stopLastfmAlbumBiographyBackfiller()

        db.startLastfmAlbumBiographyBackfiller()
        self.assertIsNot(db.lastfm_album_biography_stop_event, firstEvent)
        self.assertTrue(firstEvent.is_set())
        self.assertFalse(db.lastfm_album_biography_stop_event.is_set())
        db.stopLastfmAlbumBiographyBackfiller()

    def test_autostart_survives_a_pre_migration_schema(self):
        import sqlite3 as sqlite3Module
        db = self._makeDbWithPlays()
        with patch.object(db.repo, "getUserLastfmApiKey",
                          side_effect=sqlite3Module.OperationalError("no such column: lastfm_api_key")):
            db.startLastfmAlbumBiographyBackfiller()   #< must not raise
        self.assertIsNone(db.lastfm_album_biography_thread)

    def test_database_stop_stops_the_worker(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmAlbumBiographyBackfiller()
        runningThread = db.lastfm_album_biography_thread
        db.stop()
        self.assertFalse(runningThread.is_alive())
        self.assertIsNone(db.lastfm_album_biography_thread)

    def test_init_autostarts_only_with_a_stored_key(self):
        withoutKey = self._makeDbWithPlays()
        self.assertIsNone(withoutKey.lastfm_album_biography_thread)

        withoutKey.repo.updateUserLastfmApiKey("user1", "key123")
        dbPath = withoutKey.repo.connectionManager.dbPath
        withKey = Database("user1", dbPath=dbPath)
        self.addCleanup(withKey.repo.connectionManager.close)
        self.addCleanup(withKey.stop)
        self.assertIsNotNone(withKey.lastfm_album_biography_thread)
        self.assertTrue(withKey.lastfm_album_biography_thread.is_alive())


class WorkerLoopTestCase(AlbumBiographyWorkerBase):
    @patch("Database.database.LastfmClient")
    def test_loop_without_a_key_makes_no_client(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.lastfm_album_biography_stop_event = _oneShotStopEvent()
        db._lastfmAlbumBiographyBackfillLoop()
        mockClientClass.assert_not_called()

    @patch("Database.database.LastfmClient")
    def test_one_cycle_fetches_and_stores_bios_most_played_first(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        client = MagicMock()
        client.getAlbumInfo.return_value = AlbumInfoOutcome(OUTCOME_OK, "A landmark album.")
        mockClientClass.return_value = client

        db.lastfm_album_biography_stop_event = _oneShotStopEvent()
        db._lastfmAlbumBiographyBackfillLoop()

        mockClientClass.assert_called_with("key123")
        self.assertEqual(db.repo.getAlbumBioState("alP")["bio"], "A landmark album.")
        self.assertEqual(db.repo.getAlbumBioState("alQ")["bio"], "A landmark album.")
        # Priority order: most-played album looked up first, with its
        # resolved primary artist.
        firstCall = client.getAlbumInfo.call_args_list[0]
        self.assertEqual(firstCall.args, ("Artist X", "Album P"))

    @patch("Database.database.LastfmClient")
    def test_definitive_no_bio_still_stamps_attempted(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        client = MagicMock()
        client.getAlbumInfo.return_value = AlbumInfoOutcome(OUTCOME_NOT_FOUND, None)
        mockClientClass.return_value = client

        db.lastfm_album_biography_stop_event = _oneShotStopEvent()
        db._lastfmAlbumBiographyBackfillLoop()

        state = db.repo.getAlbumBioState("alP")
        self.assertIsNone(state["bio"])
        self.assertIsNotNone(state["attempted_at"])

    @patch("Database.database.LastfmClient")
    def test_own_queue_is_drained_before_the_global_queue(self, mockClientClass):
        db = self._makeDbWithPlays(username="user1")
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.repo.upsertUser("user2", "user2@example.com")
        db.repo.upsertTrack(normalizeTrackForTest(
            {"id": "tC", "name": "Song C", "artists": [{"id": "aZ", "name": "Artist Z"}],
             "album": self._album("alR", "Album R")}))
        db.repo.insertPlay("user2", "tC", 5000, 5000, None)
        db.repo.commit()
        db.repo.setAlbumBio("alP", "Bio P")
        db.repo.setAlbumBio("alQ", "Bio Q")

        client = MagicMock()
        client.getAlbumInfo.return_value = AlbumInfoOutcome(OUTCOME_OK, "Bio R")
        mockClientClass.return_value = client

        db.lastfm_album_biography_stop_event = _oneShotStopEvent()
        db._lastfmAlbumBiographyBackfillLoop()

        self.assertEqual(db.repo.getAlbumBioState("alR")["bio"], "Bio R")

    @patch("Database.database.LastfmClient")
    def test_disabled_kill_switch_idles_the_loop_without_reading_the_key(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.repo.setAlbumBioEnabled(False)

        db.lastfm_album_biography_stop_event = _oneShotStopEvent()
        db._lastfmAlbumBiographyBackfillLoop()

        mockClientClass.assert_not_called()
        self.assertIsNone(db.repo.getAlbumBioState("alP")["attempted_at"])

    @patch("Database.database.LastfmClient")
    def test_invalid_key_idles_the_loop_instead_of_hammering(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        client = MagicMock()
        client.getAlbumInfo.return_value = AlbumInfoOutcome(OUTCOME_INVALID_KEY, None)
        mockClientClass.return_value = client

        db.lastfm_album_biography_stop_event = _oneShotStopEvent()
        db._lastfmAlbumBiographyBackfillLoop()   #< must terminate cleanly via the idle wait

        client.getAlbumInfo.assert_called_once()
        self.assertIsNone(db.repo.getAlbumBioState("alP")["attempted_at"])

    @patch("Database.database.LastfmClient")
    def test_transient_outcome_leaves_the_album_unattempted(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        client = MagicMock()
        client.getAlbumInfo.return_value = AlbumInfoOutcome(OUTCOME_TRANSIENT, None)
        mockClientClass.return_value = client

        db.lastfm_album_biography_stop_event = _oneShotStopEvent()
        db._lastfmAlbumBiographyBackfillLoop()

        self.assertIsNone(db.repo.getAlbumBioState("alP")["attempted_at"])

    @patch("Database.database.LastfmClient")
    def test_loop_failure_then_success_updates_telemetry(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        failingClient = MagicMock()
        failingClient.getAlbumInfo.side_effect = RuntimeError("Last.fm unreachable")
        mockClientClass.return_value = failingClient

        db.lastfm_album_biography_stop_event = _oneShotStopEvent()
        db._lastfmAlbumBiographyBackfillLoop()

        telemetry = db._getWorkerTelemetry("lastfm_album_bio")
        self.assertEqual(telemetry["consecutive_failures"], 1)
        self.assertIn("Last.fm unreachable", telemetry["last_error"])

        succeedingClient = MagicMock()
        succeedingClient.getAlbumInfo.return_value = AlbumInfoOutcome(OUTCOME_OK, "A landmark album.")
        mockClientClass.return_value = succeedingClient

        db.lastfm_album_biography_stop_event = _oneShotStopEvent()
        db._lastfmAlbumBiographyBackfillLoop()

        telemetry = db._getWorkerTelemetry("lastfm_album_bio")
        self.assertEqual(telemetry["consecutive_failures"], 0)


class WorkerBatchTestCase(AlbumBiographyWorkerBase):
    """_processLastfmAlbumBiographyBatch details, driven directly with a real
    (unset) stop event and a crafted client."""

    def test_entities_claimed_by_another_worker_are_skipped_and_kept(self):
        db = self._makeDbWithPlays()
        Database._lastfm_active.add(("album_bio", "alP"))
        client = MagicMock()
        client.getAlbumInfo.return_value = AlbumInfoOutcome(OUTCOME_OK, "Bio text.")

        db._processLastfmAlbumBiographyBatch(client, "user1")

        self.assertIsNone(db.repo.getAlbumBioState("alP")["bio"])           #< skipped
        self.assertEqual(db.repo.getAlbumBioState("alQ")["bio"], "Bio text.")
        self.assertIn(("album_bio", "alP"), Database._lastfm_active)        #< other worker's claim intact
        self.assertNotIn(("album_bio", "alQ"), Database._lastfm_active)     #< own claim released

    def test_claims_are_released_even_when_processing_raises(self):
        db = self._makeDbWithPlays()
        client = MagicMock()
        client.getAlbumInfo.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            db._processLastfmAlbumBiographyBatch(client, "user1")

        self.assertNotIn(("album_bio", "alP"), Database._lastfm_active)
        self.assertNotIn(("album_bio", "alQ"), Database._lastfm_active)

    def test_aborted_rate_limit_slot_ends_the_batch(self):
        db = self._makeDbWithPlays()
        client = MagicMock()
        client.getAlbumInfo.return_value = None   #< acquire() aborted
        processed = db._processLastfmAlbumBiographyBatch(client, "user1")
        self.assertFalse(processed)

    def test_shares_the_in_flight_claim_with_the_on_demand_lazy_fetch(self):
        db = self._makeDbWithPlays()
        claimed = db._claimLastfmEntities("album_bio", [{"id": "alP"}])
        self.assertEqual(claimed, [{"id": "alP"}])

        client = MagicMock()
        client.getAlbumInfo.return_value = AlbumInfoOutcome(OUTCOME_OK, "Bio text.")
        db._processLastfmAlbumBiographyBatch(client, "user1")

        self.assertIsNone(db.repo.getAlbumBioState("alP")["bio"])   #< still claimed, skipped
        db._releaseLastfmEntities("album_bio", [{"id": "alP"}])

    def test_album_with_no_resolvable_primary_artist_is_marked_attempted_without_a_lookup(self):
        tracks = {"tOrphan": {"id": "tOrphan", "name": "No Artist", "artists": [],
                              "album": self._album("alOrphan", "Orphan Album")}}
        entries = [{"id": "tOrphan", "playedAt": 1000, "timePlayed": 5000}]
        db = self._makeDb(tracks, entries)
        client = MagicMock()

        processed = db._processLastfmAlbumBiographyBatch(client, "testuser")

        self.assertTrue(processed)
        client.getAlbumInfo.assert_not_called()
        state = db.repo.getAlbumBioState("alOrphan")
        self.assertIsNone(state["bio"])
        self.assertIsNotNone(state["attempted_at"])

    def test_decorated_album_name_retries_with_cleaned_name_if_verbatim_returns_no_bio(self):
        tracks = {"tDecorated": {"id": "tDecorated", "name": "Song",
                                 "artists": [{"id": "a1", "name": "Queen"}],
                                 "album": self._album("alDecorated", "The Game (2011 Remaster)")}}
        entries = [{"id": "tDecorated", "playedAt": 1000, "timePlayed": 5000}]
        db = self._makeDb(tracks, entries)

        def side_effect(artist, album, stop_event=None):
            if album == "The Game (2011 Remaster)":
                return AlbumInfoOutcome(OUTCOME_NOT_FOUND, None)
            elif album == "The Game":
                return AlbumInfoOutcome(OUTCOME_OK, "Bio for The Game")
            return AlbumInfoOutcome(OUTCOME_NOT_FOUND, None)

        client = MagicMock()
        client.getAlbumInfo.side_effect = side_effect

        processed = db._processLastfmAlbumBiographyBatch(client, "testuser")

        self.assertTrue(processed)
        self.assertEqual(client.getAlbumInfo.call_count, 2)
        client.getAlbumInfo.assert_any_call("Queen", "The Game (2011 Remaster)", stop_event=unittest.mock.ANY)
        client.getAlbumInfo.assert_any_call("Queen", "The Game", stop_event=unittest.mock.ANY)
        state = db.repo.getAlbumBioState("alDecorated")
        self.assertEqual(state["bio"], "Bio for The Game")
        self.assertIsNotNone(state["attempted_at"])

    def test_decorated_album_name_does_not_retry_if_verbatim_succeeds(self):
        tracks = {"tDecorated": {"id": "tDecorated", "name": "Song",
                                 "artists": [{"id": "a1", "name": "Queen"}],
                                 "album": self._album("alDecorated", "The Game (2011 Remaster)")}}
        entries = [{"id": "tDecorated", "playedAt": 1000, "timePlayed": 5000}]
        db = self._makeDb(tracks, entries)

        client = MagicMock()
        client.getAlbumInfo.return_value = AlbumInfoOutcome(OUTCOME_OK, "Bio for verbatim title")

        processed = db._processLastfmAlbumBiographyBatch(client, "testuser")

        self.assertTrue(processed)
        client.getAlbumInfo.assert_called_once_with("Queen", "The Game (2011 Remaster)", stop_event=unittest.mock.ANY)
        state = db.repo.getAlbumBioState("alDecorated")
        self.assertEqual(state["bio"], "Bio for verbatim title")


if __name__ == "__main__":
    unittest.main()

