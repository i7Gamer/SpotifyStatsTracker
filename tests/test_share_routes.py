"""Data-sharing management: the request_share action on /profile, and the
accept/decline/cancel/revoke actions on POST /profile/shares/<id>.
"""
import unittest
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp, RATE_LIMIT_MAX_ATTEMPTS

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class ShareRoutesTestCase(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _makeDb(self):
        db = MagicMock()
        db.repo = self.dash.repo
        db.getUserSpotifyCredentials.return_value = {}
        return db

    def _loginAs(self, username, email):
        self.dash.repo.upsertUser(username, email)
        patcher_login = patch.object(self.dash, 'is_user_logged_in', return_value=True)
        patcher_email = patch.object(self.dash, 'get_username_for_email', return_value=username)
        patcher_db = patch.object(self.dash, 'get_user_db', return_value=self._makeDb())
        patcher_login.start()
        patcher_email.start()
        patcher_db.start()
        self.addCleanup(patcher_login.stop)
        self.addCleanup(patcher_email.stop)
        self.addCleanup(patcher_db.stop)

        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess['email'] = email
            sess['username'] = username
        return client

    def setUp(self):
        self.dash = self._makeApp()


class TestRequestShareAction(ShareRoutesTestCase):
    def test_requesting_a_share_creates_a_pending_request(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={"action": "request_share", "target_username": "bob"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Share request sent", resp.data)
        outgoing = self.dash.repo.getPendingOutgoingShares("alice")
        self.assertEqual([r["recipient_username"] for r in outgoing], ["bob"])

    def test_reverse_pending_request_reports_as_immediately_active(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.createShareRequest("bob", "alice")
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={"action": "request_share", "target_username": "bob"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"now sharing", resp.data)
        self.assertIn("bob", self.dash.repo.getAcceptedShareUsernames("alice"))

    def test_cannot_request_a_share_with_yourself(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={"action": "request_share", "target_username": "alice"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"yourself", resp.data)
        self.assertEqual(self.dash.repo.getPendingOutgoingShares("alice"), [])

    def test_cannot_request_a_share_with_a_nonexistent_user(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={"action": "request_share", "target_username": "ghost"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"does not exist", resp.data)
        self.assertEqual(self.dash.repo.getPendingOutgoingShares("alice"), [])

    def test_blank_target_username_is_rejected(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={"action": "request_share", "target_username": ""})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"error", resp.data.lower())
        self.assertEqual(self.dash.repo.getPendingOutgoingShares("alice"), [])

    def test_re_requesting_a_pending_share_says_already_pending(self):
        """createShareRequest treats a repeat as a no-op - the message must
        say so, not claim a new request was just sent."""
        self.dash.repo.upsertUser("bob", "bob@example.com")
        client = self._loginAs("alice", "alice@example.com")
        client.post("/profile", data={"action": "request_share", "target_username": "bob"})

        resp = client.post("/profile", data={"action": "request_share", "target_username": "bob"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"already pending", resp.data)
        self.assertNotIn(b"Share request sent", resp.data)

    def test_re_requesting_an_accepted_share_says_already_sharing(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.createShareRequest("alice", "bob")
        shareId = self.dash.repo.getPendingIncomingShares("bob")[0]["id"]
        self.dash.repo.respondToShareRequest(shareId, "bob", accept=True)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={"action": "request_share", "target_username": "bob"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"already share data with bob", resp.data)
        self.assertNotIn(b"now sharing data with each other", resp.data)

    def test_request_share_is_rate_limited(self):
        """Declines delete the share row, so without a throttle a rejected
        requester could re-request (or fan out to every user) indefinitely -
        request_share shares the same per-IP limiter as /login and /register."""
        self.dash.repo.upsertUser("bob", "bob@example.com")
        client = self._loginAs("alice", "alice@example.com")

        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            resp = client.post("/profile", data={"action": "request_share", "target_username": "bob"})
            self.assertEqual(resp.status_code, 200)

        resp = client.post("/profile", data={"action": "request_share", "target_username": "bob"})

        self.assertEqual(resp.status_code, 429)
        self.assertIn(b"Too many attempts", resp.data)

    def test_rate_limit_does_not_affect_other_profile_actions(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        client = self._loginAs("alice", "alice@example.com")
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS + 1):
            client.post("/profile", data={"action": "request_share", "target_username": "bob"})

        resp = client.post("/profile", data={"action": "save_preferences",
                                             "default_dashboard_window": "week", "timezone": ""})

        self.assertEqual(resp.status_code, 200)


class TestProfilePageShareListings(ShareRoutesTestCase):
    def test_picker_excludes_users_already_in_a_share_relationship(self):
        """Re-requesting an existing counterpart is always a no-op, so the
        dropdown must only offer users with no pending/accepted relationship."""
        for u in ("bob", "carol", "dave", "erin"):
            self.dash.repo.upsertUser(u, f"{u}@example.com")
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareRequest("alice", "bob")
        bobShareId = self.dash.repo.getPendingIncomingShares("bob")[0]["id"]
        self.dash.repo.respondToShareRequest(bobShareId, "bob", accept=True)   #< accepted
        self.dash.repo.createShareRequest("alice", "carol")                    #< pending outgoing
        self.dash.repo.createShareRequest("dave", "alice")                     #< pending incoming

        client = self._loginAs("alice", "alice@example.com")
        resp = client.get("/profile")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'<option value="erin">', resp.data)   #< unrelated user still offered
        self.assertNotIn(b'<option value="bob">', resp.data)
        self.assertNotIn(b'<option value="carol">', resp.data)
        self.assertNotIn(b'<option value="dave">', resp.data)

    def test_lists_pending_incoming_outgoing_and_accepted_and_candidates(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("carol", "carol@example.com")
        self.dash.repo.upsertUser("dave", "dave@example.com")
        self.dash.repo.createShareRequest("bob", "alice")       #< incoming to alice
        self.dash.repo.createShareRequest("alice", "carol")     #< outgoing from alice
        self.dash.repo.createShareRequest("alice", "dave")
        daveShareId = self.dash.repo.getPendingOutgoingShares("alice")[-1]["id"]
        self.dash.repo.respondToShareRequest(daveShareId, "dave", accept=True)   #< accepted

        client = self._loginAs("alice", "alice@example.com")
        resp = client.get("/profile")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"bob", resp.data)     #< pending incoming
        self.assertIn(b"carol", resp.data)   #< pending outgoing
        self.assertIn(b"dave", resp.data)    #< accepted


class TestShareActionRoute(ShareRoutesTestCase):
    def _pendingShareId(self, requester, recipient):
        self.dash.repo.upsertUser(requester, f"{requester}@example.com")
        self.dash.repo.upsertUser(recipient, f"{recipient}@example.com")
        self.dash.repo.createShareRequest(requester, recipient)
        return self.dash.repo.getPendingIncomingShares(recipient)[0]["id"]

    def test_recipient_can_accept(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareId("alice", "bob")
        client = self._loginAs("bob", "bob@example.com")

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "accept"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("bob", self.dash.repo.getAcceptedShareUsernames("alice"))

    def test_recipient_can_decline(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareId("alice", "bob")
        client = self._loginAs("bob", "bob@example.com")

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "decline"})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.dash.repo.getPendingIncomingShares("bob"), [])

    def test_requester_can_cancel(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareId("alice", "bob")
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "cancel"})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.dash.repo.getPendingOutgoingShares("alice"), [])

    def test_either_party_can_revoke_an_accepted_share(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareId("alice", "bob")
        self.dash.repo.respondToShareRequest(shareId, "bob", accept=True)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "revoke"})

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("bob", self.dash.repo.getAcceptedShareUsernames("alice"))

    def test_an_unrelated_user_cannot_act_on_someone_elses_share(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("carol", "carol@example.com")
        shareId = self._pendingShareId("alice", "bob")
        client = self._loginAs("carol", "carol@example.com")

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "accept"})

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("bob", self.dash.repo.getAcceptedShareUsernames("alice"))
        self.assertEqual(len(self.dash.repo.getPendingIncomingShares("bob")), 1)

    def test_unknown_action_value_is_rejected(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareId("alice", "bob")
        client = self._loginAs("bob", "bob@example.com")

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "hack"})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self.dash.repo.getPendingIncomingShares("bob")), 1)

    def test_nonexistent_share_id_does_not_500(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile/shares/999999", data={"action": "accept"})

        self.assertEqual(resp.status_code, 302)

    def test_anonymous_request_is_redirected_to_login(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareId("alice", "bob")
        client = self.dash.app.test_client()   #< no session at all

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "accept"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        self.assertEqual(len(self.dash.repo.getPendingIncomingShares("bob")), 1)   #< nothing acted on


if __name__ == "__main__":
    unittest.main()
