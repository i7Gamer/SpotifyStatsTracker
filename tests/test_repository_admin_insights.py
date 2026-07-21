"""Instance-wide admin insight methods on Repository: catalog-scoped genre/
biography coverage, registration counts, share activity - the numbers behind
the /admin page's Instance Insights section. Unlike the per-user coverage
methods (getGenreCoverage, getBiographyCoverage), none of these are scoped to
a single user's plays.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository


class RepositoryAdminInsightsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

    def _insertArtist(self, artistId, bio=None):
        conn = self.repo._conn()
        with conn:
            conn.execute("INSERT INTO artists (id, name, url, bio) VALUES (?, ?, '', ?)",
                        (artistId, artistId, bio))

    def _insertAlbum(self, albumId, bio=None):
        conn = self.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, bio) VALUES (?, ?, '', ?)",
                        (albumId, albumId, bio))

    def _insertTrack(self, trackId, albumId):
        conn = self.repo._conn()
        with conn:
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES (?, ?, '', ?)",
                        (trackId, trackId, albumId))


class TestGetCatalogGenreCoverage(RepositoryAdminInsightsTestCase):
    def test_empty_catalog_is_all_zeros(self):
        coverage = self.repo.getCatalogGenreCoverage()
        for category in ("song", "album", "artist"):
            self.assertEqual(coverage[category], {
                "covered": 0, "own_covered": 0, "total": 0, "percent": 0.0, "own_percent": 0.0, "ownPercent": 0.0
            })
        self.assertEqual(coverage["overall"]["percent"], 0.0)

    def test_counts_distinct_entities_not_plays(self):
        self._insertAlbum("al1")
        self._insertAlbum("al2")
        self._insertTrack("t1", "al1")
        self._insertTrack("t2", "al2")
        self._insertArtist("a1")
        self.repo.replaceTrackGenres("t1", ["Rock"])
        self.repo.replaceAlbumGenres("al1", ["Rock"])
        self.repo.replaceArtistGenres("a1", ["Rock"])

        coverage = self.repo.getCatalogGenreCoverage()

        self.assertEqual(coverage["song"]["covered"], 1)
        self.assertEqual(coverage["song"]["own_covered"], 1)
        self.assertEqual(coverage["album"]["covered"], 1)
        self.assertEqual(coverage["artist"]["covered"], 1)
        self.assertEqual(coverage["overall"]["percent"], round((50.0 + 50.0 + 100.0) / 3, 1))

    def test_inherited_rows_excluded_when_toggle_off(self):
        self._insertAlbum("al1")
        self._insertTrack("t1", "al1")
        self.repo.replaceTrackGenres("t1", ["Rock"], inherited=True)

        withInherited = self.repo.getCatalogGenreCoverage(includeInherited=True)
        withoutInherited = self.repo.getCatalogGenreCoverage(includeInherited=False)

        self.assertEqual(withInherited["song"]["covered"], 1)
        self.assertEqual(withoutInherited["song"]["covered"], 0)

    def test_defaults_to_instance_inherited_setting(self):
        self._insertAlbum("al1")
        self._insertTrack("t1", "al1")
        self.repo.replaceTrackGenres("t1", ["Rock"], inherited=True)
        self.repo.setInheritedGenresEnabled(False)

        coverage = self.repo.getCatalogGenreCoverage()

        self.assertEqual(coverage["song"]["covered"], 0)


class TestGetCatalogBiographyCoverage(RepositoryAdminInsightsTestCase):
    def test_empty_catalog_is_all_zeros(self):
        coverage = self.repo.getCatalogBiographyCoverage()
        self.assertEqual(coverage["artist"], {"covered": 0, "total": 0})
        self.assertEqual(coverage["album"], {"covered": 0, "total": 0})

    def test_counts_entities_with_a_stored_bio(self):
        self._insertArtist("a1", bio="A bio.")
        self._insertArtist("a2", bio=None)
        self._insertAlbum("al1", bio="An album bio.")

        coverage = self.repo.getCatalogBiographyCoverage()

        self.assertEqual(coverage["artist"], {"covered": 1, "total": 2})
        self.assertEqual(coverage["album"], {"covered": 1, "total": 1})


class TestGetRecentRegistrationCounts(RepositoryAdminInsightsTestCase):
    def test_empty_instance_is_zero(self):
        counts = self.repo.getRecentRegistrationCounts()
        self.assertEqual(counts, {"last_7_days": 0, "last_30_days": 0})

    def test_buckets_by_age(self):
        now = time.time()
        self.repo.upsertUser("recent", "recent@example.com", createdAt=now - 1 * 24 * 3600)
        self.repo.upsertUser("mid", "mid@example.com", createdAt=now - 20 * 24 * 3600)
        self.repo.upsertUser("old", "old@example.com", createdAt=now - 60 * 24 * 3600)

        counts = self.repo.getRecentRegistrationCounts()

        self.assertEqual(counts["last_7_days"], 1)
        self.assertEqual(counts["last_30_days"], 2)


class TestGetInstanceShareCounts(RepositoryAdminInsightsTestCase):
    def test_empty_instance_is_zero(self):
        self.assertEqual(self.repo.getInstanceShareCounts(), {"pending": 0, "accepted": 0})

    def test_counts_across_every_user_pair(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.upsertUser("carol", "carol@example.com")
        self.repo.createShareRequest("alice", "bob")
        self.repo.createShareRequest("alice", "carol")
        self.repo.createShareRequest("bob", "alice")  # crosses -> accepted

        counts = self.repo.getInstanceShareCounts()

        self.assertEqual(counts, {"pending": 1, "accepted": 1})


class TestGetActiveShareLinksCount(RepositoryAdminInsightsTestCase):
    def test_empty_instance_is_zero(self):
        self.assertEqual(self.repo.getActiveShareLinksCount(), 0)

    def test_counts_non_expired_links_and_purges_expired_ones(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.createShareLink("alice", "wrapped", 2024, expiresInSeconds=None)
        self.repo.createShareLink("alice", "wrapped", 2025, expiresInSeconds=3600)
        expiredToken = self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=-1)

        count = self.repo.getActiveShareLinksCount()

        self.assertEqual(count, 2)
        self.assertIsNone(self.repo.getShareLink(expiredToken))


if __name__ == "__main__":
    unittest.main()
