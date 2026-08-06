"""A failed Spotify login must degrade cleanly, not crash listener startup.

self.sp.startRecentlyPlayedListener() builds a PlayerStatus/WebsocketStreamer
around self.sp.user_auth - spotapi's WebsocketStreamer.__init__ does
`login.logged_in`, which raises AttributeError once user_auth is left as the
plain `False` SpotipyFree's own login() leaves it on failure (cookies
invalid/expired/undecryptable). That crash used to propagate out of
Listener.__init__, through ConnectionManager.startListener(), into
get_user_db()'s except-and-rollback - silently uncaching the user, so admin
showed a generic "Inactive" instead of anything actionable, and the crash
repeated every 5-minute recheck. A failed login must instead be skipped
cleanly and reported the same way contaminationDetected already is (see
tests/test_listener_contamination.py): DEAD health with a reason.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from conftest import DatabaseTestCase
from Database.Listeners.spotifyListener import Listener


def _makeListener(loggedIn, expectedEmail="expected@example.com"):
    """A real Listener constructed against a mocked Spotify client whose
    login succeeded (loggedIn=True) or failed (loggedIn=False). On failure,
    startRecentlyPlayedListener is wired to raise the exact error the real
    spotapi/SpotipyFree stack raises when called with an unauthenticated
    client, so a regression (the guard being removed) fails loudly here
    instead of the flag simply not being set."""
    sp = MagicMock()
    sp.isLoggedIn.return_value = loggedIn
    sp.current_user.return_value = {"id": "spotify-user-1", "email": expectedEmail}
    sp.current_user_recently_played.return_value = []
    if not loggedIn:
        sp.startRecentlyPlayedListener.side_effect = AttributeError(
            "'bool' object has no attribute 'logged_in'"
        )
    with patch("Database.Listeners.spotifyListener.Spotify", return_value=sp):
        listener = Listener(cookiesFile="unused.json", email=expectedEmail)
    return listener, sp


class TestLoginFailureDetection(unittest.TestCase):
    def test_successful_login_is_not_flagged(self):
        listener, sp = _makeListener(loggedIn=True)
        self.assertFalse(listener.loginFailed)
        sp.startRecentlyPlayedListener.assert_called_once()

    def test_failed_login_is_flagged_without_starting_the_player_status_listener(self):
        listener, sp = _makeListener(loggedIn=False)
        self.assertTrue(listener.loginFailed)
        sp.startRecentlyPlayedListener.assert_not_called()

    def test_failed_login_does_not_raise(self):
        """Regression test for the original crash: constructing a Listener
        against cookies that fail to authenticate must not raise. sp's
        startRecentlyPlayedListener is wired to raise the real
        AttributeError - this only passes because the fix never calls it."""
        try:
            _makeListener(loggedIn=False)
        except Exception as e:
            self.fail(f"Listener construction raised on a failed login: {e!r}")


class TestAFailedConstructionRetiresItsSession(unittest.TestCase):
    """A Listener whose __init__ raises has to release the TLS session it built.

    Every login mints a fresh spotapi TLSClient, and Spotify.close() is
    reachable from exactly one place: Listener.stop(). An exception out of
    __init__ means startListener never assigns `self.listener`, so stop() is
    never reachable for that object and spotapi's atexit registration keeps the
    curl session open until the process exits.

    Failing construction used to be rare - the init-packet read simply parked
    forever. c8b688b gave it a deadline and a899886 a socket close, which is
    what turned a wedge into a raise, and retrying is what _checkLoginLoop's
    restart pass and onStaleWithBackoff both do. So the leak is one session per
    failed reconnect, for the life of the process.

    Note this does NOT cover the streamer's own dealer socket: Spotify.close()
    releases login.client, and lastPlayedManager is still None when the
    PlayerStatus constructor raised. That half belongs to Database/patches.py -
    _closeSocketOfFailedConstruction, reached from both patched frames of the
    construction (the WebsocketStreamer base __init__ and PlayerStatus's own,
    whose second register_device() call runs after the base one has already
    returned successfully)."""

    def _construct(self, sp):
        with patch("Database.Listeners.spotifyListener.Spotify", return_value=sp):
            return Listener(cookiesFile="unused.json", email="expected@example.com")

    def _loggedInClient(self):
        sp = MagicMock()
        sp.isLoggedIn.return_value = True
        sp.current_user.return_value = {"id": "spotify-user-1", "email": "expected@example.com"}
        sp.current_user_recently_played.return_value = []
        return sp

    def test_a_handshake_that_raises_closes_the_session(self):
        """The path c8b688b created: the init packet never arrives and the
        bounded read raises out of the PlayerStatus constructor."""
        sp = self._loggedInClient()
        sp.startRecentlyPlayedListener.side_effect = TimeoutError("no init packet")

        with self.assertRaises(TimeoutError):
            self._construct(sp)

        sp.close.assert_called_once_with()

    def test_a_later_failure_in_the_same_constructor_closes_it_too(self):
        """The guard covers the constructor, not just the handshake.

        What this pins is a CONTRACT - any raise after the session exists
        retires it - not a reachable bug. Worth saying plainly, because two
        earlier attempts to name a concrete raise here were both wrong and the
        record should stop at the truth:

        - 3f44703 called current_user_recently_played "a second network call".
          It is not. Database/Spotify/client.py implements it as
          `return list(self.recentlyPlayed)` and its own docstring says "NOT a
          Spotify call".
        - The replacement claim - that the copy races _addToRecentlyPlayed on
          the manager thread and `list()` raises "deque mutated during
          iteration" - is also wrong. list(deque) is a single C-level
          iteration: it runs no bytecode, so the interpreter never switches
          threads inside it. Measured on this repo's interpreter (CPython
          3.13.14, GIL on): 9.5M copies against 33M concurrent appends, zero
          RuntimeError. The producer could not have appended that early anyway
          - _applyStateToTracking fires the callback only from the SECOND
          observed connect state onward.

        So no statement between the session coming up and the end of __init__
        is known to be able to raise. The guard is breadth, not a fix: it costs
        nothing, and it means the next line added to this constructor is
        covered without anyone having to notice that it needs to be. Driven
        through a MagicMock for exactly that reason."""
        sp = self._loggedInClient()
        sp.current_user_recently_played.side_effect = RuntimeError("Max retries exceeded")

        with self.assertRaises(RuntimeError):
            self._construct(sp)

        sp.close.assert_called_once_with()

    def test_a_construction_that_succeeds_keeps_its_session(self):
        """The session IS the listener; closing it here would end every
        listener at the moment it was built."""
        sp = self._loggedInClient()

        listener = self._construct(sp)

        sp.close.assert_not_called()
        self.assertIs(listener.sp, sp)

    def test_a_login_failure_is_not_a_construction_failure(self):
        """Invalid cookies return a real Listener (see above), so stop() still
        owns its close - retiring it here would shut a session the caller
        holds, and startListener reports DEAD from the flag, not from a raise."""
        sp = MagicMock()
        sp.isLoggedIn.return_value = False
        sp.current_user.return_value = {"id": "spotify-user-1", "email": "expected@example.com"}
        sp.current_user_recently_played.return_value = []

        listener = self._construct(sp)

        self.assertTrue(listener.loginFailed)
        sp.close.assert_not_called()

    def test_a_close_that_fails_does_not_replace_the_real_failure(self):
        """classifyListenerError decides retry-vs-give-up from the construction
        error; a teardown OSError in its place would be diagnosed as something
        else. Listener.stop() guards its own close the same way."""
        sp = self._loggedInClient()
        sp.startRecentlyPlayedListener.side_effect = TimeoutError("no init packet")
        sp.close.side_effect = OSError("session already gone")

        with self.assertRaises(TimeoutError):
            self._construct(sp)

        sp.close.assert_called_once_with()


class TestLoginFailedListenerHealth(DatabaseTestCase):
    def _startWithListener(self, listener):
        db = self._makeDb({}, [])
        with patch("Database.database.Listener", return_value=listener):
            db.startListener(email="expected@example.com")
        return db

    def test_login_failed_listener_marks_health_dead(self):
        listener = MagicMock()
        listener.contaminationDetected = False
        listener.loginFailed = True

        db = self._startWithListener(listener)

        health = db.getListenerHealth()
        self.assertEqual(health["status"], "DEAD")
        self.assertIn("login failed", health["last_error"])
        listener.startListener_thread.assert_not_called()

    def test_logged_in_listener_still_marks_healthy_and_starts(self):
        listener = MagicMock()
        listener.contaminationDetected = False
        listener.loginFailed = False

        db = self._startWithListener(listener)

        self.assertEqual(db.getListenerHealth()["status"], "HEALTHY")
        listener.startListener_thread.assert_called_once()


if __name__ == "__main__":
    unittest.main()
