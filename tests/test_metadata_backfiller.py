import sys
import os
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.database import Database
from Database.workers.metadata_backfiller import (
    CONSECUTIVE_FAILURE_ABORT, TOKEN_REFRESH_REAUTH_THRESHOLD,
)
from test_track_isrc_backfill import REAL_ID, _insertTrack


def runsCycles(db, count, event=None):
    """Wire a stop event so _metadataBackfillLoop runs exactly `count` cycles.

    Counts the end-of-cycle wait, for the reason runsOneCycle explains."""
    event = event if event is not None else MagicMock()
    event.is_set.return_value = False
    remaining = {"n": count}

    def wait(seconds=None):
        if seconds != db.BACKFILLER_IDLE_WAIT_SECONDS:
            return False    #< the startup delay, or a per-id pacing wait
        remaining["n"] -= 1
        return remaining["n"] <= 0
    event.wait.side_effect = wait
    return event


def runsOneCycle(db, event=None):
    """Wire a stop event so _metadataBackfillLoop runs exactly ONE cycle.

    Keyed on WHICH wait it is, rather than on how many is_set() calls a cycle
    happens to make. Every one of these used to be a fixed side_effect list
    ([False, False, True] and friends), which a per-id batch exhausted the
    moment a batch became one is_set per item - the loop's shutdown check moved
    inside the batch, so the count is now the batch size. The startup delay is a
    random 30-90s and the pacing is sub-second, so neither collides with
    BACKFILLER_IDLE_WAIT_SECONDS."""
    event = event if event is not None else MagicMock()
    event.is_set.return_value = False
    event.wait.side_effect = lambda seconds=None: seconds == db.BACKFILLER_IDLE_WAIT_SECONDS
    return event


def perIdResponder(albums=(), trackPayload=None):
    """Serves the per-id catalog endpoints from a list of album payloads.

    Spotify withdrew GET /v1/albums?ids= and GET /v1/tracks?ids= on 2026-07-31
    (403 Forbidden even for a single id, while /v1/albums/<id> answers 200), so
    a batch is now one request per id. The tests below keep the album data they
    were written with and are served it one URL at a time; an album not in the
    list answers 404, which is the per-id spelling of the null entry the bulk
    payload used for "Spotify has no data for this one"."""
    byId = {album["id"]: album for album in albums if album and album.get("id")}

    def respond(url, **kwargs):
        response = MagicMock()
        response.text = ""
        if "/v1/tracks/" in url:
            response.status_code = 200
            response.json.return_value = trackPayload if trackPayload is not None else {"external_ids": {}}
            return response
        album = byId.get(url.rsplit("/", 1)[-1])
        response.status_code = 200 if album else 404
        response.json.return_value = album or {}
        return response
    return respond


