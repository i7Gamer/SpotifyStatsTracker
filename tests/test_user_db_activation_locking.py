"""Item 2 (2026-07-24 review): get_user_db() must not hold the global _db_lock
across a user's Spotify login (startListener). One user's slow first-time login
used to stall every authenticated request in the process, because
get_current_user_or_redirect() takes the same lock on every request.
"""
import sys
import os
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import makeApp


class TestActivationDoesNotBlockOtherUsers(unittest.TestCase):
    def test_slow_activation_of_one_user_does_not_block_another(self):
        dash = makeApp()

        # User A is already constructed AND activated - the fast path should
        # return instantly regardless of what any other user is doing.
        aDb = MagicMock()
        dash.user_databases["alice"] = aDb
        dash._activatedUsers.add("alice")

        # User B's activation blocks inside startListener, standing in for a
        # slow Spotify login.
        started = threading.Event()
        release = threading.Event()
        bDb = MagicMock()

        def blockingStartListener(email=None):
            started.set()
            release.wait(timeout=5)

        bDb.startListener.side_effect = blockingStartListener
        dash.user_databases["bob"] = bDb   # present but not yet activated

        bThread = threading.Thread(target=lambda: dash.get_user_db("bob", "bob@example.com"))
        bThread.start()
        self.assertTrue(started.wait(timeout=2), "bob's activation should have started")

        try:
            # While bob is blocked in startListener, alice's fast path must
            # return at once - it did NOT with the old global-lock-held-across-
            # startListener code.
            result = []
            aThread = threading.Thread(
                target=lambda: result.append(dash.get_user_db("alice", "alice@example.com")))
            aThread.start()
            aThread.join(timeout=2)

            self.assertFalse(aThread.is_alive(),
                             "get_user_db(alice) blocked on bob's in-progress activation")
            self.assertIs(result[0], aDb)
        finally:
            release.set()
            bThread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
