import sqlite3
import unittest
import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository


class RepositorySharesTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        for username in ("alice", "bob", "carol"):
            self.repo.upsertUser(username, f"{username}@example.com")

    def tearDown(self):
        self.repo.connectionManager.close()
        self._tmpdir.cleanup()


class TestCreateShareRequest(RepositorySharesTestCase):
    def test_creates_a_pending_request(self):
        result = self.repo.createShareRequest("alice", "bob")

        self.assertEqual(result, "requested")
        incoming = self.repo.getPendingIncomingShares("bob")
        self.assertEqual(len(incoming), 1)
        self.assertEqual(incoming[0]["requester_username"], "alice")

    def test_repeating_the_same_pending_request_does_not_duplicate(self):
        self.repo.createShareRequest("alice", "bob")
        result = self.repo.createShareRequest("alice", "bob")

        self.assertEqual(result, "requested")
        self.assertEqual(len(self.repo.getPendingIncomingShares("bob")), 1)

    def test_reverse_pending_request_auto_accepts_instead_of_duplicating(self):
        """If alice already asked bob, and bob separately asks alice before
        responding, that's the same relationship both people want - it should
        become accepted immediately rather than leaving two independent
        pending rows sitting around."""
        self.repo.createShareRequest("alice", "bob")

        result = self.repo.createShareRequest("bob", "alice")

        self.assertEqual(result, "accepted")
        self.assertEqual(self.repo.getPendingIncomingShares("bob"), [])
        self.assertEqual(self.repo.getPendingIncomingShares("alice"), [])
        self.assertTrue(self.repo.hasAcceptedShare("alice", "bob"))

    def test_requesting_an_already_accepted_share_is_a_noop(self):
        self.repo.createShareRequest("alice", "bob")
        shareId = self.repo.getPendingIncomingShares("bob")[0]["id"]
        self.repo.respondToShareRequest(shareId, "bob", accept=True)

        result = self.repo.createShareRequest("bob", "alice")

        self.assertEqual(result, "accepted")
        self.assertEqual(len(self.repo.getAcceptedShareUsernames("alice")), 1)

    def test_self_share_is_rejected_at_the_database_level(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.createShareRequest("alice", "alice")


class TestPendingShareLists(RepositorySharesTestCase):
    def setUp(self):
        super().setUp()
        self.repo.createShareRequest("alice", "bob")
        self.repo.createShareRequest("alice", "carol")

    def test_incoming_only_lists_requests_addressed_to_that_user(self):
        bobIncoming = self.repo.getPendingIncomingShares("bob")
        aliceIncoming = self.repo.getPendingIncomingShares("alice")

        self.assertEqual([r["requester_username"] for r in bobIncoming], ["alice"])
        self.assertEqual(aliceIncoming, [])

    def test_outgoing_only_lists_requests_sent_by_that_user(self):
        aliceOutgoing = self.repo.getPendingOutgoingShares("alice")

        self.assertEqual(
            sorted(r["recipient_username"] for r in aliceOutgoing),
            ["bob", "carol"],
        )
        self.assertEqual(self.repo.getPendingOutgoingShares("bob"), [])

    def test_accepted_shares_no_longer_appear_as_pending(self):
        shareId = self.repo.getPendingIncomingShares("bob")[0]["id"]
        self.repo.respondToShareRequest(shareId, "bob", accept=True)

        self.assertEqual(self.repo.getPendingIncomingShares("bob"), [])
        self.assertEqual(
            [r["recipient_username"] for r in self.repo.getPendingOutgoingShares("alice")],
            ["carol"],
        )


class TestAcceptedShareUsernames(RepositorySharesTestCase):
    def _accept(self, requester, recipient):
        self.repo.createShareRequest(requester, recipient)
        shareId = self.repo.getPendingIncomingShares(recipient)[0]["id"]
        self.repo.respondToShareRequest(shareId, recipient, accept=True)

    def test_returns_counterpart_when_username_was_the_requester(self):
        self._accept("alice", "bob")

        self.assertEqual(self.repo.getAcceptedShareUsernames("alice"), ["bob"])

    def test_returns_counterpart_when_username_was_the_recipient(self):
        self._accept("alice", "bob")

        self.assertEqual(self.repo.getAcceptedShareUsernames("bob"), ["alice"])

    def test_pending_only_requests_are_excluded(self):
        self.repo.createShareRequest("alice", "carol")

        self.assertEqual(self.repo.getAcceptedShareUsernames("carol"), [])

    def test_multiple_accepted_shares_all_listed(self):
        self._accept("alice", "bob")
        self._accept("alice", "carol")

        self.assertEqual(sorted(self.repo.getAcceptedShareUsernames("alice")), ["bob", "carol"])


class TestGetAcceptedShares(RepositorySharesTestCase):
    def test_includes_the_share_id_alongside_the_counterpart(self):
        self.repo.createShareRequest("alice", "bob")
        shareId = self.repo.getPendingIncomingShares("bob")[0]["id"]
        self.repo.respondToShareRequest(shareId, "bob", accept=True)

        shares = self.repo.getAcceptedShares("alice")

        self.assertEqual(shares, [{"id": shareId, "counterpart": "bob"}])

    def test_pending_only_requests_are_excluded(self):
        self.repo.createShareRequest("alice", "carol")

        self.assertEqual(self.repo.getAcceptedShares("carol"), [])


class TestHasAcceptedShare(RepositorySharesTestCase):
    def test_true_once_accepted_regardless_of_argument_order(self):
        self.repo.createShareRequest("alice", "bob")
        shareId = self.repo.getPendingIncomingShares("bob")[0]["id"]
        self.repo.respondToShareRequest(shareId, "bob", accept=True)

        self.assertTrue(self.repo.hasAcceptedShare("alice", "bob"))
        self.assertTrue(self.repo.hasAcceptedShare("bob", "alice"))

    def test_false_while_only_pending(self):
        self.repo.createShareRequest("alice", "bob")

        self.assertFalse(self.repo.hasAcceptedShare("alice", "bob"))

    def test_false_with_no_relationship_at_all(self):
        self.assertFalse(self.repo.hasAcceptedShare("alice", "carol"))


class TestRespondToShareRequest(RepositorySharesTestCase):
    def setUp(self):
        super().setUp()
        self.repo.createShareRequest("alice", "bob")
        self.shareId = self.repo.getPendingIncomingShares("bob")[0]["id"]

    def test_accept_marks_the_row_accepted(self):
        result = self.repo.respondToShareRequest(self.shareId, "bob", accept=True)

        self.assertTrue(result)
        self.assertTrue(self.repo.hasAcceptedShare("alice", "bob"))

    def test_decline_removes_the_row_entirely(self):
        result = self.repo.respondToShareRequest(self.shareId, "bob", accept=False)

        self.assertTrue(result)
        self.assertEqual(self.repo.getPendingIncomingShares("bob"), [])
        self.assertFalse(self.repo.hasAcceptedShare("alice", "bob"))

    def test_someone_other_than_the_recipient_cannot_respond(self):
        result = self.repo.respondToShareRequest(self.shareId, "carol", accept=True)

        self.assertFalse(result)
        self.assertFalse(self.repo.hasAcceptedShare("alice", "bob"))

    def test_the_requester_cannot_respond_to_their_own_request(self):
        result = self.repo.respondToShareRequest(self.shareId, "alice", accept=True)

        self.assertFalse(result)

    def test_cannot_respond_to_an_already_accepted_share(self):
        self.repo.respondToShareRequest(self.shareId, "bob", accept=True)

        result = self.repo.respondToShareRequest(self.shareId, "bob", accept=False)

        self.assertFalse(result)
        self.assertTrue(self.repo.hasAcceptedShare("alice", "bob"))


class TestCancelShareRequest(RepositorySharesTestCase):
    def setUp(self):
        super().setUp()
        self.repo.createShareRequest("alice", "bob")
        self.shareId = self.repo.getPendingIncomingShares("bob")[0]["id"]

    def test_requester_can_cancel_their_own_pending_request(self):
        result = self.repo.cancelShareRequest(self.shareId, "alice")

        self.assertTrue(result)
        self.assertEqual(self.repo.getPendingOutgoingShares("alice"), [])

    def test_the_recipient_cannot_cancel_it(self):
        result = self.repo.cancelShareRequest(self.shareId, "bob")

        self.assertFalse(result)
        self.assertEqual(len(self.repo.getPendingOutgoingShares("alice")), 1)

    def test_cannot_cancel_an_already_accepted_share(self):
        self.repo.respondToShareRequest(self.shareId, "bob", accept=True)

        result = self.repo.cancelShareRequest(self.shareId, "alice")

        self.assertFalse(result)
        self.assertTrue(self.repo.hasAcceptedShare("alice", "bob"))


class TestRevokeShare(RepositorySharesTestCase):
    def setUp(self):
        super().setUp()
        self.repo.createShareRequest("alice", "bob")
        shareId = self.repo.getPendingIncomingShares("bob")[0]["id"]
        self.repo.respondToShareRequest(shareId, "bob", accept=True)
        self.shareId = shareId

    def test_the_requester_can_revoke(self):
        result = self.repo.revokeShare(self.shareId, "alice")

        self.assertTrue(result)
        self.assertFalse(self.repo.hasAcceptedShare("alice", "bob"))

    def test_the_recipient_can_also_revoke(self):
        result = self.repo.revokeShare(self.shareId, "bob")

        self.assertTrue(result)
        self.assertFalse(self.repo.hasAcceptedShare("alice", "bob"))

    def test_an_unrelated_user_cannot_revoke(self):
        result = self.repo.revokeShare(self.shareId, "carol")

        self.assertFalse(result)
        self.assertTrue(self.repo.hasAcceptedShare("alice", "bob"))

    def test_cannot_revoke_a_merely_pending_request(self):
        self.repo.createShareRequest("alice", "carol")
        pendingId = self.repo.getPendingIncomingShares("carol")[0]["id"]

        result = self.repo.revokeShare(pendingId, "alice")

        self.assertFalse(result)
        self.assertEqual(len(self.repo.getPendingIncomingShares("carol")), 1)


class TestGetAllUsernamesExcept(RepositorySharesTestCase):
    def test_excludes_only_the_given_username(self):
        others = self.repo.getAllUsernamesExcept("alice")

        self.assertEqual(others, ["bob", "carol"])

    def test_does_not_pull_sensitive_columns(self):
        """This backs a plain "who can I share with" dropdown - it must not
        reuse getAllUsersDetails()'s SELECT, which includes cookies_json/
        spotify_refresh_token that this list has no reason to touch."""
        self.repo.setUserCookies("bob", {"sp_dc": "secret"})

        others = self.repo.getAllUsernamesExcept("alice")

        self.assertEqual(others, ["bob", "carol"])
        self.assertTrue(all(isinstance(u, str) for u in others))


if __name__ == "__main__":
    unittest.main()
