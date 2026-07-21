import datetime
import json
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
from conftest import DatabaseTestCase

import app as appModule
from app import SpotifyDashboardApp
from _app_factory import AppTestCase
import Database.utils as utilsModule
from Database.Migrators.migrate1_12_0 import Migrator as Migrator_1_12_0


class TestWrappedCacheSchema(DatabaseTestCase):
    def test_migration_creates_table_and_updates_version(self):
        # DatabaseTestCase setups a temp database and runs all schemas/migrations up to current.
        # Let's verify the user_wrapped table exists.
        db = self._makeDb({}, [])
        conn = db.repo.connection()
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_wrapped'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "user_wrapped")


class TestWrappedCacheRepository(DatabaseTestCase):
    def test_repo_cache_operations(self):
        db = self._makeDb({}, [])
        repo = db.repo
        username = "testuser"
        year = 2026

        # Test getMaxPlayedAtInPeriod
        # insert some dummy plays
        repo.upsertTrack({
            "id": "t1", "name": "Song 1", "url": "u1", "imageId": "img1", "duration": 30000,
            "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album 1", "url": "u", "imageId": "i", "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "artists": [{"id": "art1", "name": "Artist 1", "url": "u", "imageId": "i"}]
        })
        
        # Add plays
        repo.insertPlay(username, "t1", 1774000000, 30000, "listener") # 2026-03-04
        repo.insertPlay(username, "t1", 1775000000, 30000, "listener") # later

        max_play = repo.getMaxPlayedAtInPeriod(username, 1767225600, 1798761600) # Year 2026 range
        self.assertEqual(max_play, 1775000000)

        # Test save and fetch from cache
        dummy_data = {
            "calculated_at": time.time(),
            "max_played_at": 1775000000,
            "total_plays": 2,
            "total_ms": 60000,
            "longest_streak": 1,
            "peak_day": "2026-03-04",
            "peak_plays": 1,
            "unique_songs": 1,
            "unique_artists": 1,
            "discovered_songs": 1,
            "discovered_artists": 1,
            "time_series_day": "[]",
            "time_series_week": "[]",
            "time_series_month": "[]",
            "top_songs": "[]",
            "top_artists": "[]",
            "top_albums": "[]",
            "discovered_songs_list": "[]",
            "discovered_artists_list": "[]",
            "discovered_albums_list": "[]",
        }

        repo.saveCachedWrapped(username, year, dummy_data)
        
        cached_max = repo.getCachedWrappedMaxPlayedAt(username, year)
        self.assertEqual(cached_max, 1775000000)

        cached_data = repo.getCachedWrapped(username, year)
        self.assertIsNotNone(cached_data)
        self.assertEqual(cached_data["total_plays"], 2)
        self.assertEqual(cached_data["peak_day"], "2026-03-04")

        # Test delete
        repo.deleteUserWrapped(username, year)
        self.assertIsNone(repo.getCachedWrapped(username, year))