class TestMetadataBackfiller(DatabaseTestCase):
    def test_repository_get_albums_missing_metadata(self):
        import time
        from Database.repository import ALBUM_BACKFILL_RETRY_SECONDS

        db = self._makeDb({}, [])
        conn = db.repo._conn()
        now = time.time()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date) VALUES ('alb1', 'Album 1', '', 0.0)")
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb2', 'Album 2', '', 1700000000.0, 12)")
            conn.execute("INSERT INTO albums (id, name, url, release_date) VALUES ('alb3', 'Album 3', '', NULL)")
            # Restricted-style album: track count known, release date missing - must be queued
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb4', 'Album 4', '', 0.0, 5)")
            # Fabricated fallback album ids never existed on Spotify - never queued
            conn.execute("INSERT INTO albums (id, name, url, release_date) VALUES ('album_deadbeef', 'Synthetic', '', 0.0)")
            # Recently attempted - rate-limited out of the queue
            conn.execute("INSERT INTO albums (id, name, url, release_date, backfill_attempted_at) VALUES ('alb5', 'Album 5', '', 0.0, ?)", (now,))
            # Attempted longer than the retry interval ago - queued again
            conn.execute("INSERT INTO albums (id, name, url, release_date, backfill_attempted_at) VALUES ('alb6', 'Album 6', '', 0.0, ?)",
                         (now - ALBUM_BACKFILL_RETRY_SECONDS - 60,))

        missing = db.repo.getAlbumsMissingMetadata(10)
        self.assertIn("alb1", missing)
        self.assertIn("alb3", missing)
        self.assertIn("alb4", missing)
        self.assertIn("alb6", missing)
        self.assertNotIn("alb2", missing)
        self.assertNotIn("album_deadbeef", missing)
        self.assertNotIn("alb5", missing)

    def test_repository_update_album_metadata(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 0.0, 0)")

        db.repo.updateAlbumMetadata("alb1", 1600000000.0, 12, "Updated Album 1")
        row = conn.execute("SELECT name, release_date, total_tracks FROM albums WHERE id='alb1'").fetchone()
        self.assertEqual(row["name"], "Updated Album 1")
        self.assertEqual(row["release_date"], 1600000000.0)
        self.assertEqual(row["total_tracks"], 12)

    def test_repository_update_track_name(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'Album 1', '')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr1', 'Track 1', '', 'alb1')")

        db.repo.updateTrackName("tr1", "Updated Track 1")
        row = conn.execute("SELECT name, duration_ms FROM tracks WHERE id='tr1'").fetchone()
        self.assertEqual(row["name"], "Updated Track 1")
        self.assertEqual(row["duration_ms"], 0)

        db.repo.updateTrackName("tr1", "Updated Track 1", duration_ms=215000)
        row = conn.execute("SELECT duration_ms FROM tracks WHERE id='tr1'").fetchone()
        self.assertEqual(row["duration_ms"], 215000)

    def test_repository_get_albums_with_artistless_tracks(self):
        import time
        from Database.repository import ALBUM_BACKFILL_RETRY_SECONDS

        db = self._makeDb({}, [])
        conn = db.repo._conn()
        now = time.time()
        with conn:
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('art1', 'Artist 1', '')")
            # alb1: has a track without artist links -> queued
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 1700000000.0, 10)")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr1', 'Track 1', '', 'alb1')")
            # alb2: all tracks have artists -> not queued
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb2', 'Album 2', '', 1700000000.0, 10)")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr2', 'Track 2', '', 'alb2')")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('tr2', 'art1', 0)")
            # Fabricated fallback album ids never existed on Spotify -> never queued
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('album_deadbeef', 'Synthetic', '')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr3', 'Track 3', '', 'album_deadbeef')")
            # alb4: artist-less track but recently attempted -> rate-limited out
            conn.execute("INSERT INTO albums (id, name, url, backfill_attempted_at) VALUES ('alb4', 'Album 4', '', ?)", (now,))
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr4', 'Track 4', '', 'alb4')")
            # alb5: artist-less track, attempted beyond the retry interval -> queued again
            conn.execute("INSERT INTO albums (id, name, url, backfill_attempted_at) VALUES ('alb5', 'Album 5', '', ?)",
                         (now - ALBUM_BACKFILL_RETRY_SECONDS - 60,))
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr5', 'Track 5', '', 'alb5')")

        queued = db.repo.getAlbumsWithArtistlessTracks(10)
        self.assertIn("alb1", queued)
        self.assertIn("alb5", queued)
        self.assertNotIn("alb2", queued)
        self.assertNotIn("album_deadbeef", queued)
        self.assertNotIn("alb4", queued)

    def test_repository_add_missing_track_artists(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'Album 1', '')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr1', 'Track 1', '', 'alb1')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr2', 'Track 2', '', 'alb1')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('artOld', 'Existing Artist', '')")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('tr2', 'artOld', 0)")

        newArtists = [
            {"id": "artA", "name": "Artist A", "url": "http://a", "imageId": "artA"},
            {"id": "artB", "name": "Artist B", "url": "http://b", "imageId": "artB"},
        ]
        self.assertTrue(db.repo.addMissingTrackArtists("tr1", newArtists))
        rows = conn.execute(
            "SELECT artist_id FROM track_artists WHERE track_id='tr1' ORDER BY position").fetchall()
        self.assertEqual([r["artist_id"] for r in rows], ["artA", "artB"])
        self.assertEqual(conn.execute("SELECT name FROM artists WHERE id='artA'").fetchone()["name"],
                         "Artist A")

        # A track that already has links is never touched.
        self.assertFalse(db.repo.addMissingTrackArtists("tr2", newArtists))
        rows = conn.execute(
            "SELECT artist_id FROM track_artists WHERE track_id='tr2' ORDER BY position").fetchall()
        self.assertEqual([r["artist_id"] for r in rows], ["artOld"])

        # Unknown tracks and empty artist lists are quiet no-ops.
        self.assertFalse(db.repo.addMissingTrackArtists("trMissing", newArtists))
        self.assertFalse(db.repo.addMissingTrackArtists("tr1", []))

        # Existing artist rows keep their data (no blanked-payload regressions).
        db.repo.addMissingTrackArtists(
            "tr1", [{"id": "artOld", "name": "Blanked", "url": "", "imageId": "artOld"}])
        self.assertEqual(conn.execute("SELECT name FROM artists WHERE id='artOld'").fetchone()["name"],
                         "Existing Artist")

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("requests.get")
    def test_backfiller_loop_repairs_artistless_tracks(self, mock_get, mock_refresh):
        """An album with complete metadata but an artist-less track enters the
        queue through getAlbumsWithArtistlessTracks, and the album payload's
        per-track artists repair the missing links - without disturbing
        tracks whose links already exist."""
        mock_get.side_effect = perIdResponder([{
            "id": "alb1",
            "name": "Album 1",
            "release_date": "2020-05-05",
            "total_tracks": 2,
            "tracks": {"items": [
                {"id": "tr1", "name": "Track 1", "duration_ms": 1000,
                 "artists": [
                     {"id": "artA", "name": "Artist A",
                      "external_urls": {"spotify": "https://open.spotify.com/artist/artA"}},
                     {"id": "artB", "name": "Artist B",
                      "external_urls": {"spotify": "https://open.spotify.com/artist/artB"}},
                     {"name": "No Id Artist"},   #< skipped: nothing real to link
                 ]},
                {"id": "tr2", "name": "Track 2", "duration_ms": 1000,
                 "artists": [{"id": "artC", "name": "Artist C"}]},
            ]},
        }])

        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            # Complete metadata: invisible to getAlbumsMissingMetadata.
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 1600000000.0, 2)")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr1', 'Track 1', '', 'alb1')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr2', 'Track 2', '', 'alb1')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('artOld', 'Existing Artist', '')")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('tr2', 'artOld', 0)")

        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id", "client_secret": "test_secret", "refresh_token": "test_refresh"})

        Database._active_backfills.clear()
        db.backfiller_stop_event = MagicMock()
        #< loop head, the ISRC step's shutdown guard, loop head again - see the
        #  same three-value sequence in test_backfiller_loop_fetches_and_deduplicates
        runsOneCycle(db, db.backfiller_stop_event)

        db._metadataBackfillLoop()

        rows = conn.execute(
            "SELECT artist_id FROM track_artists WHERE track_id='tr1' ORDER BY position").fetchall()
        self.assertEqual([r["artist_id"] for r in rows], ["artA", "artB"])
        self.assertEqual(conn.execute("SELECT name FROM artists WHERE id='artB'").fetchone()["name"],
                         "Artist B")
        # tr2's existing link is untouched despite the payload naming artC.
        rows = conn.execute(
            "SELECT artist_id FROM track_artists WHERE track_id='tr2' ORDER BY position").fetchall()
        self.assertEqual([r["artist_id"] for r in rows], ["artOld"])
        # The album is stamped, so it leaves the repair queue until the retry window.
        self.assertIsNotNone(conn.execute(
            "SELECT backfill_attempted_at FROM albums WHERE id='alb1'").fetchone()[0])
        Database._active_backfills.clear()

    @patch("requests.get")
    def test_backfiller_loop_skips_fetching_when_disabled(self, mock_get):
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 0.0, 0)")

        db.repo.setSpotifyApiBackfillEnabled(False)
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id", "client_secret": "test_secret", "refresh_token": "test_refresh"})

        db.backfiller_stop_event = MagicMock()
        runsOneCycle(db, db.backfiller_stop_event)

        db._metadataBackfillLoop()

        mock_get.assert_not_called()
        db.getUserSpotifyCredentials.assert_not_called()
        row = conn.execute("SELECT backfill_attempted_at, release_date FROM albums WHERE id='alb1'").fetchone()
        self.assertIsNone(row["backfill_attempted_at"])
        self.assertEqual(row["release_date"], 0.0)

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("requests.get")
    def test_the_cycle_token_mint_names_the_user_it_logs_for(self, mock_get, mock_refresh):
        """_refresh_spotify_access_token's failure lines carry logUser to say
        WHOSE worker failed - the listener passes it, and the cycle's shared
        mint must too, or a refused refresh logs anonymously in a multi-user
        instance."""
        mock_get.side_effect = perIdResponder([])
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            #< one album missing metadata, so the cycle has a reason to mint
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 0.0, 0)")
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id", "client_secret": "test_secret", "refresh_token": "test_refresh"})

        Database._active_backfills.clear()
        db.backfiller_stop_event = MagicMock()
        runsOneCycle(db, db.backfiller_stop_event)

        db._metadataBackfillLoop()

        self.assertEqual(mock_refresh.call_args.kwargs.get("logUser"), db.user)
        Database._active_backfills.clear()

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("requests.get")
    def test_a_base_exception_still_releases_the_in_flight_ids(self, mock_get, mock_refresh):
        """KeyboardInterrupt/SystemExit skip `except Exception`, and the
        release used to live only on the success and Exception paths - an
        interrupted cycle leaked its claimed ids, and every later cycle
        skipped those albums as "already in flight" for the life of the
        process. The first requests.get here IS the album fetch (no tracks
        exist, so the ISRC step asks for nothing), which lands after the
        claim - exactly the window that leaked."""
        mock_get.side_effect = KeyboardInterrupt()
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 0.0, 0)")
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id", "client_secret": "test_secret", "refresh_token": "test_refresh"})

        Database._active_backfills.clear()
        db.backfiller_stop_event = MagicMock()
        runsOneCycle(db, db.backfiller_stop_event)

        with self.assertRaises(KeyboardInterrupt):
            db._metadataBackfillLoop()

        self.assertEqual(Database._active_backfills, set(),
                         "an interrupted cycle must not leave its albums claimed")

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("requests.get")
    def test_backfiller_loop_fetches_and_deduplicates(self, mock_get, mock_refresh):
        # 1. Setup mock HTTP response for Spotify v1/albums
        mock_get.side_effect = perIdResponder([
            {
                "id": "alb1",
                "name": "Updated Album Name",
                "release_date": "2020-05-05",
                "total_tracks": 10,
                "tracks": {
                    "items": [
                        {
                            "id": "tr1",
                            "name": "Updated Track Name",
                            "duration_ms": 215000
                        }
                    ]
                }
            },
            {
                # Blanked response (restricted album): names are not data and
                # must not overwrite what the importer filled from the export
                "id": "alb3",
                "name": "",
                "release_date": "2020-01-01",
                "total_tracks": 4,
                "tracks": {
                    "items": [
                        {
                            "id": "tr3",
                            "name": "",
                            "duration_ms": 100000
                        }
                    ]
                }
            }
        ])

        # Disable the default automatic backfiller from launching automatically in init by overriding startMetadataBackfiller
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
            
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 0.0, 0)")
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb2', 'Album 2', '', 0.0, 0)")
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb3', 'Export Album Name', '', 0.0, 0)")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr1', 'Track 1', '', 'alb1')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr3', 'Export Track Name', '', 'alb3')")

        # Mock getUserSpotifyCredentials to return active credentials
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id",
            "client_secret": "test_secret",
            "refresh_token": "test_refresh"
        })

        # Add alb2 to _active_backfills to simulate another thread already working on it
        Database._active_backfills.clear()
        Database._active_backfills.add("alb2")

        # Mock backfiller_stop_event to break the loop after one run
        db.backfiller_stop_event = MagicMock()
        # Side effect: loop head (do run), the ISRC step's own shutdown guard
        # (keep going), then the loop head again (exit). The ISRC step is a
        # no-op here regardless - its queue only accepts real 22-char Spotify
        # ids, and these fixtures use short fake ones.
        runsOneCycle(db, db.backfiller_stop_event)

        # Run one iteration of the backfiller
        db._metadataBackfillLoop()

        # Verify alb1 was updated (including its name and tracks)
        row = conn.execute("SELECT name, release_date, total_tracks FROM albums WHERE id='alb1'").fetchone()
        self.assertEqual(row["name"], "Updated Album Name")
        self.assertGreater(row["release_date"], 0)
        self.assertEqual(row["total_tracks"], 10)

        track_row = conn.execute("SELECT name, duration_ms FROM tracks WHERE id='tr1'").fetchone()
        self.assertEqual(track_row["name"], "Updated Track Name")
        self.assertEqual(track_row["duration_ms"], 215000)

        # Verify alb1 was stamped as attempted (rate-limits its next retry)
        self.assertIsNotNone(conn.execute("SELECT backfill_attempted_at FROM albums WHERE id='alb1'").fetchone()[0])

        # Blanked names in the response must not overwrite export-filled names,
        # but the other metadata still lands
        alb3 = conn.execute("SELECT name, release_date, total_tracks FROM albums WHERE id='alb3'").fetchone()
        self.assertEqual(alb3["name"], "Export Album Name")
        self.assertGreater(alb3["release_date"], 0)
        self.assertEqual(alb3["total_tracks"], 4)
        self.assertEqual(conn.execute("SELECT name FROM tracks WHERE id='tr3'").fetchone()["name"], "Export Track Name")

        # Verify alb2 was skipped because it was in _active_backfills
        row2 = conn.execute("SELECT release_date, total_tracks, backfill_attempted_at FROM albums WHERE id='alb2'").fetchone()
        self.assertEqual(row2["release_date"], 0.0)
        self.assertIsNone(row2["backfill_attempted_at"])

        # Verify alb2 is still in _active_backfills, but alb1 has been removed after processing
        self.assertIn("alb2", Database._active_backfills)
        self.assertNotIn("alb1", Database._active_backfills)

        # A clean cycle records success telemetry (see Database/workers/telemetry.py).
        telemetry = db._getWorkerTelemetry("spotify_api")
        self.assertEqual(telemetry["consecutive_failures"], 0)

        # Clean up
        Database._active_backfills.clear()

    def test_backfiller_loop_records_failure_telemetry(self):
        """A cycle that raises before completing (here: the missing-metadata
        query itself fails) is recorded as a failure - the loop's
        try/except/else in Database/workers/metadata_backfiller.py."""
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])

        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id", "client_secret": "test_secret", "refresh_token": "test_refresh"})
        db.repo.getAlbumsMissingMetadata = MagicMock(side_effect=RuntimeError("db unavailable"))

        db.backfiller_stop_event = MagicMock()
        #< loop head, the ISRC step's shutdown guard, loop head again - see the
        #  same three-value sequence in test_backfiller_loop_fetches_and_deduplicates.
        #  The ISRC step runs BEFORE the album queue, so it must not swallow the
        #  RuntimeError this test is checking the telemetry for.
        runsOneCycle(db, db.backfiller_stop_event)

        db._metadataBackfillLoop()

        telemetry = db._getWorkerTelemetry("spotify_api")
        self.assertEqual(telemetry["consecutive_failures"], 1)
        self.assertIn("db unavailable", telemetry["last_error"])

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value=None)
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_backfiller_loop_fallback_to_spotipy_free(self, mock_get, mock_spotipy_class, mock_refresh):
        # 1. Setup mock response for official API: return 403 Forbidden
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        # 2. Setup mock for SpotipyFree.Spotify
        mock_sp = MagicMock()
        mock_spotipy_class.return_value = mock_sp
        
        # Use a real event so we can trigger shutdown naturally
        import threading
        event = threading.Event()
        
        def mock_album_impl(album_id):
            event.set()  # Signal loop to stop after this fetch
            return {
                "id": "alb1",
                "release_date": "2021-01-01",
                "total_tracks": 8
            }
        mock_sp.album.side_effect = mock_album_impl

        # Disable the default automatic backfiller
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
            
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 0.0, 0)")

        # Mock credentials
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id",
            "client_secret": "test_secret",
            "refresh_token": "test_refresh"
        })

        db.backfiller_stop_event = event
        event.wait = MagicMock(return_value=False)

        # Run backfiller
        db._metadataBackfillLoop()

        # Verify alb1 was updated via the cookie-client fallback and stamped as attempted
        row = conn.execute("SELECT release_date, total_tracks, backfill_attempted_at FROM albums WHERE id='alb1'").fetchone()
        self.assertGreater(row["release_date"], 0)
        self.assertEqual(row["total_tracks"], 8)
        self.assertIsNotNone(row["backfill_attempted_at"])
        mock_sp.album.assert_called_once_with("alb1")
        # No cookiesFile: album() is a public lookup through spotapi's pooled
        # client, and a login here would atexit-pin one live curl session per
        # backfill cycle (the leak _pooledPublicClient documents).
        mock_spotipy_class.assert_called_once_with()

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    @patch("Database.database.logger")
    def test_backfiller_loop_403_warning_logged_only_with_debug(self, mock_logger, mock_get, mock_spotipy_class, mock_refresh):
        import threading
        # Setup mock 403 response
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        mock_sp = MagicMock()
        mock_spotipy_class.return_value = mock_sp
        mock_sp.album.return_value = {
            "id": "alb1",
            "release_date": "2021-01-01",
            "total_tracks": 8
        }

        # Disable the default automatic backfiller
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
            
        conn = db.repo._conn()

        def queueAlbum():
            """Put alb1 back in the queue before each of the three runs below.

            Each run now completes its cookie-client fallback, which fetches the
            album and takes it out of getAlbumsMissingMetadata - so runs two and
            three would find an empty queue and never reach the Web API at all.
            They used to leave it behind only because the stop-event mock was a
            call counter that fired mid-fallback."""
            with conn:
                conn.execute("INSERT OR REPLACE INTO albums (id, name, url, release_date, total_tracks, "
                             "backfill_attempted_at) VALUES ('alb1', 'Album 1', '', 0.0, 0, NULL)")

        queueAlbum()

        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id",
            "client_secret": "test_secret",
            "refresh_token": "test_refresh"
        })

        # Run with FLASK_DEBUG = "1"
        with patch.dict(os.environ, {"FLASK_DEBUG": "1"}):
            Database._active_backfills.clear()
            queueAlbum()
            db.backfiller_stop_event = runsOneCycle(db)
            db._metadataBackfillLoop()
            
            # Check warning was logged
            #< rendered, not the raw template: the status moved into an argument
            #  when the response body joined it there, and a template match
            #  would go on passing for a line that no longer says the status
            warning_calls = [
                args[0] % args[1:] for args, _ in mock_logger.warning.call_args_list
                if "Spotify Web API returned" in args[0]
            ]
            self.assertTrue(len(warning_calls) > 0)

        # Reset mock
        mock_logger.reset_mock()

        # Run with FLASK_DEBUG = "true"
        with patch.dict(os.environ, {"FLASK_DEBUG": "true"}):
            Database._active_backfills.clear()
            queueAlbum()
            db.backfiller_stop_event = runsOneCycle(db)
            db._metadataBackfillLoop()
            
            # Check warning was logged
            #< rendered, not the raw template: the status moved into an argument
            #  when the response body joined it there, and a template match
            #  would go on passing for a line that no longer says the status
            warning_calls = [
                args[0] % args[1:] for args, _ in mock_logger.warning.call_args_list
                if "Spotify Web API returned" in args[0]
            ]
            self.assertTrue(len(warning_calls) > 0)

        # Reset mock
        mock_logger.reset_mock()

        # Run with FLASK_DEBUG = "0"
        with patch.dict(os.environ, {"FLASK_DEBUG": "0"}):
            Database._active_backfills.clear()
            queueAlbum()
            db.backfiller_stop_event = runsOneCycle(db)
            db._metadataBackfillLoop()
            
            # Check warning was NOT logged
            #< rendered, not the raw template: the status moved into an argument
            #  when the response body joined it there, and a template match
            #  would go on passing for a line that no longer says the status
            warning_calls = [
                args[0] % args[1:] for args, _ in mock_logger.warning.call_args_list
                if "Spotify Web API returned" in args[0]
            ]
            self.assertEqual(len(warning_calls), 0)


