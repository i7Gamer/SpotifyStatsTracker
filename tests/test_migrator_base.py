"""BaseMigrator.checkPreconditions() must compare the full (major, minor)
version, not just the minor component - a minor-only comparison would treat
e.g. database "1.7.0" vs app "2.7.0" (same minor, different major) as
compatible, silently letting a migrator run against the wrong major version.
"""
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
from Database.Migrators.base import BaseMigrator


class MigratorBaseTestCase(unittest.TestCase):
    """Mirrors test_migrate1_7_0.py's setup: BaseMigrator.baseDir resolves
    against base.py's own __file__, so that has to be patched to point at a
    temp Database/Migrators/ directory with its own VERSION files."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

    def _writeVersions(self, appVersion, databaseVersion):
        (self.root / "Database" / "VERSION").write_text(appVersion, encoding="utf-8")
        (self.dataDir / "VERSION").write_text(databaseVersion, encoding="utf-8")


class TestGetMajorMinor(unittest.TestCase):
    def test_extracts_major_and_minor_as_ints(self):
        self.assertEqual(BaseMigrator.getMajorMinor("1.7.0"), (1, 7))

    def test_ignores_a_trailing_patch_or_extra_component(self):
        self.assertEqual(BaseMigrator.getMajorMinor("2.0.5"), (2, 0))


class TestCheckPreconditions(MigratorBaseTestCase):
    def test_passes_when_db_is_one_minor_behind_within_the_same_major(self):
        self._writeVersions(appVersion="1.8.0", databaseVersion="1.7.0")
        BaseMigrator().checkPreconditions()  # must not raise

    def test_raises_on_an_identical_minor_different_major_mismatch(self):
        """This is the exact case a minor-only comparison used to miss:
        getMiddleVersion("1.7.0") == getMiddleVersion("2.7.0") == 7, so the old
        check never even looked at the major component to notice these belong
        to different major versions entirely."""
        self._writeVersions(appVersion="2.7.0", databaseVersion="1.7.0")
        with self.assertRaises(Exception):
            BaseMigrator().checkPreconditions()

    def test_raises_when_db_is_more_than_one_minor_behind(self):
        self._writeVersions(appVersion="1.9.0", databaseVersion="1.7.0")
        with self.assertRaises(Exception):
            BaseMigrator().checkPreconditions()

    def test_raises_when_major_differs_even_if_minor_plus_one_matches(self):
        """Same shape as the pre-fix bug: minor 6+1==7 lines up, but major 1
        vs 2 must still be rejected."""
        self._writeVersions(appVersion="2.7.0", databaseVersion="1.6.0")
        with self.assertRaises(Exception):
            BaseMigrator().checkPreconditions()


if __name__ == "__main__":
    unittest.main()
