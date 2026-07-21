"""Tests for WorkerTelemetryMixin: per-worker cycle success/failure counters
backing the /admin Worker Health FAILING badges."""
import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.database import Database


class TestWorkerTelemetryMixin(unittest.TestCase):
    def setUp(self):
        self.db = Database.__new__(Database)
        self.db._initWorkerTelemetry()

    def test_unknown_worker_returns_zero_defaults(self):
        snapshot = self.db._getWorkerTelemetry("never_recorded")
        self.assertEqual(snapshot["consecutive_failures"], 0)
        self.assertEqual(snapshot["failure_rate"], 0.0)
        self.assertIsNone(snapshot["last_error"])

    def test_single_success_recorded(self):
        self.db._recordWorkerCycle("wrapped", success=True)
        snapshot = self.db._getWorkerTelemetry("wrapped")
        self.assertEqual(snapshot["consecutive_failures"], 0)
        self.assertEqual(snapshot["failure_rate"], 0.0)
        self.assertIsNone(snapshot["last_error"])

    def test_single_failure_recorded(self):
        self.db._recordWorkerCycle("wrapped", success=False, error="boom")
        snapshot = self.db._getWorkerTelemetry("wrapped")
        self.assertEqual(snapshot["consecutive_failures"], 1)
        self.assertEqual(snapshot["failure_rate"], 1.0)
        self.assertEqual(snapshot["last_error"], "boom")

    def test_consecutive_failures_accumulate(self):
        self.db._recordWorkerCycle("wrapped", success=False, error="first")
        self.db._recordWorkerCycle("wrapped", success=False, error="second")
        self.db._recordWorkerCycle("wrapped", success=False, error="third")
        snapshot = self.db._getWorkerTelemetry("wrapped")
        self.assertEqual(snapshot["consecutive_failures"], 3)
        self.assertEqual(snapshot["last_error"], "third")

    def test_success_resets_consecutive_failures_but_not_failure_rate(self):
        self.db._recordWorkerCycle("wrapped", success=False, error="first")
        self.db._recordWorkerCycle("wrapped", success=False, error="second")
        self.db._recordWorkerCycle("wrapped", success=True)
        snapshot = self.db._getWorkerTelemetry("wrapped")
        self.assertEqual(snapshot["consecutive_failures"], 0)
        # 2 failures out of 3 total cycles - lifetime rate is unaffected by the reset.
        self.assertAlmostEqual(snapshot["failure_rate"], 2 / 3)

    def test_failure_rate_computed_over_total_cycles(self):
        for _ in range(3):
            self.db._recordWorkerCycle("spotify_api", success=True)
        self.db._recordWorkerCycle("spotify_api", success=False, error="oops")
        snapshot = self.db._getWorkerTelemetry("spotify_api")
        self.assertAlmostEqual(snapshot["failure_rate"], 0.25)
        self.assertEqual(snapshot["consecutive_failures"], 1)

    def test_workers_tracked_independently(self):
        self.db._recordWorkerCycle("lastfm_genre", success=False, error="genre failed")
        self.db._recordWorkerCycle("lastfm_artist_bio", success=True)
        genre_snapshot = self.db._getWorkerTelemetry("lastfm_genre")
        bio_snapshot = self.db._getWorkerTelemetry("lastfm_artist_bio")
        self.assertEqual(genre_snapshot["consecutive_failures"], 1)
        self.assertEqual(bio_snapshot["consecutive_failures"], 0)


if __name__ == "__main__":
    unittest.main()
