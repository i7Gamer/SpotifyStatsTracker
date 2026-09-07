# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""1.53.0 -> 1.54.0 is a stamp-only step: 1.54.0 adds no schema, but the chain
steps at minor granularity and migrateIfNeeded imports migrate<major>_<minor>_0
for every minor between the database and the app - so without this file a
1.53.x database meeting a 1.54.0 app fails at boot with a missing module.
Shape copied from tests/test_migrate1_37_0.py, the previous stamp-only step."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.Migrators.migrate1_53_0 import Migrator
from Database.Migrators.dbversion import writeDbVersion, readDbVersion

FROM_VERSION = "1.53.0"
TO_VERSION = "1.54.0"
PATCH_STAMPED_FROM_VERSION = "1.53.1"   #< what an install upgraded through 1.53.1 actually carries
WRONG_FROM_VERSION = "1.52.0"


class TestMigrate1_53_0(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.base_dir = Path(self.tmpdir.name)
        self.data_dir = self.base_dir / "Data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "spotify_stats.db"
        self.version_file = self.data_dir / "VERSION"

    def _stamp(self, version):
        writeDbVersion(self.db_path, version)
        self.version_file.write_text(version)

    def test_the_fixture_stays_inside_its_own_tempdir(self):
        self.assertEqual(self.data_dir.resolve().parent, self.base_dir.resolve())

    @patch("Database.Migrators.base.resolveRuntimeDir")
    def test_a_1_53_0_database_is_stamped_1_54_0_in_both_markers(self, mock_resolve):
        mock_resolve.return_value = self.data_dir
        self._stamp(FROM_VERSION)

        Migrator(FROM_VERSION, TO_VERSION).migrate()

        self.assertEqual(readDbVersion(self.db_path), TO_VERSION)
        self.assertEqual(self.version_file.read_text().strip(), TO_VERSION)

    @patch("Database.Migrators.base.resolveRuntimeDir")
    def test_a_patch_stamped_1_53_1_database_passes_the_same_minor_check(self, mock_resolve):
        """The live instance was upgraded through 1.53.1, and migrateIfNeeded
        patch-stamps the full version - so the marker this step meets reads
        1.53.1, not 1.53.0. checkPreconditions compares (major, minor)."""
        mock_resolve.return_value = self.data_dir
        self._stamp(PATCH_STAMPED_FROM_VERSION)

        Migrator(FROM_VERSION, TO_VERSION).migrate()

        self.assertEqual(readDbVersion(self.db_path), TO_VERSION)

    @patch("Database.Migrators.base.resolveRuntimeDir")
    def test_a_database_one_minor_behind_is_refused_not_stamped(self, mock_resolve):
        mock_resolve.return_value = self.data_dir
        self._stamp(WRONG_FROM_VERSION)

        with self.assertRaises(Exception) as ctx:
            Migrator(FROM_VERSION, TO_VERSION).migrate()

        self.assertIn(WRONG_FROM_VERSION, str(ctx.exception))
        self.assertEqual(readDbVersion(self.db_path), WRONG_FROM_VERSION)   #< untouched
        self.assertEqual(self.version_file.read_text().strip(), WRONG_FROM_VERSION)


if __name__ == "__main__":
    unittest.main()
