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


if __name__ == "__main__":
    unittest.main()