class TestWrappedBackgroundWorker(DatabaseTestCase):
    def test_worker_triggers_recalculation(self):
        db = self._makeDb({}, [])
        # Insert a play in 2026
        db.repo.upsertTrack({
            "id": "t1", "name": "Song 1", "url": "u1", "imageId": "img1", "duration": 30000,
            "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album 1", "url": "u", "imageId": "i", "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "artists": [{"id": "art1", "name": "Artist 1", "url": "u", "imageId": "i"}]
        })
        db.repo.insertPlay(db.user, "t1", 1774000000, 30000, "listener")

        # Clear existing cache if any
        db.repo.deleteUserWrapped(db.user, 2026)

        # Run checkAndRecalculate (bypass the real WRAPPED_YEAR_DELAY_SECONDS
        # breathing-room wait between recalculated years - see the same patch
        # in test_boundary_play_does_not_cause_perpetual_recalculation below)
        with patch.object(db.wrapped_stop_event, "wait", return_value=False):
            db._checkAndRecalculateWrapped()

        # Check if cache is now populated
        cached = db.repo.getCachedWrapped(db.user, 2026)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["total_plays"], 1)
        self.assertEqual(cached["max_played_at"], 1774000000)

    def test_recalculation_log_uses_readable_timestamps(self):
        """cached max/actual max should be logged as ISO timestamps, not raw
        epoch floats, so the log is readable without doing math in your head."""
        db = self._makeDb({}, [])
        db.repo.upsertTrack({
            "id": "t1", "name": "Song 1", "url": "u1", "imageId": "img1", "duration": 30000,
            "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album 1", "url": "u", "imageId": "i", "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "artists": [{"id": "art1", "name": "Artist 1", "url": "u", "imageId": "i"}]
        })
        db.repo.insertPlay(db.user, "t1", 1774000000, 30000, "listener")  # somewhere in 2026
        db.repo.deleteUserWrapped(db.user, 2026)

        with self.assertLogs("Database.database", level="INFO") as cm, \
             patch.object(db.wrapped_stop_event, "wait", return_value=False):
            db._checkAndRecalculateWrapped()

        recalcLines = [line for line in cm.output if "Recalculating wrapped" in line]
        self.assertEqual(len(recalcLines), 1)
        self.assertIn("cached max: none", recalcLines[0])   #< no prior cache yet
        self.assertRegex(recalcLines[0], r"actual max: 2026-\d{2}-\d{2}T")
        self.assertNotIn("1774000000", recalcLines[0])

    def test_worker_detects_historical_inserts(self):
        db = self._makeDb({}, [])
        # Insert plays in 2026: one late play
        db.repo.upsertTrack({
            "id": "t1", "name": "Song 1", "url": "u1", "imageId": "img1", "duration": 30000,
            "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album 1", "url": "u", "imageId": "i", "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "artists": [{"id": "art1", "name": "Artist 1", "url": "u", "imageId": "i"}]
        })
        db.repo.insertPlay(db.user, "t1", 1775000000, 30000, "listener") # late play

        with patch.object(db.wrapped_stop_event, "wait", return_value=False):
            # Check and populate cache
            db._checkAndRecalculateWrapped()
            cached1 = db.repo.getCachedWrapped(db.user, 2026)
            self.assertEqual(cached1["total_plays"], 1)
            self.assertEqual(cached1["max_played_at"], 1775000000)

            # Now insert an earlier play (historical, in-between) in 2026
            db.repo.insertPlay(db.user, "t1", 1774000000, 30000, "listener") # earlier play (max_played_at is still 1775000000)

            # Run check
            db._checkAndRecalculateWrapped()

        # Cache should have updated and now show 2 plays
        cached2 = db.repo.getCachedWrapped(db.user, 2026)
        self.assertEqual(cached2["total_plays"], 2)
        self.assertEqual(cached2["max_played_at"], 1775000000)

    def test_boundary_play_does_not_cause_perpetual_recalculation(self):
        """A play landing exactly at a year's end boundary (midnight Jan 1 of
        the following year) belongs to that following year only.
        getPlayTotals() (cached as total_plays, via Repository's date-range
        clause) and getPlayCountInPeriod() (used here to detect drift, always
        exclusive) must agree on that boundary - otherwise a year's cached
        total permanently disagrees with the freshly queried total and the
        worker recalculates that year every single cycle forever."""
        db = self._makeDb({}, [])
        db.repo.upsertTrack({
            "id": "t1", "name": "Song 1", "url": "u1", "imageId": "img1", "duration": 30000,
            "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album 1", "url": "u", "imageId": "i", "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "artists": [{"id": "art1", "name": "Artist 1", "url": "u", "imageId": "i"}]
        })

        nowLocal = datetime.datetime.now(tz=db.tz)
        priorYear = nowLocal.year - 1
        priorYearStart = nowLocal.replace(year=priorYear, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        priorYearEnd = nowLocal.replace(year=nowLocal.year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

        # Strictly inside priorYear - keeps it from being skipped as "no plays".
        db.repo.insertPlay(db.user, "t1", (priorYearStart + datetime.timedelta(days=1)).timestamp(), 30000, "listener")
        # Exactly at priorYear's end boundary - belongs to nowLocal.year, not priorYear.
        db.repo.insertPlay(db.user, "t1", priorYearEnd.timestamp(), 30000, "listener")

        # Recalculating multiple years each sleeps WRAPPED_YEAR_DELAY_SECONDS
        # for real (no test currently mocks this) - skip that stall here.
        with patch.object(db.wrapped_stop_event, "wait", return_value=False):
            db._checkAndRecalculateWrapped()
            firstRunTotal = db.repo.getCachedWrappedTotalPlays(db.user, priorYear)
            self.assertEqual(firstRunTotal, 1)   #< only the play strictly inside priorYear

            with patch.object(db, "_calculateAndSaveWrapped", wraps=db._calculateAndSaveWrapped) as spy:
                db._checkAndRecalculateWrapped()

        # No new data since the first run - priorYear must not be recalculated again.
        recalculatedYears = [call.args[0] for call in spy.call_args_list]
        self.assertNotIn(priorYear, recalculatedYears)


class TestWrappedRecalcLocking(DatabaseTestCase):
    """The periodic wrapped worker (_checkAndRecalculateWrapped) and the
    on-demand recompute triggered by a /wrapped cache miss
    (recalculateWrappedForYear) must never run the expensive
    _calculateAndSaveWrapped for the same year at the same time."""

    def _seedOnePlayIn2026(self, db):
        db.repo.upsertTrack({
            "id": "t1", "name": "Song 1", "url": "u1", "imageId": "img1", "duration": 30000,
            "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album 1", "url": "u", "imageId": "i", "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "artists": [{"id": "art1", "name": "Artist 1", "url": "u", "imageId": "i"}]
        })
        db.repo.insertPlay(db.user, "t1", 1774000000, 30000, "listener")
        db.repo.deleteUserWrapped(db.user, 2026)

    def test_concurrent_on_demand_recalculations_only_compute_once(self):
        """Simulates several near-simultaneous /wrapped requests on a cache
        miss (e.g. multiple browser tabs): only the first should actually run
        _calculateAndSaveWrapped - the rest must see the now-fresh cache after
        waiting for the lock and skip redundant work."""
        db = self._makeDb({}, [])
        self._seedOnePlayIn2026(db)

        real = db._calculateAndSaveWrapped
        callCount = []

        def slowRealCalc(*args, **kwargs):
            callCount.append(1)
            time.sleep(0.05)   #< widen the race window
            return real(*args, **kwargs)

        def recalcThenCloseConnection():
            # Each thread gets its own thread-local sqlite connection (see
            # Database/db.py's ConnectionManager) - close it before the thread
            # exits so the test's tempdir cleanup can delete the file on Windows.
            db.recalculateWrappedForYear(2026)
            db.repo.connectionManager.close()

        with patch.object(db, "_calculateAndSaveWrapped", side_effect=slowRealCalc):
            threads = [threading.Thread(target=recalcThenCloseConnection) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(len(callCount), 1)
        cached = db.repo.getCachedWrapped(db.user, 2026)
        self.assertEqual(cached["total_plays"], 1)

    def test_background_check_skips_year_being_recalculated_on_demand(self):
        """If an on-demand recalculation already holds the lock for a year,
        the periodic worker must skip that year this cycle instead of
        duplicating the work or blocking the whole loop."""
        db = self._makeDb({}, [])
        self._seedOnePlayIn2026(db)

        lock = db._getWrappedRecalcLock(2026)
        lock.acquire()
        try:
            with patch.object(db, "_calculateAndSaveWrapped") as spy:
                db._checkAndRecalculateWrapped()
            spy.assert_not_called()
        finally:
            lock.release()

    def test_on_demand_recalc_skips_if_already_fresh_after_acquiring_lock(self):
        """After waiting for the lock, recalculateWrappedForYear must re-check
        freshness rather than blindly recomputing - otherwise a request that
        waited behind the periodic worker would redo the same expensive work
        that worker just finished."""
        db = self._makeDb({}, [])
        self._seedOnePlayIn2026(db)

        with patch.object(db.wrapped_stop_event, "wait", return_value=False):
            db._checkAndRecalculateWrapped()   #< populates a fresh cache
        with patch.object(db, "_calculateAndSaveWrapped") as spy:
            db.recalculateWrappedForYear(2026)
        spy.assert_not_called()


class TestWrappedRouteAjax(AppTestCase):
    def setUp(self):
        self.tzPatcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        self.tzPatcher.start()
        self.addCleanup(self.tzPatcher.stop)

        self.nowPatcher = patch.object(appModule, "now",
                                       return_value=datetime.datetime(2026, 7, 11, tzinfo=datetime.timezone.utc))
        self.nowPatcher.start()
        self.addCleanup(self.nowPatcher.stop)

    def test_ajax_returns_json_fragments(self):
        dash = self._makeApp()
        
        # Setup mock db
        db = MagicMock()
        db.tz = datetime.timezone.utc
        db.user = "alice"
        db.getEntriesFromOld.return_value = [{"playedAt": 1774000000}]
        
        # Return dummy cached data
        dummy_cached = {
            "total_plays": 12,
            "total_ms": 360000,
            "longest_streak": 3,
            "peak_day": "2026-03-04",
            "peak_plays": 4,
            "unique_songs": 5,
            "unique_artists": 2,
            "discovered_songs": 2,
            "discovered_artists": 1,
            "time_series_day": "[]",
            "time_series_week": "[]",
            "time_series_month": "[]",
            "top_songs": "[]",
            "top_artists": "[]",
            "top_albums": "[]",
            "discovered_songs_list": "[]",
            "discovered_artists_list": "[]",
            "discovered_albums_list": "[]",
        }
        db.repo.getCachedWrapped.return_value = dummy_cached

        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            
            resp = client.get("/wrapped?year=2026&ajax=true")
            self.assertEqual(resp.status_code, 200)
            data = json.loads(resp.data.decode())
            
            self.assertEqual(data["totalPlays"], 12)
            self.assertEqual(data["longestStreak"], 3)
            self.assertEqual(data["peakDay"], "2026-03-04")
            self.assertIn("topSongsHtml", data)
            self.assertIn("topArtistsHtml", data)
            # discoveredAlbumsCount has no consumer - the front-end reads
            # discoveredSongsCount/discoveredArtistsCount but never this key
            # (see templates/wrapped.html's AJAX handler) - it must not be
            # computed and shipped for nothing.
            self.assertNotIn("discoveredAlbumsCount", data)

    def test_sort_by_resorts_the_cached_top_songs_pool(self):
        """The cache stores top_songs pre-ranked by plays (up to 100 items) -
        sortBy re-sorts that already-fetched pool by the chosen field before
        slicing to the requested limit. Membership stays whatever the
        plays-ranked cache captured; only order/what survives the limit cut
        within it follows sortBy."""
        dash = self._makeApp()

        db = MagicMock()
        db.tz = datetime.timezone.utc
        db.user = "alice"
        db.getEntriesFromOld.return_value = [{"playedAt": 1774000000}]

        dummy_cached = {
            "total_plays": 12, "total_ms": 360000, "longest_streak": 3,
            "peak_day": "2026-03-04", "peak_plays": 4, "unique_songs": 2, "unique_artists": 0,
            "discovered_songs": 0, "discovered_artists": 0,
            "time_series_day": "[]", "time_series_week": "[]", "time_series_month": "[]",
            "top_songs": json.dumps([
                {"id": "many", "name": "ManyShortPlays", "artists": [], "duration": 60000,
                 "plays": 10, "totalTimeListened": 10000},
                {"id": "long", "name": "FewLongPlays", "artists": [], "duration": 60000,
                 "plays": 2, "totalTimeListened": 999999},
            ]),
            "top_artists": "[]", "top_albums": "[]",
            "discovered_songs_list": "[]", "discovered_artists_list": "[]", "discovered_albums_list": "[]",
        }
        db.repo.getCachedWrapped.return_value = dummy_cached

        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'

            resp = client.get("/wrapped?year=2026&sortBy=totalTimeListened")

        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertLess(body.index("FewLongPlays"), body.index("ManyShortPlays"))
