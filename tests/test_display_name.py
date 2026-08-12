"""users.display_name - the editable label that stands in for the immutable key.

`username` is the primary key eight tables reference by foreign key, so it can
never change. display_name is the user-facing name shown wherever a person is
named; NULL means "fall back to the username", which is what every account
starts as.

The uniqueness rule is the part worth testing hard: a display name must not
collide with another account's display name OR with another account's actual
username, case-insensitively. The second half is the one that isn't obvious -
the admin console, /compare?with= and the share picker identify people by the
real username, so being allowed to display as someone else's username would be
an impersonation vector inside the share flow.
"""
import unittest
import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository


class DisplayNameTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.repo.upsertUser("timo", "timo@example.com")
        self.repo.upsertUser("alice", "alice@example.com")

    def tearDown(self):
        self.repo.connectionManager.close()
        self._tmpdir.cleanup()


class TestFallback(DisplayNameTestCase):
    def test_an_untouched_account_displays_as_its_username(self):
        self.assertEqual(self.repo.getDisplayName("timo"), "timo")

    def test_an_unknown_username_falls_back_to_itself(self):
        """Templates render whatever name a row carries; a stale or deleted
        counterpart must degrade to the raw name, never to None or a crash."""
        self.assertEqual(self.repo.getDisplayName("ghost"), "ghost")

    def test_a_stored_name_wins_over_the_username(self):
        self.repo.setDisplayName("timo", "Timo R")

        self.assertEqual(self.repo.getDisplayName("timo"), "Timo R")

    def test_clearing_reverts_to_the_username(self):
        self.repo.setDisplayName("timo", "Timo R")

        self.assertTrue(self.repo.setDisplayName("timo", None))

        self.assertEqual(self.repo.getDisplayName("timo"), "timo")


class TestBulkLookup(DisplayNameTestCase):
    def test_it_resolves_set_and_unset_names_together(self):
        self.repo.setDisplayName("alice", "Alice A")

        names = self.repo.getDisplayNames(["timo", "alice"])

        self.assertEqual(names, {"timo": "timo", "alice": "Alice A"})

    def test_an_unknown_name_is_absent_rather_than_invented(self):
        """Callers memoize on this dict; inventing a key for a user that isn't
        there would cache a fiction. getDisplayName is the forgiving one."""
        names = self.repo.getDisplayNames(["timo", "ghost"])

        self.assertEqual(names, {"timo": "timo"})

    def test_an_empty_request_costs_no_query(self):
        self.assertEqual(self.repo.getDisplayNames([]), {})


class TestUniqueness(DisplayNameTestCase):
    def test_another_users_display_name_is_taken(self):
        self.repo.setDisplayName("alice", "Shared Name")

        self.assertFalse(self.repo.setDisplayName("timo", "Shared Name"))

        self.assertEqual(self.repo.getDisplayName("timo"), "timo")

    def test_the_collision_check_ignores_case(self):
        self.repo.setDisplayName("alice", "Shared Name")

        self.assertFalse(self.repo.setDisplayName("timo", "sHaReD nAmE"))

    def test_another_users_username_is_taken_too(self):
        """The impersonation case: /admin, ?with= and the share picker all name
        people by the real username, so displaying as one is a real confusion."""
        self.assertFalse(self.repo.setDisplayName("timo", "alice"))

        self.assertEqual(self.repo.getDisplayName("timo"), "timo")

    def test_another_users_username_is_taken_case_insensitively(self):
        self.assertFalse(self.repo.setDisplayName("timo", "ALICE"))

    def test_your_own_username_is_always_available_to_you(self):
        """The guard excludes your own row, so re-cased versions of your own
        name - the single most likely thing someone types here - work."""
        self.assertTrue(self.repo.setDisplayName("timo", "Timo"))

        self.assertEqual(self.repo.getDisplayName("timo"), "Timo")

    def test_resaving_your_own_display_name_is_not_a_collision(self):
        self.repo.setDisplayName("timo", "Timo R")

        self.assertTrue(self.repo.setDisplayName("timo", "Timo R"))

    def test_clearing_is_never_blocked_by_another_user(self):
        """NULL is not a value that can collide - several accounts hold it at
        once - so the guard must not apply to a clear."""
        self.repo.setDisplayName("alice", "Alice A")
        self.repo.setDisplayName("timo", "Timo R")

        self.assertTrue(self.repo.setDisplayName("timo", None))

    def test_a_freed_name_becomes_available_again(self):
        self.repo.setDisplayName("alice", "Shared Name")
        self.repo.setDisplayName("alice", None)

        self.assertTrue(self.repo.setDisplayName("timo", "Shared Name"))


class TestWriteContract(DisplayNameTestCase):
    def test_an_unknown_user_reports_failure_rather_than_raising(self):
        """The route turns False into a form error; an exception there would be
        a 500 for what is just a stale session."""
        self.assertFalse(self.repo.setDisplayName("ghost", "Ghost"))

    def test_a_rejected_write_leaves_the_other_account_untouched(self):
        self.repo.setDisplayName("alice", "Shared Name")

        self.repo.setDisplayName("timo", "Shared Name")

        self.assertEqual(self.repo.getDisplayName("alice"), "Shared Name")


class TestAdminListing(DisplayNameTestCase):
    def test_the_repository_rows_carry_the_display_name(self):
        """The column /admin's users table is built from. This asserts the
        REPOSITORY only - it issues no request, so it cannot see whether the
        route passes the column on (it did not, and this test passed anyway
        while claiming to cover the page). The rendering contract lives in
        tests/test_admin_route.py::TestAdminUsersTable, one page-level
        assertion per display-name state."""
        self.repo.setDisplayName("timo", "Timo R")

        rows = {row["username"]: row for row in self.repo.getAllUsersDetails()}

        self.assertEqual(rows["timo"]["display_name"], "Timo R")
        self.assertIsNone(rows["alice"]["display_name"])


if __name__ == "__main__":
    unittest.main()
