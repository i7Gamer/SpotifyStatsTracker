"""Database/Migrators/dbversion.py stores the schema version *inside* the
sqlite file itself (a schema_version table), so it travels with the file -
unlike the old sibling VERSION text file, which a raw file copy (e.g. a
backup) leaves behind. These helpers deliberately use a bare sqlite3.connect()
rather than Repository/ConnectionManager: the latter runs db.py's full current
SCHEMA (CREATE TABLE IF NOT EXISTS ...) on every connection, which would stamp
every current table onto an old database before its true version was ever
read."""
import sqlite3
import sys
import os
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.Migrators import dbversion


class DbVersionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.dbPath = Path(self._tmpdir.name) / "spotify_stats.db"


class TestReadDbVersion(DbVersionTestCase):
    def test_returns_none_for_a_database_with_no_schema_version_table(self):
        sqlite3.connect(self.dbPath).close()   #< brand-new, empty file
        self.assertIsNone(dbversion.readDbVersion(self.dbPath))

    def test_returns_none_for_a_database_with_an_empty_schema_version_table(self):
        conn = sqlite3.connect(self.dbPath)
        conn.execute(dbversion.SCHEMA_VERSION_TABLE_SQL)
        conn.commit()
        conn.close()
        self.assertIsNone(dbversion.readDbVersion(self.dbPath))

    def test_returns_the_written_version(self):
        dbversion.writeDbVersion(self.dbPath, "1.18.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.18.0")

    def test_returns_the_most_recently_written_version(self):
        dbversion.writeDbVersion(self.dbPath, "1.17.0")
        dbversion.writeDbVersion(self.dbPath, "1.18.0")
        dbversion.writeDbVersion(self.dbPath, "1.19.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.19.0")


class TestWriteDbVersion(DbVersionTestCase):
    def test_creates_the_table_if_missing(self):
        dbversion.writeDbVersion(self.dbPath, "1.19.0")
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("schema_version", tables)

    def test_appends_rather_than_overwriting(self):
        """Keeping every historical row is a cheap audit trail and means a
        second write can never accidentally lose the first."""
        dbversion.writeDbVersion(self.dbPath, "1.18.0")
        dbversion.writeDbVersion(self.dbPath, "1.19.0")
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        self.assertEqual(count, 2)

    def test_survives_sqlites_own_backup_api(self):
        """The whole point: SQLite's online backup API (used by
        Database/backup.py) copies every table, so a version written before a
        backup must still be readable from the backup file afterward."""
        dbversion.writeDbVersion(self.dbPath, "1.19.0")
        backupPath = self.dbPath.with_name("backup.db")
        source = sqlite3.connect(self.dbPath)
        destination = sqlite3.connect(backupPath)
        source.backup(destination)
        destination.close()
        source.close()

        self.assertEqual(dbversion.readDbVersion(backupPath), "1.19.0")


class TestHasAnyData(DbVersionTestCase):
    def test_false_for_a_brand_new_empty_database(self):
        sqlite3.connect(self.dbPath).close()
        self.assertFalse(dbversion.hasAnyData(self.dbPath))

    def test_false_when_only_schema_version_itself_has_rows(self):
        dbversion.writeDbVersion(self.dbPath, "1.19.0")
        self.assertFalse(dbversion.hasAnyData(self.dbPath))

    def test_true_when_any_other_table_has_a_row(self):
        conn = sqlite3.connect(self.dbPath)
        conn.execute("CREATE TABLE users (username TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO users (username) VALUES ('alice')")
        conn.commit()
        conn.close()
        self.assertTrue(dbversion.hasAnyData(self.dbPath))

    def test_false_when_other_tables_exist_but_are_empty(self):
        conn = sqlite3.connect(self.dbPath)
        conn.execute("CREATE TABLE users (username TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        self.assertFalse(dbversion.hasAnyData(self.dbPath))


if __name__ == "__main__":
    unittest.main()
