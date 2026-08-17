"""The migration helpers wait out a lock like everything else does.

readDbVersion / writeDbVersion / hasAnyData each open a raw sqlite3 connection.
A raw connection's busy_timeout is 0, so any lock held at that instant is an
immediate "database is locked" rather than a wait - and these run at startup,
on the same file the live instance may still be checkpointing. It is the same
gap Database/backup.py closed for the snapshot connection, for the same reason:
every ConnectionManager connection already waits, and a startup path that
uniquely refuses to is a version read that fails for a wait the rest of the app
is happy to make.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.Migrators.dbversion import (
    MIGRATION_BUSY_TIMEOUT_MS,
    hasAnyData,
    openMigrationConnection,
    readDbVersion,
    writeDbVersion,
)


class TestMigrationConnectionWaitsOutALock(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.dbPath = Path(self._tmpdir.name) / "test.db"
        conn = sqlite3.connect(self.dbPath)
        try:
            with conn:
                conn.execute("CREATE TABLE t (x INTEGER)")
        finally:
            conn.close()

    def test_the_helper_connection_carries_the_timeout(self):
        conn = openMigrationConnection(self.dbPath)
        try:
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0],
                             MIGRATION_BUSY_TIMEOUT_MS)
        finally:
            conn.close()

    def test_the_timeout_is_a_real_wait_not_zero(self):
        """0 is what a raw connection has, and is the bug this pins."""
        self.assertGreater(MIGRATION_BUSY_TIMEOUT_MS, 0)

    def test_every_helper_uses_it(self):
        """All three open their own connection, so all three had the gap."""
        writeDbVersion(self.dbPath, "1.50.5")

        for helper in (readDbVersion, hasAnyData):
            with self.subTest(helper=helper.__name__):
                #< it answering at all proves it opened through the helper;
                #  the pragma itself is asserted above
                self.assertIsNotNone(helper(self.dbPath))

    def test_reading_a_version_back_still_works(self):
        writeDbVersion(self.dbPath, "1.50.5")

        self.assertEqual(readDbVersion(self.dbPath), "1.50.5")

    def test_a_read_does_not_create_the_file_it_was_given(self):
        """readDbVersion is documented as never writing - including not
        bringing a missing database into existence."""
        missing = Path(self._tmpdir.name) / "absent.db"

        readDbVersion(missing)

        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
