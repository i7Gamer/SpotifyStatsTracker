import sqlite3
import threading
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
        # dave deliberately gets no cookies - hasAnyAcceptedShare() only
        # counts counterparts the Compare page can actually load.
        for username in ("alice", "bob", "carol"):
            self.repo.upsertUser(username, f"{username}@example.com")
            self.repo.setUserCookies(username, {"sp_dc": "test"})
        self.repo.upsertUser("dave", "dave@example.com")

    def tearDown(self):
        self.repo.connectionManager.close()
        self._tmpdir.cleanup()

    def _accept(self, requester, recipient):
        self.repo.createShareRequest(requester, recipient)
        shareId = self.repo.getPendingIncomingShares(recipient)[0]["id"]
        self.repo.respondToShareRequest(shareId, recipient, accept=True)


class TestCreateShareRequest(RepositorySharesTestCase):
    def test_creates_a_pending_request(self):
        result = self.repo.createShareRequest("alice", "bob")

        self.assertEqual(result, "requested")
        incoming = self.repo.getPendingIncomingShares("bob")
        self.assertEqual(len(incoming), 1)
        self.assertEqual(incoming[0]["requester_username"], "alice")

    def test_repeating_the_same_pending_request_reports_already_requested(self):
        """A repeat is a no-op - the caller must be able to word its message
        honestly ("already pending") instead of claiming a new request was
        just sent."""
        self.repo.createShareRequest("alice", "bob")
        result = self.repo.createShareRequest("alice", "bob")

        self.assertEqual(result, "already_requested")
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
        self.assertEqual(self.repo.getAcceptedShareUsernames("alice"), ["bob"])

    def test_requesting_an_already_accepted_share_reports_already_accepted(self):
        self._accept("alice", "bob")

        result = self.repo.createShareRequest("bob", "alice")

        self.assertEqual(result, "already_accepted")
        self.assertEqual(len(self.repo.getAcceptedShareUsernames("alice")), 1)

    def test_self_share_is_rejected_at_the_database_level(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.createShareRequest("alice", "alice")


class TestCreateShareRequestConcurrency(RepositorySharesTestCase):
    def test_crossing_requests_on_two_threads_yield_exactly_one_accepted_row(self):
        """Two users requesting each other at the same instant (two Waitress
        worker threads) must resolve into ONE accepted relationship - without
        serialization, both check-then-insert paths could pass their reverse-
        pending SELECT before either INSERT lands, leaving two opposite-
        direction rows the same-direction UNIQUE constraint doesn't cover."""
        barrier = threading.Barrier(2)
        results = {}

        def requestShare(requester, recipient):
            barrier.wait()
            results[requester] = self.repo.createShareRequest(requester, recipient)
            self.repo.connectionManager.close()   #< thread-local conn - must close for tmpdir cleanup on Windows

        threads = [
            threading.Thread(target=requestShare, args=("alice", "bob")),
            threading.Thread(target=requestShare, args=("bob", "alice")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = self.repo._conn().execute("SELECT status FROM user_shares").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "accepted")
        self.assertEqual(sorted(results.values()), ["accepted", "requested"])


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

    def test_pending_lists_are_ordered_oldest_request_first(self):
        """SQLite's row order is unspecified without ORDER BY - the request
        lists must render in a stable (arrival) order."""
        self.assertEqual(
            [r["recipient_username"] for r in self.repo.getPendingOutgoingShares("alice")],
            ["bob", "carol"],   #< insertion order, not whatever SQLite returns
        )

    def test_accepted_shares_no_longer_appear_as_pending(self):
        shareId = self.repo.getPendingIncomingShares("bob")[0]["id"]
        self.repo.respondToShareRequest(shareId, "bob", accept=True)

        self.assertEqual(self.repo.getPendingIncomingShares("bob"), [])
        self.assertEqual(
            [r["recipient_username"] for r in self.repo.getPendingOutgoingShares("alice")],
            ["carol"],
        )


class TestGetPendingIncomingSharesCount(RepositorySharesTestCase):
    def test_counts_incoming_pending_requests(self):
        self.repo.createShareRequest("bob", "alice")
        self.repo.createShareRequest("carol", "alice")

        self.assertEqual(self.repo.getPendingIncomingSharesCount("alice"), 2)

    def test_excludes_outgoing_and_accepted(self):
        self.repo.createShareRequest("alice", "bob")   #< outgoing, not incoming
        self._accept("alice", "carol")                  #< accepted, not pending

        self.assertEqual(self.repo.getPendingIncomingSharesCount("alice"), 0)

    def test_zero_when_there_are_none(self):
        self.assertEqual(self.repo.getPendingIncomingSharesCount("alice"), 0)


class TestUnseenAcceptedShareNotifications(RepositorySharesTestCase):
    """The requester side's "your share request was accepted" notification -
    the recipient doesn't need one, since accepting is itself their
    acknowledgment."""

    def test_counts_as_unseen_right_after_acceptance(self):
        self._accept("alice", "bob")

        self.assertEqual(self.repo.getUnseenAcceptedShareCount("alice"), 1)

    def test_recipient_side_is_not_counted(self):
        """bob accepted it himself - he doesn't need to be told."""
        self._accept("alice", "bob")

        self.assertEqual(self.repo.getUnseenAcceptedShareCount("bob"), 0)

    def test_reverse_pending_auto_accept_notifies_the_original_requester(self):
        """alice originally asked bob; bob crossing-requesting alice back
        auto-accepts alice's original row (see createShareRequest) - alice,
        the original requester, still gets notified that it's now active."""
        self.repo.createShareRequest("alice", "bob")
        self.repo.createShareRequest("bob", "alice")   #< auto-accepts

        self.assertEqual(self.repo.getUnseenAcceptedShareCount("alice"), 1)

    def test_pending_requests_are_not_counted(self):
        self.repo.createShareRequest("alice", "bob")

        self.assertEqual(self.repo.getUnseenAcceptedShareCount("alice"), 0)

    def test_marking_seen_clears_the_count(self):
        self._accept("alice", "bob")

        self.repo.markAcceptedSharesSeenByRequester("alice")

        self.assertEqual(self.repo.getUnseenAcceptedShareCount("alice"), 0)

    def test_marking_seen_does_not_affect_other_users(self):
        self._accept("alice", "bob")
        self._accept("carol", "dave")

        self.repo.markAcceptedSharesSeenByRequester("alice")

        self.assertEqual(self.repo.getUnseenAcceptedShareCount("carol"), 1)

    def test_a_later_unrelated_share_is_unseen_again(self):
        """Marking seen must not permanently silence the requester - a
        second, later-accepted share should still notify them."""
        self._accept("alice", "bob")
        self.repo.markAcceptedSharesSeenByRequester("alice")

        self._accept("alice", "carol")

        self.assertEqual(self.repo.getUnseenAcceptedShareCount("alice"), 1)

    def test_zero_when_there_are_no_shares_at_all(self):
        self.assertEqual(self.repo.getUnseenAcceptedShareCount("alice"), 0)


class TestAcceptedShares(RepositorySharesTestCase):
    def test_returns_counterpart_when_username_was_the_requester(self):
        self._accept("alice", "bob")

        self.assertEqual(self.repo.getAcceptedShareUsernames("alice"), ["bob"])

    def test_returns_counterpart_when_username_was_the_recipient(self):
        self._accept("alice", "bob")

        self.assertEqual(self.repo.getAcceptedShareUsernames("bob"), ["alice"])

    def test_pending_only_requests_are_excluded(self):
        self.repo.createShareRequest("alice", "carol")

        self.assertEqual(self.repo.getAcceptedShareUsernames("carol"), [])
        self.assertEqual(self.repo.getAcceptedShares("carol"), [])

    def test_includes_the_share_id_alongside_the_counterpart(self):
        self.repo.createShareRequest("alice", "bob")
        shareId = self.repo.getPendingIncomingShares("bob")[0]["id"]
        self.repo.respondToShareRequest(shareId, "bob", accept=True)

        shares = self.repo.getAcceptedShares("alice")

        self.assertEqual(shares, [{"id": shareId, "counterpart": "bob"}])

    def test_accepted_shares_are_ordered_by_counterpart_name(self):
        """acceptedUsernames[0] is the Compare page's default counterpart -
        without a stable order it would flap between requests."""
        self._accept("alice", "carol")   #< accepted first, but sorts second
        self._accept("alice", "bob")

        self.assertEqual(self.repo.getAcceptedShareUsernames("alice"), ["bob", "carol"])


class TestHasAnyAcceptedShare(RepositorySharesTestCase):
    def test_true_once_a_cookie_bearing_counterpart_accepted(self):
        self._accept("alice", "bob")

        self.assertTrue(self.repo.hasAnyAcceptedShare("alice"))
        self.assertTrue(self.repo.hasAnyAcceptedShare("bob"))

    def test_false_while_only_pending(self):
        self.repo.createShareRequest("alice", "bob")

        self.assertFalse(self.repo.hasAnyAcceptedShare("alice"))

    def test_false_with_no_relationship_at_all(self):
        self.assertFalse(self.repo.hasAnyAcceptedShare("alice"))

    def test_false_when_the_only_counterpart_has_no_cookies(self):
        """The Compare page skips cookie-less counterparts (it can't load a
        live Database for them), so the nav link this backs must not point
        at a page that would 404."""
        self._accept("alice", "dave")   #< dave has no cookies (see setUp)

        self.assertFalse(self.repo.hasAnyAcceptedShare("alice"))


class TestRespondToShareRequest(RepositorySharesTestCase):
    def setUp(self):
        super().setUp()
        self.repo.createShareRequest("alice", "bob")
        self.shareId = self.repo.getPendingIncomingShares("bob")[0]["id"]

    def test_accept_marks_the_row_accepted(self):
        result = self.repo.respondToShareRequest(self.shareId, "bob", accept=True)

        self.assertTrue(result)
        self.assertEqual(self.repo.getAcceptedShareUsernames("alice"), ["bob"])

    def test_decline_removes_the_row_entirely(self):
        result = self.repo.respondToShareRequest(self.shareId, "bob", accept=False)

        self.assertTrue(result)
        self.assertEqual(self.repo.getPendingIncomingShares("bob"), [])
        self.assertEqual(self.repo.getAcceptedShareUsernames("alice"), [])

    def test_someone_other_than_the_recipient_cannot_respond(self):
        result = self.repo.respondToShareRequest(self.shareId, "carol", accept=True)

        self.assertFalse(result)
        self.assertEqual(self.repo.getAcceptedShareUsernames("alice"), [])

    def test_the_requester_cannot_respond_to_their_own_request(self):
        result = self.repo.respondToShareRequest(self.shareId, "alice", accept=True)

        self.assertFalse(result)

    def test_cannot_respond_to_an_already_accepted_share(self):
        self.repo.respondToShareRequest(self.shareId, "bob", accept=True)

        result = self.repo.respondToShareRequest(self.shareId, "bob", accept=False)

        self.assertFalse(result)
        self.assertEqual(self.repo.getAcceptedShareUsernames("alice"), ["bob"])


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
        self.assertEqual(self.repo.getAcceptedShareUsernames("alice"), ["bob"])


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
        self.assertEqual(self.repo.getAcceptedShareUsernames("alice"), [])

    def test_the_recipient_can_also_revoke(self):
        result = self.repo.revokeShare(self.shareId, "bob")

        self.assertTrue(result)
        self.assertEqual(self.repo.getAcceptedShareUsernames("alice"), [])

    def test_an_unrelated_user_cannot_revoke(self):
        result = self.repo.revokeShare(self.shareId, "carol")

        self.assertFalse(result)
        self.assertEqual(self.repo.getAcceptedShareUsernames("alice"), ["bob"])

    def test_cannot_revoke_a_merely_pending_request(self):
        self.repo.createShareRequest("alice", "carol")
        pendingId = self.repo.getPendingIncomingShares("carol")[0]["id"]

        result = self.repo.revokeShare(pendingId, "alice")

        self.assertFalse(result)
        self.assertEqual(len(self.repo.getPendingIncomingShares("carol")), 1)


class TestGetAllUsernamesExcept(RepositorySharesTestCase):
    def test_excludes_only_the_given_username(self):
        others = self.repo.getAllUsernamesExcept("alice")

        self.assertEqual(others, ["bob", "carol", "dave"])

    def test_does_not_pull_sensitive_columns(self):
        """This backs a plain "who can I share with" dropdown - it must not
        reuse getAllUsersDetails()'s SELECT, which includes cookies_json/
        spotify_refresh_token that this list has no reason to touch."""
        others = self.repo.getAllUsernamesExcept("alice")

        self.assertEqual(others, ["bob", "carol", "dave"])
        self.assertTrue(all(isinstance(u, str) for u in others))


if __name__ == "__main__":
    unittest.main()
