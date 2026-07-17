import shutil
import sqlite3
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# See tests/test_database_images.py for why this guard is needed.
if isinstance(sys.modules.get("Database.Migrators.migrate"), MagicMock):
    del sys.modules["Database.Migrators.migrate"]

import Database.Migrators.migrate as migrateModule
from Database.Migrators import dbversion


class TestMigrateIfNeeded(unittest.TestCase):
    def test_first_run_creates_data_dir_and_version_file(self):
        """On a fresh install neither Database/Data/ nor the legacy Database/Users/
        exists yet (it's runtime data, not part of the repo) - writing the version
        marker must create Data/ (the current naming) instead of crashing at
        startup or reviving the legacy Users/ name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            migratorsDir = base / "Migrators"
            migratorsDir.mkdir()
            (base / "VERSION").write_text("1.5.0", encoding="utf-8")

            with patch.object(migrateModule, "__file__", str(migratorsDir / "migrate.py")):
                migrateModule.migrateIfNeeded()

            versionFile = base / "Data" / "VERSION"
            self.assertTrue(versionFile.exists())
            self.assertEqual(versionFile.read_text(encoding="utf-8").strip(), "1.5.0")
            self.assertFalse((base / "Users").exists())

    def test_no_migration_when_versions_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            migratorsDir = base / "Migrators"
            migratorsDir.mkdir()
            (base / "VERSION").write_text("1.5.0", encoding="utf-8")
            usersDir = base / "Users"
            usersDir.mkdir()
            (usersDir / "VERSION").write_text("1.5.0", encoding="utf-8")

            with patch.object(migrateModule, "__file__", str(migratorsDir / "migrate.py")), \
                 patch.object(migrateModule, "migrate") as mock_migrate:
                migrateModule.migrateIfNeeded()

            mock_migrate.assert_not_called()

    def test_no_migration_when_versions_match_using_data_dir(self):
        """Same as above, but for an install that already went through the
        Users/ -> Data/ rename."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            migratorsDir = base / "Migrators"
            migratorsDir.mkdir()
            (base / "VERSION").write_text("1.7.0", encoding="utf-8")
            dataDir = base / "Data"
            dataDir.mkdir()
            (dataDir / "VERSION").write_text("1.7.0", encoding="utf-8")

            with patch.object(migrateModule, "__file__", str(migratorsDir / "migrate.py")), \
                 patch.object(migrateModule, "migrate") as mock_migrate:
                migrateModule.migrateIfNeeded()

            mock_migrate.assert_not_called()

    def test_same_minor_different_major_is_not_silently_skipped(self):
        """Database "1.7.0" vs app "2.7.0" only differ in major version, but
        getMiddleVersion() on each returns the same value (7) - a minor-only
        comparison used to treat that as already up to date and skip
        migration entirely instead of attempting it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            migratorsDir = base / "Migrators"
            migratorsDir.mkdir()
            (base / "VERSION").write_text("2.7.0", encoding="utf-8")
            dataDir = base / "Data"
            dataDir.mkdir()
            versionFile = dataDir / "VERSION"
            versionFile.write_text("1.7.0", encoding="utf-8")

            def fakeMigrate(major, minor, baseDir):
                versionFile.write_text("2.7.0", encoding="utf-8")

            with patch.object(migrateModule, "__file__", str(migratorsDir / "migrate.py")), \
                 patch.object(migrateModule, "migrate", side_effect=fakeMigrate) as mock_migrate:
                migrateModule.migrateIfNeeded()

            mock_migrate.assert_called_once()
            calledMajor, calledMinor, calledBaseDir = mock_migrate.call_args.args
            self.assertEqual((calledMajor, calledMinor), (1, 7))
            self.assertEqual(Path(calledBaseDir).resolve(), migratorsDir.resolve())

    def test_migrator_module_name_includes_the_major_version(self):
        """migrate() must not hardcode major version 1 into the module name it
        loads - a future migrate2_0_0.py must be reachable once the database
        is actually on major version 2."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            migratorsDir = base / "Migrators"
            migratorsDir.mkdir()
            # Create a VERSION file so BaseMigrator doesn't fail reading it
            dataDir = base / "Data"
            dataDir.mkdir()
            (dataDir / "VERSION").write_text("2.0.0", encoding="utf-8")
            modulePath = migratorsDir / "migrate2_0_0.py"
            modulePath.write_text(
                "class Migrator:\n"
                "    def __init__(self, fromVersion, toVersion):\n"
                "        pass\n"
                "    def migrate(self):\n"
                "        pass\n",
                encoding="utf-8",
            )

            migrateModule.migrate(2, 0, migratorsDir)  # must not raise (module found and imported)