class TestTheCycleSharesOneAccessToken(DatabaseTestCase):
    """The cycle does two Web-API things - one ISRC batch and one album batch -
    and each used to mint its own access token, so every 5-minute cycle spent
    two round-trips on Spotify's token endpoint to say the same thing twice.
    One refresh at the top of the cycle, handed to both."""

    def _oneCycleDb(self):
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) "
                         "VALUES ('alb1', 'Album 1', '', 0.0, 0)")
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id", "client_secret": "test_secret", "refresh_token": "test_refresh"})
        Database._active_backfills.clear()
        db.backfiller_stop_event = MagicMock()
        db.backfiller_stop_event.is_set.return_value = False
        #< exactly one cycle, keyed on WHICH wait it is rather than on how many
        #  is_set calls a cycle happens to make. It used to be a three-element
        #  side_effect, which a per-id batch exhausted the moment a batch became
        #  one is_set per item - any fixed call count is a test that breaks the
        #  next time the batch size or the pacing changes
        db.backfiller_stop_event.wait.side_effect = (
            lambda seconds=None: seconds == db.BACKFILLER_IDLE_WAIT_SECONDS)
        return db, conn

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("requests.get")
    def test_a_cycle_with_no_work_mints_nothing(self, mock_get, mock_refresh):
        """One refresh per cycle is only half the goal; the other half is none
        at all when there is nothing to spend it on. Both Web-API steps early
        out - the album queue drains for good, and the ISRC queue only refills
        every TRACK_ISRC_RETRY_SECONDS - so the steady state on a settled
        instance is an idle cycle, and minting up front spent a round-trip on
        Spotify's token endpoint every five minutes to produce a token nobody
        read."""
        db, conn = self._oneCycleDb()
        with conn:
            #< the album queue's only entry, answered, so the loop reaches its
            #  own early-out with nothing left to fetch
            conn.execute("UPDATE albums SET release_date=1.0, total_tracks=9 WHERE id='alb1'")

        db._metadataBackfillLoop()

        mock_refresh.assert_not_called()
        mock_get.assert_not_called()

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("requests.get")
    def test_one_refresh_per_cycle(self, mock_get, mock_refresh):
        mock_get.side_effect = perIdResponder()
        db, _ = self._oneCycleDb()

        db._metadataBackfillLoop()

        self.assertEqual(mock_refresh.call_count, 1)
        Database._active_backfills.clear()

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("requests.get")
    def test_an_isrc_failure_does_not_cost_the_album_backfill_its_cycle(self, mock_get, mock_refresh):
        """The ISRC step runs first on purpose - the album queue runs dry long
        before the track queue does, so putting it after that early-out would
        stop it in the steady state. That ordering makes an escaping exception
        expensive: the loop's per-cycle handler catches it and the album work
        never runs, for the whole 5-minute wait."""
        db, conn = self._oneCycleDb()
        _insertTrack(conn, "4cOdK2wGLETKBW3PvgPWqT")   #< a real-shaped id, so the ISRC queue is non-empty

        serve = perIdResponder([{"id": "alb1", "name": "Album 1", "release_date": "2020-05-05",
                                 "total_tracks": 10, "tracks": {"items": []}}])

        def byUrl(url, **kwargs):
            if "/v1/tracks/" in url:
                raise RuntimeError("connection reset")
            return serve(url, **kwargs)
        mock_get.side_effect = byUrl

        db._metadataBackfillLoop()

        self.assertEqual(conn.execute("SELECT total_tracks FROM albums WHERE id='alb1'").fetchone()[0], 10)
        Database._active_backfills.clear()

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("requests.get")
    def test_an_isrc_database_error_does_not_cost_the_album_backfill_its_cycle(self, mock_get, mock_refresh):
        """The same guarantee as the test above, for the failure the network
        guard does not cover. The ISRC step's queue read and its two writes are
        repo calls sitting outside that guard, and a locked database is the
        ordinary way one of them raises on a live instance - at which point the
        album work behind it is gone for the full five minutes, which is exactly
        what running this step first was supposed to be safe to do."""
        db, conn = self._oneCycleDb()
        _insertTrack(conn, "4cOdK2wGLETKBW3PvgPWqT")

        mock_get.side_effect = perIdResponder([
            {"id": "alb1", "name": "Album 1", "release_date": "2020-05-05",
             "total_tracks": 10, "tracks": {"items": []}}])

        with patch.object(db.repo, "getTracksMissingIsrc", side_effect=RuntimeError("database is locked")):
            db._metadataBackfillLoop()

        self.assertEqual(conn.execute("SELECT total_tracks FROM albums WHERE id='alb1'").fetchone()[0], 10)
        Database._active_backfills.clear()


