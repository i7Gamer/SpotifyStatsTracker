# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The per-user listener session ledger behind /admin's Worker Health card:
how many listener sessions this process has built for a user, and when/why
the last rebuild happened. One websocket streamer exists per build (see the
atexit notes in Database/patches.py), so this ledger doubles as the streamer
count - a runaway value here is the rebuild-churn signal the 2026-08-04
investigation had to reconstruct from app.log by hand."""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database


class SessionTrackingTestBase(unittest.TestCase):
    def _makeTestDb(self):
        with patch('Database.database.Repository'), \
             patch('Database.database.AutoImporter'), \
             patch('Database.database.Path.exists', return_value=False):
            db = Database(user="TestUser", email="test@example.com")
        self.addCleanup(db.stop)
        return db

    def _startListener(self, db, rebuildReason=None):
        """Run startListener with the Spotify login mocked out."""
        with patch.object(db, '_withCookiesFile') as mock_cookies, \
             patch('Database.database.Listener'):
            mock_listener = MagicMock()
            mock_listener.contaminationDetected = False  #< a bare MagicMock's auto-attribute is truthy = contaminated
            mock_listener.loginFailed = False  #< same trap - see above
            mock_cookies.return_value = mock_listener
            if rebuildReason is None:
                db.startListener(email="test@example.com")
            else:
                db.startListener(email="test@example.com", rebuildReason=rebuildReason)
        return mock_listener


class TestListenerSessionLedger(SessionTrackingTestBase):
    def test_the_first_build_counts_without_a_rebuild_entry(self):
        db = self._makeTestDb()

        self._startListener(db)

        self.assertEqual(db.listener_session_builds, 1)
        self.assertIsNone(db.listener_last_rebuild_time)
        self.assertIsNone(db.listener_last_rebuild_reason)

    def test_a_rebuild_records_its_time_and_reason(self):
        db = self._makeTestDb()

        self._startListener(db)
        self._startListener(db, rebuildReason="quiet feed hard ceiling")

        self.assertEqual(db.listener_session_builds, 2)
        self.assertIsNotNone(db.listener_last_rebuild_time)
        self.assertEqual(db.listener_last_rebuild_reason, "quiet feed hard ceiling")

    def test_a_reasonless_rebuild_does_not_keep_the_previous_reason(self):
        """A cookies-update or health-check restart carries no stale reason; a
        leftover one from an earlier rebuild would misattribute it."""
        db = self._makeTestDb()

        self._startListener(db)
        self._startListener(db, rebuildReason="auth error")
        self._startListener(db)

        self.assertEqual(db.listener_session_builds, 3)
        self.assertIsNone(db.listener_last_rebuild_reason)

    def test_get_listener_health_carries_the_ledger(self):
        db = self._makeTestDb()

        self._startListener(db)
        self._startListener(db, rebuildReason="unrecorded playback on a quiet feed")
        health = db.getListenerHealth()

        self.assertEqual(health["session_builds"], 2)
        self.assertIsNotNone(health["last_rebuild_time"])
        self.assertEqual(health["last_rebuild_reason"], "unrecorded playback on a quiet feed")


class TestOnStaleReasonPassThrough(SessionTrackingTestBase):
    """_makeOnStaleCallback is the seam between the listener's diagnosis and
    the ledger: whatever reason the listener names must reach startListener."""

    def test_the_reason_reaches_startListener(self):
        db = self._makeTestDb()
        onStale = db._makeOnStaleCallback()

        with patch.object(db, 'startListener', return_value=True) as mock_start:
            onStale(reason="quiet feed hard ceiling")

        mock_start.assert_called_once_with(
            email="test@example.com", rebuildReason="quiet feed hard ceiling")

    def test_a_reasonless_call_still_rebuilds(self):
        """Direct callers (tests, older wiring) may invoke onStale() bare."""
        db = self._makeTestDb()
        onStale = db._makeOnStaleCallback()

        with patch.object(db, 'startListener', return_value=True) as mock_start:
            onStale()

        mock_start.assert_called_once_with(email="test@example.com", rebuildReason=None)


if __name__ == "__main__":
    unittest.main()
