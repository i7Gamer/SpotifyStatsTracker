"""Wiring for the achievement-milestones feature: the topbar "new milestone"
badge (layout.html + app.py's _injectMilestoneStatus) and the background
detection pass folded into _ensureAllUsersLogin.

The detection LOGIC itself is covered by test_milestones.py; the card the badge
points at lives on the dashboard, so its rendering (and the badge-clearing that
comes with viewing it) is covered by test_dashboard_cards.py's
DashboardMilestonesCardTestCase. This file covers that the feature surfaces to
the user and fires from the right place.
"""
import os
import sys
import json
import datetime
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase


class _BadgeTestCase(AppTestCase):
    def _makeDb(self):
        db = MagicMock()
        db.repo = self.dash.repo
        db.tz = datetime.timezone.utc   #< /profile formats share-link dates with this
        db.getUserSpotifyCredentials.return_value = {}
        db.getUserLastfmApiKey.return_value = None
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


class TestMilestoneTopbarBadge(_BadgeTestCase):
    def test_hidden_when_no_unseen_milestones(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/import")

        self.assertNotIn(b"milestone-badge", resp.data)

    def test_shows_count_when_unseen(self):
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.recordMilestone("alice", "plays", 1000, None, 1.0, seen=False)
        self.dash.repo.recordMilestone("alice", "streak", 7, None, 2.0, seen=False)

        resp = client.get("/import")

        self.assertIn(b'class="milestone-badge"', resp.data)
        self.assertIn(b"2 new milestones", resp.data)
        #< straight to the card, not to the settings page it used to live on
        self.assertIn(b'href="/#milestones"', resp.data)

    def test_seen_milestones_do_not_show(self):
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.recordMilestone("alice", "plays", 1000, None, 1.0, seen=True)

        resp = client.get("/import")

        self.assertNotIn(b"milestone-badge", resp.data)

    def test_badge_does_not_leak_to_another_user(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.recordMilestone("bob", "plays", 1000, None, 1.0, seen=False)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/import")

        self.assertNotIn(b"milestone-badge", resp.data)

    def test_badge_hidden_when_feature_disabled(self):
        # Admin kill switch hides the badge without deleting the rows (same
        # contract as data-sharing's toggle zeroing the share badges).
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.recordMilestone("alice", "plays", 1000, None, 1.0, seen=False)
        self.dash.repo.setMilestonesEnabled(False)

        resp = client.get("/import")

        self.assertNotIn(b"milestone-badge", resp.data)


class TestProfileNoLongerCarriesMilestones(_BadgeTestCase):
    """The list moved to the dashboard - /profile must not render it (or pay
    for its query), and must not silently clear the badge on the way past."""

    def test_profile_does_not_render_milestones(self):
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.recordMilestone("alice", "plays", 1000, None, 1609459200.0, seen=True)

        resp = client.get("/profile")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"1,000 lifetime plays", resp.data)
        self.assertNotIn(b"milestone-list", resp.data)

    def test_profile_does_not_clear_the_badge(self):
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.recordMilestone("alice", "plays", 1000, None, 1609459200.0, seen=False)

        client.get("/profile")

        self.assertEqual(self.dash.repo.getUnseenMilestoneCount("alice"), 1)


class TestDetectionWiring(AppTestCase):
    def test_ensure_all_users_login_runs_detection(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getListenerHealth.return_value = {"status": "OK"}
        db.listener.thread.is_alive.return_value = True
        with patch.object(dash.repo, "getAllUsersWithCookies", return_value=[("alice", "alice@example.com")]), \
             patch.object(dash, "get_user_db", return_value=db), \
             patch("app.detectMilestones") as mockDetect:
            dash._ensureAllUsersLogin()

        mockDetect.assert_called_once()
        self.assertEqual(mockDetect.call_args.args[0], db)          #< db
        self.assertEqual(mockDetect.call_args.args[2], "alice")     #< username

    def test_detection_failure_does_not_stall_the_loop(self):
        dash = self._makeApp()
        with patch("app.detectMilestones", side_effect=RuntimeError("boom")):
            dash._detectMilestonesSafely(MagicMock(), "alice")   #< must not raise

    def test_detection_skipped_when_feature_disabled(self):
        dash = self._makeApp()
        dash.repo.setMilestonesEnabled(False)
        db = MagicMock()
        db.getListenerHealth.return_value = {"status": "OK"}
        db.listener.thread.is_alive.return_value = True
        with patch.object(dash.repo, "getAllUsersWithCookies", return_value=[("alice", "alice@example.com")]), \
             patch.object(dash, "get_user_db", return_value=db), \
             patch("app.detectMilestones") as mockDetect:
            dash._ensureAllUsersLogin()

        mockDetect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