class TestPerIdAlbumLookups(DatabaseTestCase):
    """The album queue lost the same bulk endpoint the ISRC queue did
    (/v1/albums?ids= answers 403 Forbidden since 2026-07-31, /v1/albums/<id>
    answers 200), which is why every cycle since has degraded to the cookie
    client. Per-id restores the Web-API path and makes partial success normal."""

    def _db(self, albumIds):
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            for albumId in albumIds:
                conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) "
                             "VALUES (?, ?, '', 0.0, 0)", (albumId, f"Album {albumId}"))
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id", "client_secret": "test_secret", "refresh_token": "test_refresh"})
        Database._active_backfills.clear()
        db.backfiller_stop_event = MagicMock()
        db.backfiller_stop_event.is_set.return_value = False
        #< exactly one cycle, keyed on WHICH wait it is rather than on how many
        #  is_set calls a cycle happens to make - a per-id batch calls is_set
        #  once per item now, so any fixed call count is a test that breaks the
        #  next time the batch size or the pacing changes
        db.backfiller_stop_event.wait.side_effect = (
            lambda seconds=None: seconds == db.BACKFILLER_IDLE_WAIT_SECONDS)
        self.addCleanup(Database._active_backfills.clear)
        return db, conn

    def _albumPayload(self, albumId):
        return {"id": albumId, "name": f"Album {albumId}", "release_date": "2020-05-05",
                "total_tracks": 7, "tracks": {"items": []}}

    def _response(self, status=200, payload=None):
        response = MagicMock()
        response.status_code = status
        response.text = ""
        response.json.return_value = payload if payload is not None else {}
        return response

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_each_album_is_asked_for_on_its_own_url(self, mock_get, mock_spotify, mock_refresh):
        db, conn = self._db(["alb1", "alb2"])
        mock_get.side_effect = lambda url, **kw: (
            self._response() if "/v1/tracks/" in url
            else self._response(payload=self._albumPayload(url.rsplit("/", 1)[-1])))

        db._metadataBackfillLoop()

        albumUrls = [c[0][0] for c in mock_get.call_args_list if "/v1/albums" in c[0][0]]
        self.assertEqual(len(albumUrls), 2)
        for url in albumUrls:
            self.assertNotIn("?ids=", url)
        mock_spotify.assert_not_called()   #< the Web API answered; no fallback

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_one_album_failing_leaves_the_others_fetched(self, mock_get, mock_spotify, mock_refresh):
        db, conn = self._db(["alb1", "alb2"])

        def byId(url, **kw):
            if "/v1/tracks/" in url:
                return self._response()
            albumId = url.rsplit("/", 1)[-1]
            if albumId == "alb1":
                return self._response(status=503)
            return self._response(payload=self._albumPayload(albumId))
        mock_get.side_effect = byId

        db._metadataBackfillLoop()

        self.assertGreater(conn.execute("SELECT release_date FROM albums WHERE id='alb2'").fetchone()[0], 0)
        #< the failed one learned nothing, so it is not stamped and comes back
        self.assertIsNone(
            conn.execute("SELECT backfill_attempted_at FROM albums WHERE id='alb1'").fetchone()[0])

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_a_web_api_that_answers_nothing_still_falls_back(self, mock_get, mock_spotify, mock_refresh):
        """Exactly today's live state - every album request 403ing - has to keep
        reaching the cookie client, which is the only reason album metadata kept
        arriving at all while this was broken."""
        db, conn = self._db(["alb1"])
        mock_get.side_effect = lambda url, **kw: self._response(
            status=200 if "/v1/tracks/" in url else 403)
        mock_spotify.return_value.album.return_value = self._albumPayload("alb1")

        db._metadataBackfillLoop()

        mock_spotify.assert_called_once_with()
        self.assertGreater(conn.execute("SELECT release_date FROM albums WHERE id='alb1'").fetchone()[0], 0)

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_a_partial_web_api_batch_does_not_also_run_the_cookie_client(self, mock_get, mock_spotify,
                                                                        mock_refresh):
        """The fallback re-fetches the whole target list, so running it after a
        partial success would ask the cookie client for albums the Web API just
        answered - paying twice and, worse, twice as often as the pacing allows."""
        db, conn = self._db(["alb1", "alb2"])

        def byId(url, **kw):
            if "/v1/tracks/" in url:
                return self._response()
            albumId = url.rsplit("/", 1)[-1]
            return (self._response(status=503) if albumId == "alb1"
                    else self._response(payload=self._albumPayload(albumId)))
        mock_get.side_effect = byId

        db._metadataBackfillLoop()

        mock_spotify.assert_not_called()


