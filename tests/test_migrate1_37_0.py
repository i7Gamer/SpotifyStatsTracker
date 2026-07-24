import unittest
import tempfile
from pathlib import Path
from Database.Migrators.migrate1_37_0 import Migrator
from Database.Migrators.dbversion import writeDbVersion, readDbVersion


class TestMigrate1370(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmpdir.name)
        self.data_dir = self.base_dir / ".." / "Data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "spotify_stats.db"
        self.version_file = self.data_dir / "VERSION"

        writeDbVersion(self.db_path, "1.37.0")
        self.version_file.write_text("1.37.0")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_migration_updates_version(self):
        migrator = Migrator("1.37.0", "1.38.0")
        migrator.baseDir = self.base_dir
        migrator.dbPath = self.db_path
        migrator.databaseVersionFile = self.version_file
        migrator.databaseVersion = "1.37.0"

        migrator.migrate()

        self.assertEqual(readDbVersion(self.db_path), "1.38.0")
        self.assertEqual(self.version_file.read_text().strip(), "1.38.0")


if __name__ == "__main__":
    unittest.main()
