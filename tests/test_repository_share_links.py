import time
import unittest
import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.repository as repositoryModule
from Database.repository import Repository


def _frozenAt(timestamp):
    """Patches the `time` module reference inside Database.repository (not
    the global stdlib module) so time.time() returns a fixed value - mirrors
    the patch.object(lastfm, "time", ...) convention in test_lastfm_client.py."""
    fakeTime = MagicMock()
    fakeTime.time.return_value = timestamp
    return patch.object(repositoryModule, "time", fakeTime)


class RepositoryShareLinksTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")

    def tearDown(self):
        self.repo.connectionManager.close()
        self._tmpdir.cleanup()


class TestCreateAndGetShareLink(RepositoryShareLinksTestCase):
    def test_create_returns_a_token_that_looks_up_to_the_same_link(self):
        token = self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)

        link = self.repo.getShareLink(token)

        self.assertIsNotNone(link)
        self.assertEqual(link["username"], "alice")
        self.assertEqual(link["kind"], "wrapped")
        self.assertEqual(link["year"], 2026)
        self.assertIsNone(link["expires_at"])

    def test_two_links_get_distinct_tokens(self):
        tokenA = self.repo.createShareLink("alice", "wrapped", 2025, expiresInSeconds=None)
        tokenB = self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)

        self.assertNotEqual(tokenA, tokenB)

    def test_never_expires_when_expires_in_seconds_is_none(self):
        token = self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)

        link = self.repo.getShareLink(token)

        self.assertIsNone(link["expires_at"])

    def test_expiry_is_stamped_relative_to_creation_time(self):
        before = time.time()
        token = self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=3600)
        after = time.time()

        link = self.repo.getShareLink(token)

        self.assertGreaterEqual(link["expires_at"], before + 3600)
        self.assertLessEqual(link["expires_at"], after + 3600)

    def test_unknown_token_returns_none(self):
        self.assertIsNone(self.repo.getShareLink("does-not-exist"))

    def test_year_none_creates_an_all_years_link(self):
        token = self.repo.createShareLink("alice", "wrapped", None, expiresInSeconds=None)

        link = self.repo.getShareLink(token)

        self.assertIsNotNone(link)
        self.assertIsNone(link["year"])

    def test_all_years_link_and_a_per_year_link_can_coexist(self):
        self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)
        self.repo.createShareLink("alice", "wrapped", None, expiresInSeconds=None)

        links = self.repo.getShareLinksForUser("alice")

        self.assertEqual(len(links), 2)


class TestExpiredShareLinkLazyDeletion(RepositoryShareLinksTestCase):
    def test_expired_token_returns_none(self):
        token = self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=1)

        with _frozenAt(time.time() + 3600):
            link = self.repo.getShareLink(token)

        self.assertIsNone(link)

    def test_expired_token_is_actually_deleted_not_just_filtered(self):
        """A lookup on an expired token must remove the row (lazy deletion),
        not merely hide it - otherwise Profile's link list would still show
        a dead link forever."""
        token = self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=1)

        with _frozenAt(time.time() + 3600):
            self.repo.getShareLink(token)

        self.assertEqual(self.repo.getShareLinksForUser("alice"), [])

    def test_unexpired_token_survives_a_lookup(self):
        token = self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=3600)

        link = self.repo.getShareLink(token)

        self.assertIsNotNone(link)
        self.assertEqual(len(self.repo.getShareLinksForUser("alice")), 1)

    def test_expiring_one_token_does_not_delete_a_different_users_unexpired_token(self):
        expiredToken = self.repo.createShareLink("alice", "wrapped", 2025, expiresInSeconds=1)
        liveToken = self.repo.createShareLink("bob", "wrapped", 2025, expiresInSeconds=3600)

        with _frozenAt(time.time() + 3600):
            self.repo.getShareLink(expiredToken)

        self.assertIsNotNone(self.repo.getShareLink(liveToken))