class TestAlbumLookupsShareTheQuotaStandDown(DatabaseTestCase):
    """Spotify meters ONE per-app budget for the catalog endpoints - measured
    live on 2026-08-09, when the ISRC step stood itself down for the day while
    the album step, on the same app and the same quota, would have gone on
    spending a token refresh and one refused request every cycle to rediscover
    the wall. So the wall is shared: a QUOTA_EXCEEDED on either step arms it,
    the ISRC step waits it out (it has no other source), and the album step
    skips straight to the cookie client - which answers through a different
    door and spends no Web-API quota at all."""

    def _db(self, albumIds=("alb1",)):
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            for albumId in albumIds:
                conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) "
                             "VALUES (?, ?, '', 0.0, 0)", (albumId, f"Album {albumId}"))
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id", "client_secret": "test_secret", "refresh_token": "test_refresh"})
        Database._active_backfills.clear()
        db.backfiller_stop_event = MagicMock()
        db.backfiller_stop_event.is_set.return_value = False
        #< exactly one cycle, keyed on WHICH wait it is - see TestPerIdAlbumLookups
        db.backfiller_stop_event.wait.side_effect = (
            lambda seconds=None: seconds == db.BACKFILLER_IDLE_WAIT_SECONDS)
        self.addCleanup(Database._active_backfills.clear)
        return db, conn

    def _albumPayload(self, albumId):
        return {"id": albumId, "name": f"Album {albumId}", "release_date": "2020-05-05",
                "total_tracks": 7, "tracks": {"items": []}}

    def _quotaResponse(self, retryAfter=None):
        response = MagicMock()
        response.status_code = 429
        response.text = '{"error":{"status":429,"message":"Too many requests","reason":"QUOTA_EXCEEDED"}}'
        response.headers = {"Retry-After": retryAfter} if retryAfter is not None else {}
        return response

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_a_walled_cycle_asks_the_cookie_client_not_the_web_api(self, mock_get, mock_spotify,
                                                                   mock_refresh):
        """While the wall is up, a Web-API album request buys exactly one 429,
        and the token refresh that precedes it buys nothing at all. The cookie
        client is the whole album path meanwhile - metadata keeps arriving, at
        zero quota."""
        import time
        db, conn = self._db()
        mock_spotify.return_value.album.return_value = self._albumPayload("alb1")
        db._catalogBackoffUntil = time.time() + 3600

        db._metadataBackfillLoop()

        mock_get.assert_not_called()       #< no refused request bought on the wall
        mock_refresh.assert_not_called()   #< and no token round-trip spent deciding to
        mock_spotify.assert_called_once_with()
        self.assertGreater(conn.execute("SELECT release_date FROM albums WHERE id='alb1'").fetchone()[0], 0)

    @patch("Database.database.logger")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_a_walled_cycle_does_not_claim_the_refresh_failed(self, mock_get, mock_spotify,
                                                              mock_refresh, mock_logger):
        """The fallback warning diagnoses a refresh failure. While walled, the
        token was deliberately never asked for - logging "Failed to refresh"
        would send whoever reads it off to debug credentials that are fine."""
        import time
        db, _ = self._db()
        mock_spotify.return_value.album.return_value = self._albumPayload("alb1")
        db._catalogBackoffUntil = time.time() + 3600

        db._metadataBackfillLoop()

        lines = [args[0] for args, _ in mock_logger.warning.call_args_list]
        self.assertFalse(any("Failed to refresh access token" in line for line in lines), lines)

    @patch("Database.database.logger")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_an_album_quota_refusal_arms_the_stand_down(self, mock_get, mock_spotify,
                                                        mock_refresh, mock_logger):
        """The other direction: a 429 met on the ALBUM step is the same budget
        running out, so it must arm the same wall - honouring Spotify's own
        Retry-After, exactly as the ISRC step does."""
        import time
        db, _ = self._db()
        mock_get.return_value = self._quotaResponse(retryAfter="120")
        mock_spotify.return_value.album.return_value = self._albumPayload("alb1")

        before = time.time()
        db._metadataBackfillLoop()

        self.assertGreaterEqual(getattr(db, "_catalogBackoffUntil", 0.0), before + 120)
        self.assertLess(db._catalogBackoffUntil, before + 120 + 60)
        #< the walled batch answered nothing, so the cookie client still serves
        #  this cycle - standing down is about quota, not about giving up
        mock_spotify.assert_called_once_with()

    @patch("Database.database.logger")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_a_run_of_failures_gives_up_on_the_album_batch(self, mock_get, mock_spotify,
                                                            mock_refresh, mock_logger):
        """The album step aborts on a failure streak like the ISRC step does -
        the two share _spendCatalogBatch, and this is the assertion that fails
        if one of them grows its own loop again.

        Untested on this side while the policy was duplicated, which is what a
        duplicated policy costs: the ISRC copy had five tests for its failure
        behaviour and this one had none, so a fix to either was free to miss
        the other. 5xx rather than 429, so it is the streak being asserted and
        not the stand-down that ends a batch by another route."""
        db, _ = self._db(albumIds=[f"alb{i}" for i in range(CONSECUTIVE_FAILURE_ABORT + 5)])
        failure = MagicMock()
        failure.status_code = 500
        failure.text = '{"error":{"status":500,"message":"Server error"}}'
        failure.headers = {}
        mock_get.return_value = failure
        mock_spotify.return_value.album.return_value = self._albumPayload("alb0")

        db._metadataBackfillLoop()

        self.assertEqual(mock_get.call_count, CONSECUTIVE_FAILURE_ABORT)

    @patch("Database.database.logger")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_an_album_armed_wall_stands_the_isrc_step_down_too(self, mock_get, mock_spotify,
                                                               mock_refresh, mock_logger):
        """One budget, one wall, both directions: the ISRC step must not walk
        into a quota the album step already found exhausted."""
        db, conn = self._db()
        mock_get.return_value = self._quotaResponse()
        mock_spotify.return_value.album.return_value = self._albumPayload("alb1")

        db._metadataBackfillLoop()

        _insertTrack(conn, REAL_ID)
        db._backfillTrackIsrcs(lambda: "mock_token",
                               MagicMock(is_set=MagicMock(return_value=False)))

        trackUrls = [c[0][0] for c in mock_get.call_args_list if "/v1/tracks/" in c[0][0]]
        self.assertEqual(trackUrls, [])

    @patch("Database.database.logger")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_an_isrc_armed_wall_covers_the_album_step_the_same_cycle(self, mock_get, mock_spotify,
                                                                     mock_refresh, mock_logger):
        """The ISRC step runs first, so the wall it hits is already up when the
        album step's turn comes - waiting a cycle to honour it would spend one
        more refused request for nothing."""
        db, conn = self._db()
        _insertTrack(conn, REAL_ID)
        mock_get.return_value = self._quotaResponse()
        mock_spotify.return_value.album.return_value = self._albumPayload("alb1")

        db._metadataBackfillLoop()

        albumUrls = [c[0][0] for c in mock_get.call_args_list if "/v1/albums" in c[0][0]]
        self.assertEqual(albumUrls, [])
        mock_spotify.assert_called_once_with()


