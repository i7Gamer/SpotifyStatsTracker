import sys
import os
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.database import Database
from Database.repository import TRACK_ISRC_RETRY_SECONDS
from Database.workers.metadata_backfiller import (
    CONSECUTIVE_FAILURE_ABORT, ERROR_BODY_LOG_LIMIT, ISRC_QUOTA_BACKOFF_SECONDS,
    TRACK_BATCH_SIZE,
)

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

    def _insertNamed(self, conn, trackId, name, artistId="art1"):
        with conn:
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb1', 'Album 1', '')")
            conn.execute("INSERT OR IGNORE INTO artists (id, name, url) VALUES (?, ?, '')",
                         (artistId, f"Artist {artistId}"))
            conn.execute("INSERT INTO tracks (id, name, url, album_id, duration_ms) "
                         "VALUES (?, ?, '', 'alb1', 200000)", (trackId, name))
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES (?, ?, 0)",
                         (trackId, artistId))

    def test_merge_candidates_are_queued_first(self):
        """The queue is drained against an app quota that ran out after 1,791 of
        25,134 tracks, so the ORDER decides when this becomes USEFUL rather than
        when it finishes. An ISRC only changes an answer where two tracks are
        already candidates to be merged - ~900 of them - and everywhere else it
        is a value nothing reads yet.

        The pair goes in LAST so it cannot win on rowid."""
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        for i in range(10):
            self._insertNamed(conn, f"{i:022d}", f"Unique Song {i}")
        self._insertNamed(conn, "A" * 22, "Shared Song")
        self._insertNamed(conn, "B" * 22, "Shared Song")

        self.assertEqual(sorted(db.repo.getTracksMissingIsrc(2)), ["A" * 22, "B" * 22])

    def test_the_same_title_by_a_different_artist_is_not_a_candidate(self):
        """A cover is not the same recording, and titles alone are not rare:
        prioritising every shared title queued 6,099 tracks against 872 with the
        artist in the key. Both go in FIRST here, so they would win on rowid if
        the artist condition did nothing."""
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        self._insertNamed(conn, "A" * 22, "Shared Song", artistId="artA")
        self._insertNamed(conn, "B" * 22, "Shared Song", artistId="artB")   #< a cover
        self._insertNamed(conn, "C" * 22, "Other Song", artistId="artC")
        self._insertNamed(conn, "D" * 22, "Other Song", artistId="artC")    #< a genuine pair

        self.assertEqual(sorted(db.repo.getTracksMissingIsrc(2)), ["C" * 22, "D" * 22])

    def test_the_title_match_ignores_case(self):
        """SQLite compares text case-sensitively, and without LOWER() a third of
        the real candidates never sorted to the front at all - 62% coverage
        against 100%. Measured, not guessed at."""
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        for i in range(4):
            self._insertNamed(conn, f"{i:022d}", f"Unique Song {i}")
        self._insertNamed(conn, "A" * 22, "Shared Song")
        self._insertNamed(conn, "B" * 22, "SHARED SONG")

        self.assertEqual(sorted(db.repo.getTracksMissingIsrc(2)), ["A" * 22, "B" * 22])

    def test_everything_else_still_drains_behind_them(self):
        """A priority, not a filter. The rest of the catalog is still worth
        having - it just does not gate the merge decision."""
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        self._insertNamed(conn, "A" * 22, "Shared Song")
        self._insertNamed(conn, "B" * 22, "Shared Song")
        for i in range(5):
            self._insertNamed(conn, f"{i:022d}", f"Unique Song {i}")

        self.assertEqual(len(db.repo.getTracksMissingIsrc(50)), 7)

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