class TestGetShareLinksForUser(RepositoryShareLinksTestCase):
    def test_orders_newest_year_first(self):
        self.repo.createShareLink("alice", "wrapped", 2023, expiresInSeconds=None)
        self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)
        self.repo.createShareLink("alice", "wrapped", 2024, expiresInSeconds=None)

        years = [link["year"] for link in self.repo.getShareLinksForUser("alice")]

        self.assertEqual(years, [2026, 2024, 2023])

    def test_only_returns_the_given_users_links(self):
        self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)
        self.repo.createShareLink("bob", "wrapped", 2026, expiresInSeconds=None)

        aliceLinks = self.repo.getShareLinksForUser("alice")

        self.assertEqual(len(aliceLinks), 1)

    def test_no_links_returns_empty_list(self):
        self.assertEqual(self.repo.getShareLinksForUser("alice"), [])

    def test_expired_links_are_lazily_dropped_from_the_list(self):
        """Listing must not keep showing a link as active once it's expired -
        otherwise Profile would offer 'Revoke' on a link visiting it would
        already 404 for (see getShareLink's identical lazy-deletion)."""
        self.repo.createShareLink("alice", "wrapped", 2025, expiresInSeconds=-10)
        self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)

        years = [link["year"] for link in self.repo.getShareLinksForUser("alice")]

        self.assertEqual(years, [2026])

    def test_all_years_link_sorts_after_every_dated_link(self):
        self.repo.createShareLink("alice", "wrapped", 2023, expiresInSeconds=None)
        self.repo.createShareLink("alice", "wrapped", None, expiresInSeconds=None)
        self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)

        years = [link["year"] for link in self.repo.getShareLinksForUser("alice")]

        self.assertEqual(years, [2026, 2023, None])

    def test_expired_link_listing_does_not_affect_a_different_users_links(self):
        self.repo.createShareLink("alice", "wrapped", 2025, expiresInSeconds=-10)
        self.repo.createShareLink("bob", "wrapped", 2025, expiresInSeconds=3600)

        self.repo.getShareLinksForUser("alice")

        self.assertEqual(len(self.repo.getShareLinksForUser("bob")), 1)

    def test_multiple_links_in_the_same_year_all_appear(self):
        tokenA = self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)
        tokenB = self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=7 * 24 * 3600)
        tokenC = self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=30 * 24 * 3600)

        links = self.repo.getShareLinksForUser("alice")

        self.assertEqual(len(links), 3)
        self.assertEqual({link["token"] for link in links}, {tokenA, tokenB, tokenC})


class TestCountActiveShareLinksForBucket(RepositoryShareLinksTestCase):
    def test_zero_when_bucket_is_empty(self):
        self.assertEqual(self.repo.countActiveShareLinksForBucket("alice", "wrapped", 2026), 0)

    def test_counts_only_the_matching_year_bucket(self):
        self.repo.createShareLink("alice", "wrapped", 2025, expiresInSeconds=None)
        self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)

        self.assertEqual(self.repo.countActiveShareLinksForBucket("alice", "wrapped", 2026), 1)

    def test_counts_the_all_years_bucket_when_year_is_none(self):
        self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)
        self.repo.createShareLink("alice", "wrapped", None, expiresInSeconds=None)

        self.assertEqual(self.repo.countActiveShareLinksForBucket("alice", "wrapped", None), 1)

    def test_does_not_count_a_different_users_links(self):
        self.repo.createShareLink("bob", "wrapped", 2026, expiresInSeconds=None)

        self.assertEqual(self.repo.countActiveShareLinksForBucket("alice", "wrapped", 2026), 0)

    def test_expired_links_do_not_count(self):
        self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=-10)

        self.assertEqual(self.repo.countActiveShareLinksForBucket("alice", "wrapped", 2026), 0)

    def test_counts_up_to_the_cap_and_beyond(self):
        """The repository itself enforces no cap - creating a 6th link in
        the same bucket still succeeds and is counted, confirming the cap is
        purely an app.py-level gate (see SHARE_LINK_MAX_PER_BUCKET)."""
        for _ in range(6):
            self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)

        self.assertEqual(self.repo.countActiveShareLinksForBucket("alice", "wrapped", 2026), 6)


class TestRevokeShareLink(RepositoryShareLinksTestCase):
    def test_owner_can_revoke(self):
        token = self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)
        linkId = self.repo.getShareLink(token)["id"]

        result = self.repo.revokeShareLink(linkId, "alice")

        self.assertTrue(result)
        self.assertIsNone(self.repo.getShareLink(token))

    def test_non_owner_cannot_revoke(self):
        token = self.repo.createShareLink("alice", "wrapped", 2026, expiresInSeconds=None)
        linkId = self.repo.getShareLink(token)["id"]

        result = self.repo.revokeShareLink(linkId, "bob")

        self.assertFalse(result)
        self.assertIsNotNone(self.repo.getShareLink(token))

    def test_revoking_an_unknown_id_returns_false(self):
        self.assertFalse(self.repo.revokeShareLink(999, "alice"))


if __name__ == "__main__":
    unittest.main()
