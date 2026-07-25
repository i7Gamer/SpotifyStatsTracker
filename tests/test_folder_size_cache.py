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


#< a folder's "size", keyed off its name so two paths can't be confused for one
def _fakeSizeFor(folderPath):
    return 1000 + len(Path(folderPath).name)


class FolderSizeCacheTestCase(unittest.TestCase):
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


class TestFolderSizeCacheBehaviour(FolderSizeCacheTestCase):
    """When the wrapper calls through, and what it keys on.

    The uncached scan is stubbed here rather than wrapped: on Windows it spawns
    a PowerShell subprocess (~180 ms), and these tests assert call COUNTS, not
    byte counts - they were paying a real process launch per scan to prove the
    wrapper's bookkeeping. TestFolderSizeRealScan below still exercises the real
    implementation end to end.
    """

    def _stubbedScan(self):
        return patch.object(settingsModule.SettingQueries, "_calculateFolderSizeUncached",
                            side_effect=_fakeSizeFor)

    def test_second_call_within_ttl_uses_cache(self):
        """The expensive OS-level scan must only run once within the TTL window."""
        with self._stubbedScan() as mockUncached:
            size1 = self.repo._calculateFolderSize(self.folderPath)
            size2 = self.repo._calculateFolderSize(self.folderPath)

        self.assertEqual(size1, size2)
        mockUncached.assert_called_once()

    def test_cache_expires_after_ttl(self):
        """After the TTL elapses, the next call must recompute, not reuse a stale value."""
        with self._stubbedScan() as mockUncached:
            self.repo._calculateFolderSize(self.folderPath)

            # Prime the cache with an already-expired entry rather than sleeping
            # out the TTL, mirroring tests/test_login_cache.py's pattern.
            expiredTs = time.monotonic() - 1
            settingsModule._folderSizeCache[self.folderPath] = (12345, expiredTs)

            self.repo._calculateFolderSize(self.folderPath)

        self.assertEqual(mockUncached.call_count, 2)

    def test_expired_entry_is_replaced_by_the_fresh_value(self):
        """Recomputing is only half the contract - the stale number must not
        survive in the cache or be returned."""
        with self._stubbedScan():
            settingsModule._folderSizeCache[self.folderPath] = (12345, time.monotonic() - 1)

            size = self.repo._calculateFolderSize(self.folderPath)

        self.assertEqual(size, _fakeSizeFor(self.folderPath))
        self.assertEqual(settingsModule._folderSizeCache[self.folderPath][0],
                         _fakeSizeFor(self.folderPath))

    def test_cache_is_per_path(self):
        """A cache hit for one folder must not bleed into a different folder."""
        otherPath = Path(self._tmpdir.name) / "other_media"
        otherPath.mkdir()

        with self._stubbedScan() as mockUncached:
            ownSize = self.repo._calculateFolderSize(self.folderPath)
            otherSize = self.repo._calculateFolderSize(otherPath)

        self.assertEqual(mockUncached.call_count, 2)
        #< and the values stayed with their own path, not just the call count
        self.assertEqual(ownSize, _fakeSizeFor(self.folderPath))
        self.assertEqual(otherSize, _fakeSizeFor(otherPath))

    def test_constant_value(self):
        self.assertEqual(MEDIA_FOLDER_SIZE_CACHE_TTL_SECONDS, 300)

    def test_cache_populated_after_first_call(self):
        with self._stubbedScan():
            self.repo._calculateFolderSize(self.folderPath)

        self.assertIn(self.folderPath, settingsModule._folderSizeCache)
        cachedSize, expiresAt = settingsModule._folderSizeCache[self.folderPath]
        self.assertGreaterEqual(cachedSize, 0)
        self.assertGreater(expiresAt, time.monotonic())


class TestFolderSizeRealScan(FolderSizeCacheTestCase):
    """The one place the real OS-level scan runs, so the stubs above can't hide
    a broken implementation: it must return real byte counts, and the cache must
    hand back the same number until it's cleared."""

    def test_real_scan_reports_bytes_and_is_then_cached(self):
        size1 = self.repo._calculateFolderSize(self.folderPath)

        (self.folderPath / "new_file.bin").write_bytes(b"x" * 4096)

        size2 = self.repo._calculateFolderSize(self.folderPath)
        self.assertEqual(size1, size2)   #< still cached, doesn't see the new file yet

        settingsModule._folderSizeCache.clear()
        size3 = self.repo._calculateFolderSize(self.folderPath)
        self.assertGreaterEqual(size3 - size1, 4096)


if __name__ == "__main__":
    unittest.main()
