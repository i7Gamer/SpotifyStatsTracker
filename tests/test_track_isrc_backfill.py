import sys
import os
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.database import Database
from Database.repository import TRACK_ISRC_RETRY_SECONDS
from Database.workers.metadata_backfiller import SPOTIFY_BULK_TRACK_LIMIT

# A 22-character alphanumeric id is what looksLikeSpotifyTrackId accepts; the
# export importer's surrogate is a bare 32-char md5. Both shapes appear below
# because the queue has to tell them apart.
REAL_ID = "3xMBguKPth2j8YPuhmJHSO"
REAL_ID_2 = "6YcwCi4Guhw3TEfnSH9ROX"
FABRICATED_ID = "0123456789abcdef0123456789abcdef"


def _insertTrack(conn, trackId, isrc=None, attemptedAt=None):
    with conn:
        conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb1', 'Album 1', '')")
        conn.execute(
            "INSERT INTO tracks (id, name, url, album_id, isrc, isrc_attempted_at) "
            "VALUES (?, ?, '', 'alb1', ?, ?)",
            (trackId, f"Track {trackId[:4]}", isrc, attemptedAt),
        )


class TestUpsertTrackIsrcPreservation(DatabaseTestCase):
    """The backfill is only worth running if the value survives the track's
    next play. Every live ingest path (pathfinder, the export importer) supplies
    isrc="", so an unconditional isrc=excluded.isrc wipes it."""

    def test_blank_isrc_does_not_wipe_a_stored_one(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()

        track = normalizeTrackForTest({"id": REAL_ID, "name": "Song", "artists": []})
        track["isrc"] = "USRC12345678"
        db.repo.upsertTrack(track)
        db.repo.commit()
        self.assertEqual(conn.execute("SELECT isrc FROM tracks WHERE id=?", (REAL_ID,)).fetchone()["isrc"],
                         "USRC12345678")

        # The same track played again, through a source that has no ISRC.
        blanked = normalizeTrackForTest({"id": REAL_ID, "name": "Song", "artists": []})
        blanked["isrc"] = ""
        db.repo.upsertTrack(blanked)
        db.repo.commit()
        self.assertEqual(conn.execute("SELECT isrc FROM tracks WHERE id=?", (REAL_ID,)).fetchone()["isrc"],
                         "USRC12345678")

    def test_real_isrc_still_overwrites(self):
        """Blank-isn't-data must not freeze the column: a genuine correction
        from a later lookup still lands."""
        db = self._makeDb({}, [])
        conn = db.repo._conn()

        track = normalizeTrackForTest({"id": REAL_ID, "name": "Song", "artists": []})
        track["isrc"] = "USRC00000000"
        db.repo.upsertTrack(track)
        track["isrc"] = "USRC99999999"
        db.repo.upsertTrack(track)
        db.repo.commit()
        self.assertEqual(conn.execute("SELECT isrc FROM tracks WHERE id=?", (REAL_ID,)).fetchone()["isrc"],
                         "USRC99999999")


class TestIsrcQueue(DatabaseTestCase):
    def test_queues_only_real_ids_without_an_isrc(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        now = time.time()

        _insertTrack(conn, REAL_ID)                                  #< NULL isrc -> queued
        _insertTrack(conn, REAL_ID_2, isrc="")                       #< empty isrc -> queued
        _insertTrack(conn, "1AAAAAAAAAAAAAAAAAAAAA", isrc="USRC1")   #< already known -> not queued
        _insertTrack(conn, FABRICATED_ID)                            #< never existed on Spotify -> not queued
        _insertTrack(conn, "2AAAAAAAAAAAAAAAAAAAAA", attemptedAt=now)  #< rate-limited
        _insertTrack(conn, "3AAAAAAAAAAAAAAAAAAAAA",
                     attemptedAt=now - TRACK_ISRC_RETRY_SECONDS - 60)   #< window elapsed -> queued again

        queued = db.repo.getTracksMissingIsrc(50)
        self.assertIn(REAL_ID, queued)
        self.assertIn(REAL_ID_2, queued)
        self.assertIn("3AAAAAAAAAAAAAAAAAAAAA", queued)
        self.assertNotIn("1AAAAAAAAAAAAAAAAAAAAA", queued)
        self.assertNotIn(FABRICATED_ID, queued)
        self.assertNotIn("2AAAAAAAAAAAAAAAAAAAAA", queued)

    def test_respects_the_limit(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        for i in range(5):
            _insertTrack(conn, f"{i}AAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(len(db.repo.getTracksMissingIsrc(3)), 3)

    def test_update_writes_isrcs_and_ignores_blanks(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        _insertTrack(conn, REAL_ID_2, isrc="USRCKEEPME01")

        db.repo.updateTrackIsrcs({REAL_ID: "USRC12345678", REAL_ID_2: ""})

        self.assertEqual(conn.execute("SELECT isrc FROM tracks WHERE id=?", (REAL_ID,)).fetchone()["isrc"],
                         "USRC12345678")
        self.assertEqual(conn.execute("SELECT isrc FROM tracks WHERE id=?", (REAL_ID_2,)).fetchone()["isrc"],
                         "USRCKEEPME01")

    def test_update_with_nothing_to_write_is_a_noop(self):
        db = self._makeDb({}, [])
        db.repo.updateTrackIsrcs({})
        db.repo.updateTrackIsrcs({REAL_ID: ""})

    def test_mark_attempted_stamps_every_id(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        _insertTrack(conn, REAL_ID_2)

        db.repo.markTracksIsrcAttempted([REAL_ID, REAL_ID_2])

        for trackId in (REAL_ID, REAL_ID_2):
            self.assertIsNotNone(
                conn.execute("SELECT isrc_attempted_at FROM tracks WHERE id=?", (trackId,)).fetchone()[0])
        # And they leave the queue.
        self.assertEqual(db.repo.getTracksMissingIsrc(50), [])

    def test_mark_attempted_with_no_ids_is_a_noop(self):
        db = self._makeDb({}, [])
        db.repo.markTracksIsrcAttempted([])


#< the step takes a PROVIDER, not a token, so that a cycle with nothing to do
#  mints nothing (see _CycleAccessToken). Tests that only care about the happy
#  path hand it this.
def _token(value="mock_token"):
    return lambda: value


class TestIsrcBackfillWorker(DatabaseTestCase):
    def _db(self):
        with patch.object(Database, "startMetadataBackfiller"):
            return self._makeDb({}, [])

    @patch("requests.get")
    def test_fetches_and_stores_isrcs(self, mock_get):
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        _insertTrack(conn, REAL_ID_2)

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"tracks": [
            {"id": REAL_ID, "external_ids": {"isrc": "USRC12345678"}},
            None,   #< Spotify has no data for the second id
        ]}
        mock_get.return_value = response

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(conn.execute("SELECT isrc FROM tracks WHERE id=?", (REAL_ID,)).fetchone()["isrc"],
                         "USRC12345678")
        # Both ids are stamped - a null entry re-queued every cycle would hammer
        # the API forever for a track Spotify simply has no ISRC for.
        for trackId in (REAL_ID, REAL_ID_2):
            self.assertIsNotNone(
                conn.execute("SELECT isrc_attempted_at FROM tracks WHERE id=?", (trackId,)).fetchone()[0])

    @patch("requests.get")
    def test_batches_at_the_web_api_id_cap(self, mock_get):
        db = self._db()
        conn = db.repo._conn()
        for i in range(SPOTIFY_BULK_TRACK_LIMIT + 10):
            _insertTrack(conn, f"{i:022d}".replace("-", "0"))

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"tracks": []}
        mock_get.return_value = response

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        requestedIds = mock_get.call_args[0][0].split("ids=")[1].split(",")
        self.assertEqual(len(requestedIds), SPOTIFY_BULK_TRACK_LIMIT)

    @patch("requests.get")
    def test_without_an_access_token_it_does_nothing(self, mock_get):
        """No token means either no Web-API credentials or a refresh that
        failed. The cookie/pathfinder client does not expose ISRCs at all, so
        there is no fallback to attempt - and nothing may be stamped as
        attempted, or the ids would be skipped once credentials DO arrive."""
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)

        db._backfillTrackIsrcs(_token(None), MagicMock(is_set=MagicMock(return_value=False)))

        mock_get.assert_not_called()
        self.assertIsNone(
            conn.execute("SELECT isrc_attempted_at FROM tracks WHERE id=?", (REAL_ID,)).fetchone()[0])

    @patch("requests.get")
    def test_a_failed_response_stamps_nothing(self, mock_get):
        """A 429/500 is not a definitive "no ISRC" - marking those attempted
        would lose the tracks for the whole retry window over a transient."""
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)

        response = MagicMock()
        response.status_code = 429
        mock_get.return_value = response

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertIsNone(
            conn.execute("SELECT isrc_attempted_at FROM tracks WHERE id=?", (REAL_ID,)).fetchone()[0])

    @patch("requests.get")
    def test_empty_queue_makes_no_request(self, mock_get):
        db = self._db()
        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))
        mock_get.assert_not_called()

    @patch("requests.get")
    def test_a_set_stop_event_skips_the_fetch(self, mock_get):
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=True)))

        mock_get.assert_not_called()

    @patch("requests.get")
    def test_a_network_error_is_contained(self, mock_get):
        """A raise here does not belong to this batch alone: the ISRC step runs
        FIRST in the cycle (ahead of the album queue's early-out, deliberately),
        so an escaping exception is caught by the loop's per-cycle handler and
        takes the album and artist-link repair down with it for the whole
        5-minute wait - over a DNS blip on an unrelated request.

        Nothing is stamped either, for the same reason a 429 stamps nothing."""
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        mock_get.side_effect = RuntimeError("connection reset")

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertIsNone(
            conn.execute("SELECT isrc_attempted_at FROM tracks WHERE id=?", (REAL_ID,)).fetchone()[0])

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_failed_response_is_reported_outside_debug(self, mock_get, mock_logger):
        """The album backfill can afford to whisper a non-200: it falls back to
        the cookie client and says so on the way. This step has no fallback by
        design, so a non-200 is the whole story - and behind a FLASK_DEBUG gate
        it is a story nobody hears. A persistent 401 then looks exactly like
        "still draining": the queue is untouched cycle after cycle and the log
        says nothing at all, which is how the live instance sat at 0 ISRCs
        without a single line to explain it."""
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)

        response = MagicMock()
        response.status_code = 401
        mock_get.return_value = response

        with patch("Database.workers.metadata_backfiller.flaskDebugEnabled", return_value=False):
            db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        warnings = [args[0] % args[1:] for args, _ in mock_logger.warning.call_args_list]
        self.assertTrue(any("401" in line for line in warnings), warnings)

    @patch("requests.get")
    def test_an_empty_queue_mints_no_token(self, mock_get):
        """Laziness is the point of the provider: the album queue drains for
        good and the ISRC queue only refills every TRACK_ISRC_RETRY_SECONDS, so
        the steady state is a cycle with nothing to do. Minting up front spent a
        round-trip on Spotify's token endpoint every five minutes, forever, for
        a token neither step would use."""
        db = self._db()
        minted = []

        db._backfillTrackIsrcs(lambda: minted.append(1) or "mock_token",
                               MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(minted, [])
        mock_get.assert_not_called()

    @patch("requests.get")
    def test_a_set_stop_event_mints_no_token(self, mock_get):
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        minted = []

        db._backfillTrackIsrcs(lambda: minted.append(1) or "mock_token",
                               MagicMock(is_set=MagicMock(return_value=True)))

        self.assertEqual(minted, [])
        mock_get.assert_not_called()

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_database_error_is_contained_too(self, mock_get, mock_logger):
        """"Nothing raises out of here" has to hold for the repo calls, not just
        the request. The queue read and the two writes are as capable of raising
        as requests.get is - a locked database is the ordinary way it happens on
        a live instance - and they cost exactly the same thing when they do: the
        loop's per-cycle handler catches it and the album and artist-link repair
        lose their whole turn."""
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"tracks": [{"id": REAL_ID, "external_ids": {"isrc": "USRC12345678"}}]}
        mock_get.return_value = response

        for method in ("getTracksMissingIsrc", "updateTrackIsrcs", "markTracksIsrcAttempted"):
            with self.subTest(method=method):
                with patch.object(db.repo, method, side_effect=RuntimeError("database is locked")):
                    db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_payload_that_is_not_an_object_is_contained(self, mock_get, mock_logger):
        """resp.json() succeeding does not make the result a dict - a proxy or
        an error page can answer 200 with a JSON list. .get() on it raises, and
        that raise is outside the request's own guard."""
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = ["not", "an", "object"]
        mock_get.return_value = response

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertIsNone(
            conn.execute("SELECT isrc_attempted_at FROM tracks WHERE id=?", (REAL_ID,)).fetchone()[0])
