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
    def test_first_run_creates_users_dir_and_version_file(self):
        """On a fresh install Database/Users/ doesn't exist yet (it's runtime user
        data, not part of the repo) - writing the version marker must create it
        instead of crashing at startup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            migratorsDir = base / "Migrators"
            migratorsDir.mkdir()
            (base / "VERSION").write_text("1.5.0", encoding="utf-8")

            with patch.object(migrateModule, "__file__", str(migratorsDir / "migrate.py")):
                migrateModule.migrateIfNeeded()

            versionFile = base / "Users" / "VERSION"
            self.assertTrue(versionFile.exists())
            self.assertEqual(versionFile.read_text(encoding="utf-8").strip(), "1.5.0")

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


if __name__ == "__main__":
    unittest.main()
