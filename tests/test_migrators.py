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


if __name__ == "__main__":
    unittest.main()
