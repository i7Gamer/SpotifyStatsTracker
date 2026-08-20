"""An import_progress row stuck at 'running' must be failed at startup.

tryClaimImportRunning marks the row 'running' before the import thread starts
and refuses to claim while it stands. routes/system.py's _releaseImportSlot
writes the terminal row when the THREAD dies - but a PROCESS that dies
mid-import (a deploy, SIGKILL, power loss) skips every finally on its daemon
threads, so the row said 'running' forever and every future claim was
refused: that user could never import again without manual database surgery.
At startup no import can be in flight in this single-process app, so any
surviving 'running' row is by definition stale. Mirrors the image-claim reset
(tests/test_stale_image_claims.py).
"""
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.repository import Repository
from _app_factory import AppTestCase


class TestFailStaleRunningImports(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)
        self.repo.upsertUser("alice", "alice@example.com")

    def test_a_leftover_running_row_is_failed_and_claimable_again(self):
        self.assertTrue(self.repo.tryClaimImportRunning("alice"))
        # Simulated process death: no terminal row was ever written.

        failed = self.repo.failStaleRunningImports()

        self.assertEqual(failed, 1)
        progress = self.repo.readProgress("alice")
        self.assertEqual(progress["status"], "failed")
        self.assertTrue(progress["error"])
        #< the /import page shows this string; it has to say what happened and
        #  that retrying is safe
        self.assertIn("interrupted", progress["message"].lower())
        self.assertTrue(self.repo.tryClaimImportRunning("alice"),
                        "a cleared slot must be claimable again")

    def test_terminal_rows_are_left_alone(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.writeProgress("alice", "complete", 10, 10, "Imported 10 of 10", False)
        self.repo.writeProgress("bob", "failed", 3, 10, "Bad file", True)

        self.assertEqual(self.repo.failStaleRunningImports(), 0)

        self.assertEqual(self.repo.readProgress("alice")["message"], "Imported 10 of 10")
        self.assertEqual(self.repo.readProgress("bob")["message"], "Bad file")

    def test_only_the_running_row_of_a_mixed_set_is_touched(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.assertTrue(self.repo.tryClaimImportRunning("alice"))
        self.repo.writeProgress("bob", "complete", 1, 1, "Done", False)

        self.assertEqual(self.repo.failStaleRunningImports(), 1)

        self.assertEqual(self.repo.readProgress("alice")["status"], "failed")
        self.assertEqual(self.repo.readProgress("bob")["status"], "complete")

    def test_no_running_rows_is_a_noop(self):
        self.assertEqual(self.repo.failStaleRunningImports(), 0)


class TestAppFailsStaleImportsAtStartup(AppTestCase):
    def test_startup_fails_a_running_row_left_by_a_previous_run(self):
        # Seed the (per-test isolated) default database the app is about to
        # open - as if the previous process died mid-import.
        seedRepo = Repository()
        seedRepo.upsertUser("alice", "alice@example.com")
        self.assertTrue(seedRepo.tryClaimImportRunning("alice"))
        seedRepo.connectionManager.close()

        dash = self._makeApp()

        self.assertEqual(dash.repo.readProgress("alice")["status"], "failed")
        self.assertTrue(dash.repo.tryClaimImportRunning("alice"),
                        "the user must be able to import again after the restart")


if __name__ == "__main__":
    unittest.main()