class TestDatabaseVersionMarker(unittest.TestCase):
    """The database's version lives inside the .db file (schema_version
    table) so it survives a raw file copy/backup - see Database/backup.py,
    which only ever copies the .db file, never the sibling VERSION file."""

    def test_in_db_marker_takes_priority_over_a_stale_sibling_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            migratorsDir = base / "Migrators"
            migratorsDir.mkdir()
            (base / "VERSION").write_text("1.19.0", encoding="utf-8")
            dataDir = base / "Data"
            dataDir.mkdir()
            (dataDir / "VERSION").write_text("1.18.0", encoding="utf-8")   #< stale, must be ignored
            dbversion.writeDbVersion(dataDir / "spotify_stats.db", "1.19.0")

            with patch.object(migrateModule, "__file__", str(migratorsDir / "migrate.py")), \
                 patch.object(migrateModule, "migrate") as mock_migrate:
                migrateModule.migrateIfNeeded()

            mock_migrate.assert_not_called()

    def test_falls_back_to_sibling_file_and_backfills_the_in_db_marker(self):
        """A database that predates the schema_version table (has no rows in
        it yet) but sits next to an accurate sibling VERSION file - the
        common case for every already-deployed install on first startup
        after this feature ships."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            migratorsDir = base / "Migrators"
            migratorsDir.mkdir()
            (base / "VERSION").write_text("1.19.0", encoding="utf-8")
            dataDir = base / "Data"
            dataDir.mkdir()
            (dataDir / "VERSION").write_text("1.19.0", encoding="utf-8")
            dbPath = dataDir / "spotify_stats.db"
            sqlite3.connect(dbPath).close()   #< db exists, no schema_version rows yet

            with patch.object(migrateModule, "__file__", str(migratorsDir / "migrate.py")), \
                 patch.object(migrateModule, "migrate") as mock_migrate:
                migrateModule.migrateIfNeeded()

            mock_migrate.assert_not_called()
            self.assertEqual(dbversion.readDbVersion(dbPath), "1.19.0")

    def test_orphan_database_with_data_and_no_marker_anywhere_raises(self):
        """A database with real data but no version marker in it or beside
        it (e.g. a backup restored without its VERSION file) must not be
        silently treated as either up to date or a fresh install - too many
        historical migrations (data-only changes, in-place encryption) leave
        no structural trace a version could be reliably guessed from."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            migratorsDir = base / "Migrators"
            migratorsDir.mkdir()
            (base / "VERSION").write_text("1.19.0", encoding="utf-8")
            dataDir = base / "Data"
            dataDir.mkdir()
            dbPath = dataDir / "spotify_stats.db"
            conn = sqlite3.connect(dbPath)
            conn.execute("CREATE TABLE users (username TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO users (username) VALUES ('alice')")
            conn.commit()
            conn.close()

            with patch.object(migrateModule, "__file__", str(migratorsDir / "migrate.py")):
                with self.assertRaises(RuntimeError):
                    migrateModule.migrateIfNeeded()

    def test_restoring_an_old_backup_over_the_live_db_still_migrates(self):
        """The exact bug this feature fixes: Database/backup.py snapshots
        only the .db file (via SQLite's online backup API), never the
        sibling VERSION file. Restoring an old snapshot over a live db that
        has since advanced must not be mistaken for "already up to date"
        just because the directory's sibling VERSION file - untouched by
        the restore - still reflects the newer, since-replaced database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            migratorsDir = base / "Migrators"
            migratorsDir.mkdir()
            (base / "VERSION").write_text("1.19.0", encoding="utf-8")
            dataDir = base / "Data"
            dataDir.mkdir()
            dbPath = dataDir / "spotify_stats.db"

            # The live db starts at 1.10.0, and a backup is taken right here -
            # a bare file copy, exactly like SQLite's backup API produces.
            dbversion.writeDbVersion(dbPath, "1.10.0")
            (dataDir / "VERSION").write_text("1.10.0", encoding="utf-8")
            backupPath = dataDir / "backup_spotify_stats.db"
            shutil.copyfile(dbPath, backupPath)

            # The live db (and its sibling file) keep advancing past the backup...
            dbversion.writeDbVersion(dbPath, "1.19.0")
            (dataDir / "VERSION").write_text("1.19.0", encoding="utf-8")

            # ...then, weeks later, the backup is restored over the live db.
            # The sibling VERSION file is a directory-level file, not part of
            # the backup, so it's untouched and still says 1.19.0.
            shutil.copyfile(backupPath, dbPath)

            def fakeMigrate(major, minor, baseDir):
                dbversion.writeDbVersion(dbPath, "1.19.0")

            with patch.object(migrateModule, "__file__", str(migratorsDir / "migrate.py")), \
                 patch.object(migrateModule, "migrate", side_effect=fakeMigrate) as mock_migrate:
                migrateModule.migrateIfNeeded()

            # Must detect the restored db's TRUE version (1.10.0) from its own
            # in-db marker, not the stale-but-newer sibling file, and
            # therefore attempt to migrate it forward instead of skipping.
            mock_migrate.assert_called_once()
            calledMajor, calledMinor, _ = mock_migrate.call_args.args
            self.assertEqual((calledMajor, calledMinor), (1, 10))

    def test_empty_database_with_no_marker_anywhere_is_a_fresh_install(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            migratorsDir = base / "Migrators"
            migratorsDir.mkdir()
            (base / "VERSION").write_text("1.19.0", encoding="utf-8")
            dataDir = base / "Data"
            dataDir.mkdir()
            dbPath = dataDir / "spotify_stats.db"
            sqlite3.connect(dbPath).close()   #< empty db, no data, no marker anywhere

            with patch.object(migrateModule, "__file__", str(migratorsDir / "migrate.py")), \
                 patch.object(migrateModule, "migrate") as mock_migrate:
                migrateModule.migrateIfNeeded()

            mock_migrate.assert_not_called()
            self.assertEqual((dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.19.0")
            self.assertEqual(dbversion.readDbVersion(dbPath), "1.19.0")


if __name__ == "__main__":
    unittest.main()
