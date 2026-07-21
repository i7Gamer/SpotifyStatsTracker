"""Tests for _calculateFolderSize()'s MEDIA_FOLDER_SIZE_CACHE_TTL_SECONDS TTL
cache (Database/queries/settings.py). getGlobalDatabaseStats() is called from
the public, unauthenticated /overview page on every request; on a real media
cache (thousands of files) the underlying OS-level scan takes ~1s, so it must
not be re-run on every call within the TTL window.
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import Database.queries.settings as settingsModule
from Database.repository import Repository
from config import MEDIA_FOLDER_SIZE_CACHE_TTL_SECONDS


class TestFolderSizeCache(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)
        self.folderPath = Path(self._tmpdir.name) / "media"
        self.folderPath.mkdir()
        settingsModule._folderSizeCache.clear()

    def tearDown(self):
        settingsModule._folderSizeCache.clear()

    def test_second_call_within_ttl_uses_cache(self):
        """The expensive OS-level scan must only run once within the TTL window."""
        with patch.object(settingsModule.SettingQueries, "_calculateFolderSizeUncached",
                          wraps=self.repo._calculateFolderSizeUncached) as mockUncached:
            size1 = self.repo._calculateFolderSize(self.folderPath)
            size2 = self.repo._calculateFolderSize(self.folderPath)

        self.assertEqual(size1, size2)
        mockUncached.assert_called_once()

    def test_cache_expires_after_ttl(self):
        """After the TTL elapses, the next call must recompute, not reuse a stale value."""
        with patch.object(settingsModule.SettingQueries, "_calculateFolderSizeUncached",
                          wraps=self.repo._calculateFolderSizeUncached) as mockUncached:
            self.repo._calculateFolderSize(self.folderPath)

            # Manually prime the cache with an already-expired entry, mirroring
            # tests/test_login_cache.py's TTL-expiry pattern.
            expiredTs = time.monotonic() - 1
            settingsModule._folderSizeCache[self.folderPath] = (12345, expiredTs)

            self.repo._calculateFolderSize(self.folderPath)

        self.assertEqual(mockUncached.call_count, 2)

    def test_cache_reflects_new_files_only_after_expiry(self):
        """A file added after the first call must not be reflected until the
        cache entry expires - the whole point of caching is bounded staleness,
        not perfect real-time accuracy."""
        size1 = self.repo._calculateFolderSize(self.folderPath)

        (self.folderPath / "new_file.bin").write_bytes(b"x" * 4096)

        size2 = self.repo._calculateFolderSize(self.folderPath)
        self.assertEqual(size1, size2)   #< still cached, doesn't see the new file yet

        settingsModule._folderSizeCache.clear()
        size3 = self.repo._calculateFolderSize(self.folderPath)
        self.assertGreaterEqual(size3 - size1, 4096)

    def test_cache_is_per_path(self):
        """A cache hit for one folder must not bleed into a different folder."""
        otherPath = Path(self._tmpdir.name) / "other_media"
        otherPath.mkdir()
        (otherPath / "f.bin").write_bytes(b"x" * 2048)

        with patch.object(settingsModule.SettingQueries, "_calculateFolderSizeUncached",
                          wraps=self.repo._calculateFolderSizeUncached) as mockUncached:
            self.repo._calculateFolderSize(self.folderPath)
            self.repo._calculateFolderSize(otherPath)

        self.assertEqual(mockUncached.call_count, 2)

    def test_constant_value(self):
        self.assertEqual(MEDIA_FOLDER_SIZE_CACHE_TTL_SECONDS, 300)

    def test_cache_populated_after_first_call(self):
        self.repo._calculateFolderSize(self.folderPath)

        self.assertIn(self.folderPath, settingsModule._folderSizeCache)
        cachedSize, expiresAt = settingsModule._folderSizeCache[self.folderPath]
        self.assertGreaterEqual(cachedSize, 0)
        self.assertGreater(expiresAt, time.monotonic())


if __name__ == "__main__":
    unittest.main()
