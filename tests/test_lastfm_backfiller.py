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

    def test_restart_uses_a_fresh_stop_event_so_a_lingering_thread_cannot_revive(self):
        """stop() joins with a timeout - a worker blocked in a slow HTTP call
        can outlive it. A restart must NOT clear the event that zombie still
        watches (that would revive it, doubling the request volume forever);
        each run gets its own event instead."""
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmGenreBackfiller()
        firstEvent = db.lastfm_stop_event
        db.stopLastfmGenreBackfiller()

        db.startLastfmGenreBackfiller()
        self.assertIsNot(db.lastfm_stop_event, firstEvent)
        self.assertTrue(firstEvent.is_set())            #< the old thread's signal stays set
        self.assertFalse(db.lastfm_stop_event.is_set())
        db.stopLastfmGenreBackfiller()

    def test_autostart_survives_a_pre_migration_schema(self):
        """Database() constructed against a pre-1.19 file outside the app's
        migration path (standalone script/REPL) must not crash in __init__
        just because users.lastfm_api_key doesn't exist yet."""
        import sqlite3 as sqlite3Module
        db = self._makeDbWithPlays()
        with patch.object(db.repo, "getUserLastfmApiKey",
                          side_effect=sqlite3Module.OperationalError("no such column: lastfm_api_key")):
            db.startLastfmGenreBackfiller()   #< must not raise
        self.assertIsNone(db.lastfm_thread)

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
    def test_disabled_kill_switch_idles_the_loop_without_reading_the_key(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.repo.setLastfmGenreBackfillEnabled(False)

        db.lastfm_stop_event = _oneShotStopEvent()
        db._lastfmGenreBackfillLoop()

        mockClientClass.assert_not_called()
        self.assertIsNone(db.repo._conn().execute(
            "SELECT lastfm_attempted_at FROM artists WHERE id='aX'").fetchone()[0])

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

    def test_tagless_track_resolves_its_pending_artist_inline(self):
        """A tag-less entity whose artist has no definitive result yet must
        resolve the artist with one inline request instead of staying
        unmarked - the artist may never appear in any queue (see the album
        test below), and an unmarked entity is re-fetched every cycle."""
        db = self._makeDbWithPlays()   #< artists never attempted
        client = self._clientReturning(getTrackTopTags=OK_EMPTY, getArtistTopTags=ROCK_TAGS)

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(db.repo.getArtistGenres("aX"), ["rock", "indie rock"])
        self.assertEqual(db.repo.getTrackGenres("tA"),
                         [{"genre": "rock", "inherited": True},
                          {"genre": "indie rock", "inherited": True}])
        conn = db.repo._conn()
        for table, entityId in (("tracks", "tA"), ("artists", "aX")):
            self.assertIsNotNone(conn.execute(
                f"SELECT lastfm_attempted_at FROM {table} WHERE id=?", (entityId,)).fetchone()[0])

    def test_tagless_track_stays_unmarked_when_the_inline_artist_lookup_fails(self):
        db = self._makeDbWithPlays()
        client = self._clientReturning(getTrackTopTags=OK_EMPTY,
                                       getArtistTopTags=FetchOutcome(OUTCOME_TRANSIENT, []))

        db._processLastfmTrackBatch(client, "user1")

        conn = db.repo._conn()
        self.assertIsNone(conn.execute(
            "SELECT lastfm_attempted_at FROM tracks WHERE id='tA'").fetchone()[0])
        self.assertEqual(db.repo.getTrackGenres("tA"), [])   #< requeues next cycle

    def test_album_with_an_unplayed_primary_artist_is_resolved_in_one_pass(self):
        """Starvation regression: an album's derived primary artist can come
        from never-played sibling tracks, so it never enters the artist queue.
        Waiting for it would leave the album unmarked (re-fetched every cycle,
        permanently occupying a batch slot) - the inline resolution must
        finish it in a single pass."""
        db = self._makeDbWithPlays()
        for trackId in ("tA2", "tA3"):   #< aM outvotes aX as alP's primary artist, but was never played
            db.repo.upsertTrack(normalizeTrackForTest(
                {"id": trackId, "name": trackId,
                 "artists": [{"id": "aM", "name": "Artist M"}], "album": _album("alP", "Album P")}))
        db.repo.commit()
        client = self._clientReturning(getAlbumTopTags=OK_EMPTY, getArtistTopTags=ROCK_TAGS)

        db._processLastfmAlbumBatch(client, "user1")

        client.getArtistTopTags.assert_any_call("Artist M", stop_event=db.lastfm_stop_event)
        self.assertEqual(db.repo.getArtistGenres("aM"), ["rock", "indie rock"])
        self.assertEqual([g["genre"] for g in db.repo.getAlbumGenres("alP")], ["rock", "indie rock"])
        self.assertTrue(all(g["inherited"] for g in db.repo.getAlbumGenres("alP")))
        conn = db.repo._conn()
        self.assertIsNotNone(conn.execute(
            "SELECT lastfm_attempted_at FROM albums WHERE id='alP'").fetchone()[0])

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


class CleanedNameRetryTestCase(LastfmWorkerBase):
    """A definitive-empty lookup for a decorated Spotify name ("Song - Radio
    Edit", "Song (feat. X)") re-asks Last.fm once with the cleaned form."""

    DECORATED_NAME = "Blood (with Foy Vance) [Drezo Remix]"

    def _makeDbWithDecoratedTrack(self):
        tracks = {
            "tD": {"id": "tD", "name": self.DECORATED_NAME,
                   "artists": [{"id": "aX", "name": "Artist X"}], "album": _album("alP", "Album P")},
        }
        entries = [{"id": "tD", "playedAt": 1000, "timePlayed": 5000}]
        return self._makeDb(tracks, entries, username="user1")

    def test_empty_result_for_a_decorated_track_retries_with_the_cleaned_name(self):
        db = self._makeDbWithDecoratedTrack()
        client = MagicMock()
        client.getTrackTopTags.side_effect = [OK_EMPTY, ROCK_TAGS]

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual([call.args for call in client.getTrackTopTags.call_args_list],
                         [("Artist X", self.DECORATED_NAME), ("Artist X", "Blood")])
        self.assertEqual(db.repo.getTrackGenres("tD"),
                         [{"genre": "rock", "inherited": False},
                          {"genre": "indie rock", "inherited": False}])

    def test_undecorated_names_get_no_retry(self):
        db = self._makeDbWithPlays()
        db.repo.markArtistsLastfmAttempted(["aX", "aY"])   #< bare artists: no inline lookups
        client = MagicMock()
        client.getTrackTopTags.return_value = OK_EMPTY

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(client.getTrackTopTags.call_count, 2)   #< one per track, no retries

    def test_transient_retry_leaves_the_track_unmarked(self):
        db = self._makeDbWithDecoratedTrack()
        client = MagicMock()
        client.getTrackTopTags.side_effect = [OK_EMPTY, FetchOutcome(OUTCOME_TRANSIENT, [])]

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(db.repo.getTrackGenres("tD"), [])
        self.assertIsNone(db.repo._conn().execute(
            "SELECT lastfm_attempted_at FROM tracks WHERE id='tD'").fetchone()[0])

    def test_aborted_retry_slot_ends_the_batch_with_the_track_unmarked(self):
        db = self._makeDbWithDecoratedTrack()
        client = MagicMock()
        client.getTrackTopTags.side_effect = [OK_EMPTY, None]   #< acquire() aborted on the retry

        processed = db._processLastfmTrackBatch(client, "user1")

        self.assertFalse(processed)
        self.assertIsNone(db.repo._conn().execute(
            "SELECT lastfm_attempted_at FROM tracks WHERE id='tD'").fetchone()[0])

    def test_empty_retry_still_falls_through_to_inheritance(self):
        db = self._makeDbWithDecoratedTrack()
        db.repo.replaceArtistGenres("aX", ["shoegaze"])
        db.repo.markArtistsLastfmAttempted(["aX"])
        client = MagicMock()
        client.getTrackTopTags.side_effect = [OK_EMPTY, OK_EMPTY]

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(db.repo.getTrackGenres("tD"),
                         [{"genre": "shoegaze", "inherited": True}])

    def test_decorated_album_names_retry_too(self):
        tracks = {
            "tE": {"id": "tE", "name": "Song E",
                   "artists": [{"id": "aX", "name": "Artist X"}],
                   "album": _album("alD", "Album D (Deluxe Edition)")},
        }
        entries = [{"id": "tE", "playedAt": 1000, "timePlayed": 5000}]
        db = self._makeDb(tracks, entries, username="user1")
        client = MagicMock()
        client.getAlbumTopTags.side_effect = [OK_EMPTY, ROCK_TAGS]

        db._processLastfmAlbumBatch(client, "user1")

        self.assertEqual([call.args for call in client.getAlbumTopTags.call_args_list],
                         [("Artist X", "Album D (Deluxe Edition)"), ("Artist X", "Album D")])
        self.assertEqual([g["genre"] for g in db.repo.getAlbumGenres("alD")],
                         ["rock", "indie rock"])
        self.assertFalse(any(g["inherited"] for g in db.repo.getAlbumGenres("alD")))


class AlbumFirstInheritanceTestCase(LastfmWorkerBase):
    """A tag-less track inherits its album's OWN genres before falling back
    to the primary artist's - album tags are the closer granularity."""

    def test_tagless_track_prefers_album_own_genres_over_artist(self):
        db = self._makeDbWithPlays()
        db.repo.replaceAlbumGenres("alP", ["progressive rock"], inherited=False)
        db.repo.markAlbumsLastfmAttempted(["alP"])
        db.repo.replaceArtistGenres("aX", ["shoegaze"])
        db.repo.markArtistsLastfmAttempted(["aX", "aY"])
        client = MagicMock()
        client.getTrackTopTags.return_value = OK_EMPTY

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(db.repo.getTrackGenres("tA"),
                         [{"genre": "progressive rock", "inherited": True}])
        client.getArtistTopTags.assert_not_called()

    def test_album_inherited_genres_do_not_cascade_to_tracks(self):
        """An album whose own lookup was empty carries artist genres as
        inherited rows - those must not masquerade as album tags for its
        tracks (the artist fallback covers that case directly)."""
        db = self._makeDbWithPlays()
        db.repo.replaceAlbumGenres("alP", ["stale artist genre"], inherited=True)
        db.repo.replaceArtistGenres("aX", ["dream pop"])
        db.repo.markArtistsLastfmAttempted(["aX", "aY"])
        client = MagicMock()
        client.getTrackTopTags.return_value = OK_EMPTY

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(db.repo.getTrackGenres("tA"),
                         [{"genre": "dream pop", "inherited": True}])


if __name__ == "__main__":
    unittest.main()
