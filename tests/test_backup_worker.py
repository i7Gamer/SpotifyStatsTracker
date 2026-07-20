"""Automatic scheduled backups of the shared SQLite database.

The README's manual `docker compose exec ... backup` command protects nobody
who doesn't run it. The BackupWorker snapshots the database on a schedule
using SQLite's online backup API (safe against a live WAL database), rotates
old snapshots, and is restart-safe: whether a backup is due is judged from
the newest existing backup file, not from process start time.
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.backup as backupModule
from Database.backup import BackupWorker


def _makeSourceDb(path: Path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE plays (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("INSERT INTO plays (note) VALUES ('keep me safe')")
    conn.commit()
    conn.close()


class BackupWorkerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.dbPath = self.root / "spotify_stats.db"
        _makeSourceDb(self.dbPath)

    def _makeWorker(self, **kwargs):
        return BackupWorker(dbPath=self.dbPath, **kwargs)


class TestRunBackup(BackupWorkerTestCase):
    def test_backup_creates_a_readable_snapshot(self):
        worker = self._makeWorker()

        backupPath = worker.runBackup()

        self.assertTrue(backupPath.exists())
        self.assertEqual(backupPath.parent, self.root / "Backups")
        self.assertTrue(backupPath.name.startswith(backupModule.BACKUP_FILENAME_PREFIX))
        conn = sqlite3.connect(backupPath)
        self.addCleanup(conn.close)
        rows = conn.execute("SELECT note FROM plays").fetchall()
        self.assertEqual(rows, [("keep me safe",)])

    def test_backup_captures_committed_but_uncheckpointed_wal_data(self):
        """Guards the exact risk runBackup() exists to avoid: a raw copy of
        just spotify_stats.db would miss rows sitting in the -wal file that
        haven't been checkpointed into the main file yet. sqlite3.Connection.backup()
        is WAL-aware and must capture them regardless of checkpoint state."""
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO plays (note) VALUES ('still in the wal')")
        conn.commit()
        # Sanity check the scenario is real: the write actually landed in the
        # WAL file rather than being auto-checkpointed into the main file.
        self.assertTrue((self.root / "spotify_stats.db-wal").exists())

        worker = self._makeWorker()
        backupPath = worker.runBackup()

        snapshot = sqlite3.connect(backupPath)
        self.addCleanup(snapshot.close)
        notes = {row[0] for row in snapshot.execute("SELECT note FROM plays")}
        self.assertEqual(notes, {"keep me safe", "still in the wal"})

    def test_no_partial_files_left_behind(self):
        worker = self._makeWorker()

        worker.runBackup()

        leftovers = [p for p in (self.root / "Backups").iterdir() if p.suffix != ".db"]
        self.assertEqual(leftovers, [])

    def test_rotation_keeps_only_the_newest_snapshots(self):
        worker = self._makeWorker(retentionCount=2)
        backupDir = self.root / "Backups"
        backupDir.mkdir()
        # Pre-existing older snapshots (timestamped names sort chronologically).
        for stamp in ("20250101_000000", "20250102_000000", "20250103_000000"):
            (backupDir / f"{backupModule.BACKUP_FILENAME_PREFIX}{stamp}.db").write_bytes(b"old")

        newest = worker.runBackup()

        remaining = sorted(p.name for p in backupDir.iterdir())
        self.assertEqual(len(remaining), 2)
        self.assertIn(newest.name, remaining)
        self.assertIn(f"{backupModule.BACKUP_FILENAME_PREFIX}20250103_000000.db", remaining)

    def test_rotation_ignores_unrelated_files(self):
        worker = self._makeWorker(retentionCount=1)
        backupDir = self.root / "Backups"
        backupDir.mkdir()
        unrelated = backupDir / "my-manual-copy.db"
        unrelated.write_bytes(b"mine")

        worker.runBackup()

        self.assertTrue(unrelated.exists())

    def test_zero_retention_rotates_nothing(self):
        """retentionCount=0 disables scheduled backups (isEnabled is False), so
        a DIRECT runBackup() call must rotate nothing - not read 0 as "keep
        zero snapshots" and delete every existing snapshot including the one
        runBackup() itself just wrote."""
        worker = self._makeWorker(retentionCount=0)
        backupDir = self.root / "Backups"
        backupDir.mkdir()
        preexisting = backupDir / f"{backupModule.BACKUP_FILENAME_PREFIX}20250101_000000.db"
        preexisting.write_bytes(b"old")

        newest = worker.runBackup()

        self.assertTrue(newest.exists())
        self.assertTrue(preexisting.exists())


class TestIsDue(BackupWorkerTestCase):
    def test_due_when_no_backup_exists_yet(self):
        self.assertTrue(self._makeWorker().isDue())

    def test_not_due_right_after_a_backup(self):
        worker = self._makeWorker()
        worker.runBackup()
        self.assertFalse(worker.isDue())

    def test_due_again_once_the_newest_backup_is_older_than_the_interval(self):
        worker = self._makeWorker(intervalHours=1)
        backupPath = worker.runBackup()
        oldTime = time.time() - 2 * 3600
        os.utime(backupPath, (oldTime, oldTime))

        self.assertTrue(worker.isDue())


class TestConfiguration(BackupWorkerTestCase):
    def test_disabled_via_zero_interval(self):
        worker = self._makeWorker(intervalHours=0)
        self.assertFalse(worker.isEnabled())

    def test_disabled_via_zero_retention(self):
        worker = self._makeWorker(retentionCount=0)
        self.assertFalse(worker.isEnabled())

    def test_env_vars_override_defaults(self):
        env = {
            backupModule.BACKUP_INTERVAL_ENV_VAR: "6",
            backupModule.BACKUP_RETENTION_ENV_VAR: "3",
        }
        with patch.dict(os.environ, env):
            worker = self._makeWorker()
        self.assertEqual(worker.intervalHours, 6)
        self.assertEqual(worker.retentionCount, 3)

    def test_junk_env_values_fall_back_to_defaults(self):
        env = {
            backupModule.BACKUP_INTERVAL_ENV_VAR: "banana",
            backupModule.BACKUP_RETENTION_ENV_VAR: "",
        }
        with patch.dict(os.environ, env):
            worker = self._makeWorker()
        self.assertEqual(worker.intervalHours, backupModule.DEFAULT_BACKUP_INTERVAL_HOURS)
        self.assertEqual(worker.retentionCount, backupModule.DEFAULT_BACKUP_RETENTION_COUNT)


class TestWorkerThread(BackupWorkerTestCase):
    def test_start_and_stop_cleanly_without_backing_up_immediately(self):
        """The thread waits out a startup delay before its first due-check, so
        app construction (and every app test) doesn't race a backup write."""
        worker = self._makeWorker()
        worker.start()
        self.assertTrue(worker.thread.is_alive())

        worker.stop()

        self.assertFalse(worker.thread.is_alive())
        self.assertFalse((self.root / "Backups").exists())

    def test_disabled_worker_does_not_start_a_thread(self):
        worker = self._makeWorker(intervalHours=0)
        worker.start()
        self.assertIsNone(worker.thread)


if __name__ == "__main__":
    unittest.main()
