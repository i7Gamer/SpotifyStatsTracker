"""ConnectionManager._newConnection() copies a cached in-memory schema
template via sqlite3.Connection.backup() for brand-new database files instead
of re-running the full DDL script, since backup() is markedly cheaper and
this constructor runs on essentially every test in the suite. These tests
guard the two ways that optimization could go wrong: the copied schema
silently drifting from the real DDL, and the "existing file" fallback path
(which must NOT use backup(), since backup() overwrites its destination)
losing data on reopen.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import Database.db as dbModule
from Database.db import ConnectionManager, SCHEMA
import sqlite3


def _schemaDump(conn: sqlite3.Connection) -> list[tuple]:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name"
    ).fetchall()
    return [tuple(row) for row in rows]


class TestSchemaTemplateMatchesDirectExecutescript(unittest.TestCase):
    def test_backup_copied_schema_matches_executescript(self):
        """A brand-new file's schema (via the backup() fast path) must be
        byte-for-byte identical to running the DDL script directly - the two
        paths must never be allowed to drift apart."""
        with tempfile.TemporaryDirectory() as tmpDir:
            manager = ConnectionManager(Path(tmpDir) / "viaBackup.db")
            viaBackup = manager.connection()
            try:
                direct = sqlite3.connect(":memory:")
                direct.executescript(SCHEMA)

                self.assertEqual(_schemaDump(viaBackup), _schemaDump(direct))
            finally:
                manager.close()


class TestExistingFileIsNeverOverwritten(unittest.TestCase):
    def test_reopening_an_existing_populated_file_preserves_its_data(self):
        """The backup() fast path only applies to a file this connection is
        the first to create - reopening an existing file (a second
        ConnectionManager against the same path, simulating a restart or a
        second thread) must take the executescript fallback and must never
        wipe what's already there."""
        with tempfile.TemporaryDirectory() as tmpDir:
            dbPath = Path(tmpDir) / "existing.db"

            first = ConnectionManager(dbPath)
            first.connection().execute(
                "INSERT INTO artists (id, name, url) VALUES ('a1', 'Artist', '')"
            )
            first.connection().commit()
            first.close()

            second = ConnectionManager(dbPath)
            try:
                count = second.connection().execute(
                    "SELECT COUNT(*) FROM artists WHERE id='a1'"
                ).fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                second.close()

    def test_a_second_connection_racing_the_first_new_file_also_preserves_data(self):
        """Simulates two threads racing to create the SAME brand-new file:
        by the time the second ConnectionManager opens it, the file already
        exists (created by the first), so it must fall back to executescript
        rather than calling backup() and overwriting the first connection's
        already-committed row."""
        with tempfile.TemporaryDirectory() as tmpDir:
            dbPath = Path(tmpDir) / "race.db"

            first = ConnectionManager(dbPath)
            first.connection().execute(
                "INSERT INTO artists (id, name, url) VALUES ('a1', 'Artist', '')"
            )
            first.connection().commit()

            second = ConnectionManager(dbPath)
            try:
                count = second.connection().execute(
                    "SELECT COUNT(*) FROM artists WHERE id='a1'"
                ).fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                first.close()
                second.close()


class TestSchemaTemplateRespectsAPatchedSchema(unittest.TestCase):
    """Migration tests (e.g. test_migrate1_23_0.py) patch.object(dbModule,
    "SCHEMA", legacySchema) to build a pre-migration database, relying on a
    brand-new file's executescript(SCHEMA) picking up the patched value. The
    cached template must never let a stale, pre-patch schema leak through."""

    def test_a_schema_patched_after_the_template_was_cached_is_still_honored(self):
        with tempfile.TemporaryDirectory() as tmpDir:
            # Prime the cache with the real SCHEMA first (mirrors most tests
            # in the suite running before a migration test does).
            warmupManager = ConnectionManager(Path(tmpDir) / "warmup.db")
            warmupManager.connection()
            warmupManager.close()

            legacySchema = SCHEMA.replace(
                "is_admin              INTEGER NOT NULL DEFAULT 0,", ""
            )
            self.assertNotEqual(legacySchema, SCHEMA)   #< sanity: the replace matched something

            with patch.object(dbModule, "SCHEMA", legacySchema):
                legacyManager = ConnectionManager(Path(tmpDir) / "legacy.db")
                try:
                    columns = {
                        row["name"]
                        for row in legacyManager.connection().execute("PRAGMA table_info(users)")
                    }
                    self.assertNotIn("is_admin", columns)
                finally:
                    legacyManager.close()

            # Un-patched SCHEMA must still produce the real, current schema
            # for a later, ordinary test.
            afterManager = ConnectionManager(Path(tmpDir) / "after.db")
            try:
                columns = {
                    row["name"]
                    for row in afterManager.connection().execute("PRAGMA table_info(users)")
                }
                self.assertIn("is_admin", columns)
            finally:
                afterManager.close()


if __name__ == "__main__":
    unittest.main()