class TestReauthPrompting(DatabaseTestCase):
    """When the backfiller may tell an account its Spotify authorization needs
    redoing - and, more importantly, when it may not."""

    def _db(self, credentials=True):
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) "
                         "VALUES ('alb1', 'Album 1', '', 0.0, 0)")
        _insertTrack(conn, "4cOdK2wGLETKBW3PvgPWqT")
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id", "client_secret": "test_secret",
            "refresh_token": "test_refresh"} if credentials else None)
        db.setSpotifyNeedsReauth = MagicMock()
        Database._active_backfills.clear()
        self.addCleanup(Database._active_backfills.clear)
        return db, conn

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_a_relentless_403_never_asks_the_user_to_reauthorize(self, mock_get, mock_spotify, mock_refresh):
        """The whole reason this is message-blind rather than status-blind.

        On 2026-07-31 Spotify withdrew the bulk `?ids=` catalog endpoints and
        every call answered 403 Forbidden - with a perfectly valid token, whose
        refresh succeeded, whose scopes were intact, and whose /v1/me answered
        200. Prompting on a 403 would have told all three users their Spotify
        authorization had failed, over a platform change no login could fix. One
        of them re-consented anyway; it changed nothing, because a 403 says "not
        allowed", not "not you"."""
        db, _ = self._db()
        response = MagicMock()
        response.status_code = 403
        response.text = '{"error": {"status": 403, "message": "Forbidden"}}'
        mock_get.return_value = response

        db.backfiller_stop_event = runsCycles(db, 10)
        db._metadataBackfillLoop()

        db.setSpotifyNeedsReauth.assert_not_called()

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value=None)
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_a_refresh_that_keeps_failing_does_ask(self, mock_get, mock_spotify, mock_refresh):
        """The one failure here a user CAN fix: the stored grant is dead, so
        re-authorizing is exactly the remedy."""
        db, _ = self._db()

        db.backfiller_stop_event = runsCycles(db, TOKEN_REFRESH_REAUTH_THRESHOLD)
        db._metadataBackfillLoop()

        db.setSpotifyNeedsReauth.assert_called_once_with(True)

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value=None)
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_one_failed_refresh_is_not_enough(self, mock_get, mock_spotify, mock_refresh):
        """A refresh returns None for a revoked grant AND for a DNS blip, and
        only the first is the user's to fix - so one is never enough, the same
        way the listener waits out its scope errors."""
        db, _ = self._db()

        db.backfiller_stop_event = runsCycles(db, TOKEN_REFRESH_REAUTH_THRESHOLD - 1)
        db._metadataBackfillLoop()

        db.setSpotifyNeedsReauth.assert_not_called()

    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_a_refresh_that_recovers_resets_the_streak(self, mock_get, mock_spotify):
        """Consecutive, not cumulative: an outage that ends is not a revocation.

        The lookups fail so that nothing is ever stamped and the queue stays
        populated. Serving them SUCCESSFULLY made this test vacuous - the good
        cycle drained the queue, so the cycles after it asked for no token at
        all, were correctly not counted, and the streak could never have reached
        the threshold whether it reset or not. Caught by mutation: deleting the
        reset left the whole suite green."""
        db, _ = self._db()

        def alwaysFails(url, **kwargs):
            response = MagicMock()
            response.status_code = 503
            response.text = ""
            return response
        mock_get.side_effect = alwaysFails
        tokens = [None] * (TOKEN_REFRESH_REAUTH_THRESHOLD - 1) + ["mock_token"] \
            + [None] * (TOKEN_REFRESH_REAUTH_THRESHOLD - 1)

        with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   side_effect=tokens):
            db.backfiller_stop_event = runsCycles(db, len(tokens))
            db._metadataBackfillLoop()

        db.setSpotifyNeedsReauth.assert_not_called()

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value=None)
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_the_prompt_fires_once_per_streak_not_once_per_cycle(self, mock_get, mock_spotify,
                                                                 mock_refresh):
        """setSpotifyNeedsReauth queues an email every time it is called with
        True, guard or no guard on the UPDATE - so a streak that keeps running
        must not keep asking."""
        db, _ = self._db()

        db.backfiller_stop_event = runsCycles(db, TOKEN_REFRESH_REAUTH_THRESHOLD + 5)
        db._metadataBackfillLoop()

        db.setSpotifyNeedsReauth.assert_called_once_with(True)

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value=None)
    @patch("Database.Spotify.Spotify")
    @patch("requests.get")
    def test_an_instance_with_no_credentials_is_never_prompted(self, mock_get, mock_spotify,
                                                               mock_refresh):
        """No Web-API credentials is a configuration this instance supports, not
        an authorization that lapsed. There is nothing to re-authorize."""
        db, _ = self._db(credentials=False)

        db.backfiller_stop_event = runsCycles(db, TOKEN_REFRESH_REAUTH_THRESHOLD + 2)
        db._metadataBackfillLoop()

        db.setSpotifyNeedsReauth.assert_not_called()


if __name__ == "__main__":
    unittest.main()
