"""Image-download claims stuck in 'pending' must be cleared at startup.

tryClaimImageDownload marks a row 'pending' before the download runs and
refuses to re-claim a pending row. A crash/restart between claim and
completion - or the status write failing while a long import holds the
SQLite write lock - left that row 'pending' forever, so the artwork was
never downloaded and never retried. At startup no download can be in
flight, so any surviving 'pending' row is by definition stale: deleting it
(rather than marking it failed, which lazyFetchArtistImage treats as
permanent) returns the image to never-attempted and lets both the track
and artist paths retry naturally.
"""
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.repository import (
    Repository, IMAGE_KIND_TRACK, IMAGE_KIND_ARTIST,
    IMAGE_STATUS_OK, IMAGE_STATUS_FAILED, IMAGE_STATUS_PENDING,
)
from app import SpotifyDashboardApp
from _app_factory import AppTestCase

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class TestDeleteStalePendingImages(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

    def test_pending_claims_are_deleted_and_reclaimable(self):
        self.assertTrue(self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK))
        self.assertTrue(self.repo.tryClaimImageDownload("img2", IMAGE_KIND_ARTIST))
        # Simulated crash: the claims never resolve to ok/failed.

        cleared = self.repo.deleteStalePendingImages()

        self.assertEqual(cleared, 2)
        self.assertIsNone(self.repo.imageStatus("img1", IMAGE_KIND_TRACK))
        self.assertTrue(self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK),
                        "a cleared claim must be claimable again")

    def test_completed_and_failed_statuses_are_untouched(self):
        self.repo.markImageStatus("done", IMAGE_KIND_TRACK, IMAGE_STATUS_OK)
        self.repo.markImageStatus("broken", IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED)

        cleared = self.repo.deleteStalePendingImages()

        self.assertEqual(cleared, 0)
        self.assertEqual(self.repo.imageStatus("done", IMAGE_KIND_TRACK), IMAGE_STATUS_OK)
        self.assertEqual(self.repo.imageStatus("broken", IMAGE_KIND_ARTIST), IMAGE_STATUS_FAILED)

    def test_no_pending_rows_is_a_noop(self):
        self.assertEqual(self.repo.deleteStalePendingImages(), 0)


class TestAppClearsStaleClaimsAtStartup(AppTestCase):
    def test_startup_clears_pending_claims_left_by_a_previous_run(self):
        # Seed a stale claim into the (per-test isolated) default database the
        # app is about to open - as if the previous process died mid-download.
        seedRepo = Repository()
        seedRepo.tryClaimImageDownload("orphaned", IMAGE_KIND_TRACK)
        self.assertEqual(seedRepo.imageStatus("orphaned", IMAGE_KIND_TRACK), IMAGE_STATUS_PENDING)
        seedRepo.connectionManager.close()

        dash = self._makeApp()

        self.assertIsNone(dash.repo.imageStatus("orphaned", IMAGE_KIND_TRACK))


if __name__ == "__main__":
    unittest.main()
