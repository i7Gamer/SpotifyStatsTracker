import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from Database.Migrators.migrate1_37_0 import Migrator
from Database.Migrators.dbversion import writeDbVersion, readDbVersion


class TestMigrate1370(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmpdir.name)
        self.data_dir = self.base_dir / "Data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "spotify_stats.db"
        self.version_file = self.data_dir / "VERSION"

        writeDbVersion(self.db_path, "1.37.0")
        self.version_file.write_text("1.37.0")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_the_fixture_stays_inside_its_own_tempdir(self):
        """`base_dir / ".." / "Data"` is never resolved, so mkdir(parents=True)
        created <os-temp>/Data - OUTSIDE the TemporaryDirectory tearDown
        cleans. It was therefore left behind by every run, and shared by every
        `pytest -n auto` worker at once, each stamping the same
        spotify_stats.db and VERSION the others were asserting on."""
        self.assertEqual(self.data_dir.resolve().parent, self.base_dir.resolve())

    @patch("Database.Migrators.base.resolveRuntimeDir")
    def test_migration_updates_version(self, mock_resolve):
        mock_resolve.return_value = self.data_dir
        migrator = Migrator("1.37.0", "1.38.0")
        migrator.migrate()

        self.assertEqual(readDbVersion(self.db_path), "1.38.0")
        self.assertEqual(self.version_file.read_text().strip(), "1.38.0")


if __name__ == "__main__":
    unittest.main()
