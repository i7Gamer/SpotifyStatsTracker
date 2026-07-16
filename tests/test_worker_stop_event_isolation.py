"""Every restartable background worker hands each run its own private stop
event (instead of clear()-ing a shared one): stop() joins with a timeout, so
a thread blocked in slow I/O can outlive it - a restart that cleared the
shared event would revive that zombie alongside the new thread, permanently
duplicating the worker. The Last.fm genre worker's twin test lives in
tests/test_lastfm_backfiller.py; the auto-import watchdog needs none (its
run=False latch makes stop terminal - a fresh Watchdog is built per
Database), and the Spotify listener builds a new Listener object per start."""
import sys
import os
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.backup import BackupWorker


class MetadataBackfillerStopEventTestCase(DatabaseTestCase):
    def test_restart_uses_a_fresh_stop_event_so_a_lingering_thread_cannot_revive(self):
        db = self._makeDb({}, [])   #< __init__ auto-starts the backfiller (startup-delay wait)
        self.assertTrue(db.backfiller_thread.is_alive())
        firstEvent = db.backfiller_stop_event
        db.stopMetadataBackfiller()

        db.startMetadataBackfiller()
        self.assertIsNot(db.backfiller_stop_event, firstEvent)
        self.assertTrue(firstEvent.is_set())            #< the old thread's signal stays set
        self.assertFalse(db.backfiller_stop_event.is_set())
        db.stopMetadataBackfiller()


class WrappedWorkerStopEventTestCase(DatabaseTestCase):
    def test_restart_uses_a_fresh_stop_event_so_a_lingering_thread_cannot_revive(self):
        db = self._makeDb({}, [])   #< __init__ auto-starts the wrapped worker
        self.assertTrue(db.wrapped_thread.is_alive())
        firstEvent = db.wrapped_stop_event
        db.stopWrappedCalculationsWorker()

        db.startWrappedCalculationsWorker()
        self.assertIsNot(db.wrapped_stop_event, firstEvent)
        self.assertTrue(firstEvent.is_set())
        self.assertFalse(db.wrapped_stop_event.is_set())
        db.stopWrappedCalculationsWorker()


class BackupWorkerStopEventTestCase(unittest.TestCase):
    def test_restart_uses_a_fresh_stop_event_so_a_lingering_thread_cannot_revive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = BackupWorker(dbPath=root / "db.sqlite", backupDir=root / "Backups",
                                  intervalHours=1, retentionCount=1)
            worker.start()
            self.assertTrue(worker.thread.is_alive())   #< sits in its random startup delay
            firstEvent = worker._stop_event
            worker.stop()

            worker.start()
            self.assertIsNot(worker._stop_event, firstEvent)
            self.assertTrue(firstEvent.is_set())
            self.assertFalse(worker._stop_event.is_set())
            worker.stop()


if __name__ == "__main__":
    unittest.main()
