"""users.is_admin - the single-admin model backing admin-only surfaces.

See docs/proposal-admin-and-share-links.md: the earliest-created user is
promoted when no admin exists (the person who set the instance up), and the
ADMIN_EMAIL env var is the explicit/recovery path (handled in app.py).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository


class RepositoryAdminTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)


class TestIsAdmin(RepositoryAdminTestCase):
    def test_new_users_are_not_admins(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.assertFalse(self.repo.isAdmin("alice"))

    def test_unknown_user_is_not_admin(self):
        self.assertFalse(self.repo.isAdmin("ghost"))

    def test_set_and_unset_admin(self):
        self.repo.upsertUser("alice", "alice@example.com")

        self.repo.setUserAdmin("alice", True)
        self.assertTrue(self.repo.isAdmin("alice"))

        self.repo.setUserAdmin("alice", False)
        self.assertFalse(self.repo.isAdmin("alice"))

    def test_get_admin_usernames(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        self.assertEqual(self.repo.getAdminUsernames(), [])

        self.repo.setUserAdmin("alice", True)
        self.assertEqual(self.repo.getAdminUsernames(), ["alice"])


class TestPromoteEarliestUser(RepositoryAdminTestCase):
    def test_promotes_the_earliest_created_user(self):
        self.repo.upsertUser("newer", "newer@example.com", createdAt=200.0)
        self.repo.upsertUser("older", "older@example.com", createdAt=100.0)

        promoted = self.repo.promoteEarliestUserToAdminIfNoneExists()

        self.assertEqual(promoted, "older")
        self.assertTrue(self.repo.isAdmin("older"))
        self.assertFalse(self.repo.isAdmin("newer"))

    def test_noop_when_an_admin_already_exists(self):
        self.repo.upsertUser("older", "older@example.com", createdAt=100.0)
        self.repo.upsertUser("admin", "admin@example.com", createdAt=200.0)
        self.repo.setUserAdmin("admin", True)

        promoted = self.repo.promoteEarliestUserToAdminIfNoneExists()

        self.assertIsNone(promoted)
        self.assertFalse(self.repo.isAdmin("older"), "must not create a second admin")

    def test_noop_when_there_are_no_users(self):
        self.assertIsNone(self.repo.promoteEarliestUserToAdminIfNoneExists())


class TestGetAllUsersDetailsFilter(RepositoryAdminTestCase):
    def test_username_filter_returns_only_that_user(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")

        rows = self.repo.getAllUsersDetails(username="alice")

        self.assertEqual([r["username"] for r in rows], ["alice"])

    def test_no_filter_returns_everyone(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")

        rows = self.repo.getAllUsersDetails()

        self.assertEqual({r["username"] for r in rows}, {"alice", "bob"})

    def test_includes_is_admin_as_bool(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.setUserAdmin("alice", True)

        rows = {r["username"]: r["is_admin"] for r in self.repo.getAllUsersDetails()}

        self.assertIs(rows["alice"], True)
        self.assertIs(rows["bob"], False)

    def test_includes_spotify_needs_reauth(self):
        """/admin's users table can only show a needs-reauth badge if this
        row carries the flag - it was missing from the SELECT entirely."""
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.setSpotifyNeedsReauth("alice", True)
        self.repo.upsertUser("bob", "bob@example.com")

        rows = {r["username"]: r["spotify_needs_reauth"] for r in self.repo.getAllUsersDetails()}

        self.assertTrue(rows["alice"])
        self.assertFalse(rows["bob"])


if __name__ == "__main__":
    unittest.main()