class TestPerIdIsrcLookups(DatabaseTestCase):
    """Spotify withdrew the bulk `?ids=` catalog endpoints on 2026-07-31 - they
    answer 403 Forbidden for a single id as readily as for fifty, while
    /v1/tracks/<id> answers 200. So a batch is now fifty independent requests,
    and partial success is the ordinary outcome rather than the exception: what
    used to be one verdict for fifty tracks is fifty verdicts."""

    def _db(self):
        with patch.object(Database, "startMetadataBackfiller"):
            return self._makeDb({}, [])

    def _response(self, status=200, isrc="USRC12345678", trackId=None):
        response = MagicMock()
        response.status_code = status
        response.text = ""
        response.json.return_value = ({"id": trackId, "external_ids": {"isrc": isrc} if isrc else {}}
                                      if status == 200 else {})
        return response

    def _attempted(self, conn, trackId):
        return conn.execute("SELECT isrc_attempted_at FROM tracks WHERE id=?", (trackId,)).fetchone()[0]

    def _isrc(self, conn, trackId):
        return conn.execute("SELECT isrc FROM tracks WHERE id=?", (trackId,)).fetchone()["isrc"]

    @patch("requests.get")
    def test_each_track_is_asked_for_on_its_own_url(self, mock_get):
        """The bulk form is gone; /v1/tracks?ids= 403s even for one id."""
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        _insertTrack(conn, REAL_ID_2)
        mock_get.side_effect = lambda url, **kw: self._response(trackId=url.rsplit("/", 1)[-1])

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        urls = [call[0][0] for call in mock_get.call_args_list]
        self.assertEqual(len(urls), 2)
        for url in urls:
            self.assertNotIn("?ids=", url)
            self.assertRegex(url, r"/v1/tracks/[A-Za-z0-9]+$")

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_one_track_failing_does_not_cost_the_others_their_turn(self, mock_get, mock_logger):
        """The whole point of going per-id: under the bulk form a single bad
        response threw away the batch. Now it throws away one track."""
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        _insertTrack(conn, REAL_ID_2)

        def byId(url, **kw):
            trackId = url.rsplit("/", 1)[-1]
            if trackId == REAL_ID:
                raise RuntimeError("connection reset")
            return self._response(trackId=trackId)
        mock_get.side_effect = byId

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(self._isrc(conn, REAL_ID_2), "USRC12345678")
        self.assertIsNotNone(self._attempted(conn, REAL_ID_2))
        #< the one that failed learned nothing, so it is stamped with nothing
        #  and stays queued for the next cycle
        self.assertIsNone(self._attempted(conn, REAL_ID))

    @patch("requests.get")
    def test_a_track_spotify_has_no_data_for_is_still_attempted(self, mock_get):
        """A 404 is a definitive answer, and the bulk form said the same thing
        with a null entry. Left unstamped it would be re-requested every cycle
        forever for a track that will never have an ISRC."""
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        mock_get.return_value = self._response(status=404)

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertIsNotNone(self._attempted(conn, REAL_ID))
        self.assertIn(self._isrc(conn, REAL_ID), (None, ""))

    @patch("requests.get")
    def test_a_track_with_no_isrc_in_its_payload_is_attempted(self, mock_get):
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        mock_get.return_value = self._response(isrc=None, trackId=REAL_ID)

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertIsNotNone(self._attempted(conn, REAL_ID))

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_transient_status_stamps_nothing_for_that_track(self, mock_get, mock_logger):
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        mock_get.return_value = self._response(status=503)

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertIsNone(self._attempted(conn, REAL_ID))

    def _rateLimited(self, retryAfter=None):
        response = MagicMock()
        response.status_code = 429
        response.text = '{"error":{"status":429,"message":"Too many requests","reason":"QUOTA_EXCEEDED"}}'
        response.headers = {"Retry-After": retryAfter} if retryAfter is not None else {}
        return response

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_quota_refusal_stands_the_step_down_across_cycles(self, mock_get, mock_logger):
        """Breaking the batch is not enough - the next cycle starts five minutes
        later and asks again.

        Measured on the live instance: Spotify answered QUOTA_EXCEEDED at 16:24
        and was still answering it 15.5 hours and 557 requests later, because
        every cycle walked straight back into it. A quota window is not a burst
        limit that clears in seconds, and retrying inside one is what keeps you
        in it."""
        db = self._db()
        conn = db.repo._conn()
        for i in range(10):
            _insertTrack(conn, f"{i:022d}")
        mock_get.return_value = self._rateLimited()

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))
        callsAfterFirstCycle = mock_get.call_count

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(mock_get.call_count, callsAfterFirstCycle,
                         "the second cycle asked again while still quota-limited")

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_the_stand_down_lifts_when_the_window_passes(self, mock_get, mock_logger):
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        mock_get.return_value = self._rateLimited(retryAfter="1")

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))
        mock_get.return_value = self._response(trackId=REAL_ID)

        #< the window elapses; nothing about this test depends on real time
        db._isrcBackoffUntil = 0.0
        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(self._isrc(conn, REAL_ID), "USRC12345678")

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_spotifys_own_retry_after_is_honoured_over_the_default(self, mock_get, mock_logger):
        """It is Spotify's window, not ours to guess at."""
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        mock_get.return_value = self._rateLimited(retryAfter="120")

        before = time.time()
        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertGreaterEqual(db._isrcBackoffUntil, before + 120)
        self.assertLess(db._isrcBackoffUntil, before + 120 + 60)

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_missing_retry_after_falls_back_to_the_default_stand_down(self, mock_get, mock_logger):
        """Spotify does not always send one, and "no header" must not read as
        "retry immediately"."""
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        mock_get.return_value = self._rateLimited()

        before = time.time()
        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertGreaterEqual(db._isrcBackoffUntil, before + ISRC_QUOTA_BACKOFF_SECONDS)

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_stood_down_step_mints_no_token(self, mock_get, mock_logger):
        """Standing down has to happen before the token, or a quota wall still
        costs a refresh round-trip every five minutes for as long as it lasts."""
        db = self._db()
        conn = db.repo._conn()
        _insertTrack(conn, REAL_ID)
        mock_get.return_value = self._rateLimited()
        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        minted = []
        db._backfillTrackIsrcs(lambda: minted.append(1) or "mock_token",
                               MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(minted, [])

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_rate_limit_stops_the_batch_rather_than_finishing_it(self, mock_get, mock_logger):
        """50x the requests means 50x the ways to make a rate limit worse. A 429
        is the API asking for less, so the rest of the batch waits for the next
        cycle instead of being spent proving the point."""
        db = self._db()
        conn = db.repo._conn()
        for i in range(10):
            _insertTrack(conn, f"{i:022d}")
        mock_get.return_value = self._response(status=429)

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(mock_get.call_count, 1)

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_an_aborted_batch_reports_what_it_asked_for_not_what_it_claimed(self, mock_get, mock_logger):
        """The count has to be failures, not "everything not stamped".

        When the breaker trips, the rest of the batch is never asked about at
        all - so counting it as failed says "50 of 50 failed" over five actual
        failures. That is the same shape of misleading log that made the 403
        take a day to find, in the very line added to stop it happening again."""
        db = self._db()
        conn = db.repo._conn()
        for i in range(30):
            _insertTrack(conn, f"{i:022d}")
        mock_get.return_value = self._response(status=403)

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        lines = [args[0] % args[1:] for args, _ in mock_logger.warning.call_args_list]
        failureLines = [line for line in lines if "ISRC lookup failed for" in line]
        self.assertTrue(failureLines, lines)
        self.assertIn(f"failed for {CONSECUTIVE_FAILURE_ABORT} of", failureLines[0])

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_systemic_failure_stops_the_batch_early(self, mock_get, mock_logger):
        """The failure this was written under: every request 403ing. Without a
        breaker each worker spends a full batch of futile requests every cycle,
        forever, and three of them do it at once."""
        db = self._db()
        conn = db.repo._conn()
        for i in range(30):
            _insertTrack(conn, f"{i:022d}")
        mock_get.return_value = self._response(status=403)

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(mock_get.call_count, CONSECUTIVE_FAILURE_ABORT)

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_recovered_failure_does_not_count_toward_the_breaker(self, mock_get, mock_logger):
        """Consecutive, not cumulative - an intermittent 503 among successes is
        the API being busy, not the API being gone."""
        db = self._db()
        conn = db.repo._conn()
        for i in range(12):
            _insertTrack(conn, f"{i:022d}")
        calls = {"n": 0}

        def alternating(url, **kw):
            calls["n"] += 1
            trackId = url.rsplit("/", 1)[-1]
            return self._response(status=503) if calls["n"] % 2 else self._response(trackId=trackId)
        mock_get.side_effect = alternating

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(mock_get.call_count, 12)

    @patch("requests.get")
    def test_a_shutdown_mid_batch_stops_between_tracks(self, mock_get):
        """A batch is now up to fifty requests long, so shutdown has to be able
        to land inside one - waiting out the whole batch is the kind of delay
        the 45-second stop grace was measured without."""
        db = self._db()
        conn = db.repo._conn()
        for i in range(10):
            _insertTrack(conn, f"{i:022d}")
        mock_get.side_effect = lambda url, **kw: self._response(trackId=url.rsplit("/", 1)[-1])

        stop = MagicMock()
        #< not set at the gate, set once the third track is being considered
        stop.is_set.side_effect = [False] + [False, False] + [True] * 40

        db._backfillTrackIsrcs(_token(), stop)

        self.assertLess(mock_get.call_count, 10)
        #< what it did fetch before stopping is still recorded: a shutdown is
        #  not a failure, and re-fetching it next start is wasted work
        self.assertGreater(
            conn.execute("SELECT COUNT(*) FROM tracks WHERE isrc_attempted_at IS NOT NULL").fetchone()[0], 0)


class TestConcurrentIsrcBatches(DatabaseTestCase):
    """The catalog is shared - one tracks table for the whole instance - so
    every user's backfiller drains the SAME ISRC queue. Albums have carried
    process-wide claims since they had this problem (Database._active_backfills,
    and a pool four times the batch size so claiming does not starve anyone);
    the ISRC queue had neither, and asked for exactly one batch's worth of ids,
    so two workers in flight at once requested the same 50 - three API calls to
    make one batch of progress, and three writes of the same rows."""

    def _db(self):
        with patch.object(Database, "startMetadataBackfiller"):
            return self._makeDb({}, [])

    def _okResponse(self):
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {"external_ids": {}}
        return response

    def _requestedIds(self, mock_get):
        #< one request per id since the bulk `?ids=` form was withdrawn, so the
        #  batch is read off the URLs rather than out of one query string
        return [call[0][0].rsplit("/", 1)[-1] for call in mock_get.call_args_list]

    def _insertTracks(self, conn, count):
        for i in range(count):
            _insertTrack(conn, f"{i:022d}")

    def _heldByAnotherWorker(self, db, count):
        """Ids another user's backfiller is holding right now."""
        held = set(db.repo.getTracksMissingIsrc(count))
        Database._active_isrc_backfills.update(held)
        return held

    @patch("requests.get")
    def test_a_batch_skips_ids_another_worker_holds(self, mock_get):
        db = self._db()
        conn = db.repo._conn()
        self._insertTracks(conn, 120)
        mock_get.return_value = self._okResponse()
        held = self._heldByAnotherWorker(db, TRACK_BATCH_SIZE)

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(set(self._requestedIds(mock_get)) & held, set())

    @patch("requests.get")
    def test_the_batch_is_claimed_while_its_request_is_in_flight(self, mock_get):
        """The other half of the mechanism, and the half a skip test cannot
        reach: honouring another worker's claims only ever helps if this worker
        publishes its own. Drop the registration and every worker still skips
        perfectly - over a set nothing ever puts anything in.

        Asserted from inside the request because that is the whole window the
        claim exists for: before it there is nothing to see, and after it the
        ids have been released."""
        db = self._db()
        conn = db.repo._conn()
        self._insertTracks(conn, 60)

        claimedDuringRequest = {}

        def duringRequest(url, **kwargs):
            claimedDuringRequest["ids"] = set(Database._active_isrc_backfills)
            return self._okResponse()
        mock_get.side_effect = duringRequest

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(claimedDuringRequest["ids"], set(self._requestedIds(mock_get)))

    @patch("requests.get")
    def test_a_full_batch_is_still_available_while_another_worker_holds_one(self, mock_get):
        """Skipping in-flight ids is only half of it. Reading exactly one
        batch's worth would mean the second worker found nothing left and sat
        the cycle out - correct, but it throws away the parallelism that having
        three backfillers is supposed to buy. The pool is read wider than the
        batch for the same reason the album queue is."""
        db = self._db()
        conn = db.repo._conn()
        self._insertTracks(conn, 120)
        mock_get.return_value = self._okResponse()
        self._heldByAnotherWorker(db, TRACK_BATCH_SIZE)

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(len(self._requestedIds(mock_get)), TRACK_BATCH_SIZE)

    @patch("requests.get")
    def test_a_fully_claimed_queue_makes_no_request(self, mock_get):
        db = self._db()
        conn = db.repo._conn()
        self._insertTracks(conn, 20)
        minted = []
        self._heldByAnotherWorker(db, 20)

        db._backfillTrackIsrcs(lambda: minted.append(1) or "mock_token",
                               MagicMock(is_set=MagicMock(return_value=False)))

        mock_get.assert_not_called()
        self.assertEqual(minted, [])   #< and no token spent deciding that

    @patch("requests.get")
    def test_a_finished_batch_releases_its_ids(self, mock_get):
        db = self._db()
        conn = db.repo._conn()
        self._insertTracks(conn, 60)
        mock_get.return_value = self._okResponse()

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(Database._active_isrc_backfills, set())

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_failed_batch_releases_its_ids_too(self, mock_get, mock_logger):
        """A held id that is never released is worse than one never claimed: a
        failure stamps nothing, so the ids stay queued, and every later cycle in
        this process skips them as "already in flight". The backfill then
        quietly stops making progress on them for the life of the process."""
        db = self._db()
        conn = db.repo._conn()
        self._insertTracks(conn, 60)

        for failure in (lambda **kw: RuntimeError("connection reset"), None):
            with self.subTest(failure="raise" if failure else "non-200"):
                Database._active_isrc_backfills.clear()
                if failure:
                    mock_get.side_effect = RuntimeError("connection reset")
                else:
                    mock_get.side_effect = None
                    response = MagicMock()
                    response.status_code = 429
                    mock_get.return_value = response

                db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

                self.assertEqual(Database._active_isrc_backfills, set())

    @patch("requests.get")
    def test_a_queue_read_that_raises_releases_nothing_and_says_so(self, mock_get):
        """The release runs in a finally, so it also runs on the path where
        nothing was ever claimed."""
        db = self._db()
        with patch.object(db.repo, "getTracksMissingIsrc", side_effect=RuntimeError("database is locked")):
            db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(Database._active_isrc_backfills, set())
        mock_get.assert_not_called()


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

        def byId(url, **kwargs):
            response = MagicMock()
            response.text = ""
            if url.rsplit("/", 1)[-1] == REAL_ID:
                response.status_code = 200
                response.json.return_value = {"external_ids": {"isrc": "USRC12345678"}}
            else:
                response.status_code = 404   #< Spotify has no data for the second id
            return response
        mock_get.side_effect = byId

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
        """The cap outlived the bulk endpoint it was named for: it is now how
        many single requests a cycle spends, which is what keeps the drain rate
        and the request rate where they were measured."""
        db = self._db()
        conn = db.repo._conn()
        for i in range(TRACK_BATCH_SIZE + 10):
            _insertTrack(conn, f"{i:022d}".replace("-", "0"))

        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {"external_ids": {}}
        mock_get.return_value = response

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        self.assertEqual(mock_get.call_count, TRACK_BATCH_SIZE)

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

    def _failureLine(self, mock_logger, mock_get, status=403, body=""):
        response = MagicMock()
        response.status_code = status
        response.text = body
        mock_get.return_value = response
        db = self._db()
        _insertTrack(db.repo._conn(), REAL_ID)

        db._backfillTrackIsrcs(_token(), MagicMock(is_set=MagicMock(return_value=False)))

        lines = [args[0] % args[1:] for args, _ in mock_logger.warning.call_args_list]
        matching = [line for line in lines if str(status) in line]
        self.assertTrue(matching, lines)
        return matching[0]

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_failed_response_logs_what_spotify_said(self, mock_get, mock_logger):
        """The status alone is not a diagnosis. These endpoints take no scope at
        all, so a 403 on them is not "you asked for too much" - Spotify answers
        "Insufficient client scope", which is what tells you the token is being
        rejected wholesale rather than the request being malformed. Two very
        different problems behind one number, and the log carried only the
        number: the live instance's 403 could only be read at all by digging out
        a body the LISTENER had logged, on a different endpoint, weeks earlier."""
        line = self._failureLine(
            mock_logger, mock_get,
            body='{\n  "error" : {\n    "status" : 403,\n    "message" : "Insufficient client scope"\n  }\n}')

        self.assertIn("Insufficient client scope", line)

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_the_body_is_collapsed_onto_one_line(self, mock_get, mock_logger):
        """Spotify pretty-prints its error objects over five lines, and a log
        this size is read through grep - where a five-line entry means the
        message is invisible unless you already knew to ask for context."""
        line = self._failureLine(mock_logger, mock_get, body='{\n  "error" : {\n    "status" : 403\n  }\n}')

        self.assertNotIn("\n", line)

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_body_that_is_not_an_error_object_is_bounded(self, mock_get, mock_logger):
        """A proxy or a captive portal in front of the API answers with a page,
        not an object, and a log line is not the place to discover that."""
        line = self._failureLine(mock_logger, mock_get, status=502, body="<html>" + ("x" * 20000))

        self.assertLess(len(line), ERROR_BODY_LOG_LIMIT * 2)

    @patch("Database.database.logger")
    @patch("requests.get")
    def test_a_response_with_no_body_still_logs_its_status(self, mock_get, mock_logger):
        self.assertIn("429", self._failureLine(mock_logger, mock_get, status=429, body=""))

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
