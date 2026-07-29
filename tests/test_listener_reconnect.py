"""Tests for the Listener's self-healing reconnect.

spotapi's own websocket-fed "recently played" feed can silently die (its
reconnect() call in LastPlayedManger.updateLoop targets a method that doesn't
exist on PlayerStatus, so recovery from a dropped websocket is broken upstream
- see Database/Listeners/spotifyListener.py). Once that happens,
current_user_recently_played() keeps returning the same frozen list forever:
no exception, no new items, nothing recorded, indefinitely. This tests the
staleness timeout that detects that frozen state and asks the caller to
rebuild the session instead of staying wedged forever.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.Listeners.spotifyListener import (
    Listener,
    LISTENER_STALE_TIMEOUT_SECONDS,
    LISTENER_STALE_HARD_TIMEOUT_SECONDS,
    RATE_LIMIT_ERROR_BACKOFF_SECONDS,
    USER_VALIDATION_CACHE_SECONDS,
    USER_VALIDATION_ERROR_COOLDOWN_SECONDS,
    LISTENER_POLL_INTERVAL_SECONDS,
    LISTENER_POLL_INTERVAL_JITTER_SECONDS,
    _pollIntervalWithJitter,
    _is_auth_error,
    _is_rate_limit_error,
    classifyListenerError,
)

# Comfortably past USER_VALIDATION_CACHE_SECONDS so _validateCurrentUser's
# freshness-cache branch is deterministically bypassed, regardless of how
# large time.monotonic() already is on the host running the test (e.g. a
# freshly booted CI runner has a much smaller monotonic clock than a
# long-uptime dev machine).
_MONOTONIC_NOW = USER_VALIDATION_CACHE_SECONDS * 10


def _bareListener(recentlyPlayed=None):
    listener = Listener.__new__(Listener)
    listener.run = True
    listener.sp = MagicMock()
    listener.recentlyPlayed_Z1 = recentlyPlayed if recentlyPlayed is not None else []
    listener.sp.current_user_recently_played.return_value = listener.recentlyPlayed_Z1
    # No connect state captured (what a listener whose websocket tick never ran
    # or keeps erroring looks like) - the stale check reads this to tell a dead
    # session from an idle account, and "no evidence" means dead.
    listener.sp.lastPlayedManager.manager._state = None
    listener._lastPlayingUri = None      #< matches Listener.__init__
    listener._lastPlayingChangeTime = 0.0
    listener._lastChangeTime = 0.0
    listener._authenticated_user_id = None
    listener.email = None
    listener.user = None  #< matches Listener.__init__; self.logUser reads both
    listener._last_user_validation_time = None  #< matches Listener.__init__: never validated yet
    listener._last_user_validation_result = True
    listener._last_validation_error_time = None  #< matches Listener.__init__: no refusal recorded
    return listener


class TestCheckOnceNewItems(unittest.TestCase):
    def test_new_item_invokes_callback_and_resets_change_time(self):
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener.sp.current_user_recently_played.return_value = [{"played_at": 1}, {"played_at": 2}]
        callback = MagicMock()

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=500.0):
            stillRunning = listener._checkOnce(callback, onStale=None)

        self.assertTrue(stillRunning)
        callback.assert_called_once_with([{"played_at": 2}])
        self.assertEqual(listener.recentlyPlayed_Z1, [{"played_at": 1}, {"played_at": 2}])
        self.assertEqual(listener._lastChangeTime, 500.0)

    def test_unchanged_feed_within_timeout_does_not_trigger_onStale(self):
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0
        onStale = MagicMock()

        withinTimeout = 100.0 + LISTENER_STALE_TIMEOUT_SECONDS - 1
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=withinTimeout):
            stillRunning = listener._checkOnce(MagicMock(), onStale=onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()


class TestCheckOnceStaleness(unittest.TestCase):
    def test_frozen_feed_past_timeout_triggers_onStale_and_stops(self):
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0
        onStale = MagicMock()

        pastTimeout = 100.0 + LISTENER_STALE_TIMEOUT_SECONDS + 1
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=pastTimeout):
            stillRunning = listener._checkOnce(MagicMock(), onStale=onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_frozen_feed_past_timeout_without_onStale_keeps_running(self):
        """No onStale callback wired (e.g. a bare/manual Listener) - must not
        crash, and since there's no way to recover, it just keeps polling."""
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0

        pastTimeout = 100.0 + LISTENER_STALE_TIMEOUT_SECONDS + 1
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=pastTimeout):
            stillRunning = listener._checkOnce(MagicMock(), onStale=None)

        self.assertTrue(stillRunning)

    def test_onStale_exception_is_swallowed_and_still_stops(self):
        """A failed reconnect attempt (e.g. cookies genuinely expired) must not
        crash the polling thread silently with no trace - the exception is
        logged and the (now-dead) listener still stops."""
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0
        onStale = MagicMock(side_effect=RuntimeError("reconnect failed"))

        pastTimeout = 100.0 + LISTENER_STALE_TIMEOUT_SECONDS + 1
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=pastTimeout):
            stillRunning = listener._checkOnce(MagicMock(), onStale=onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()


def _playingState(trackUri, isPaused=False):
    return {"is_playing": True, "is_paused": isPaused, "track": {"uri": trackUri}}


class TestStaleFeedIdleDetection(unittest.TestCase):
    """A quiet recently-played feed used to mean "the session died", so a
    listener was rebuilt every 30 minutes whether or not anyone was listening -
    1,270 stale reconnects over 11 days, spread evenly across all 24 hours
    while actual plays vary 100x between night and day. The feed only changes
    when a track finishes, so silence is the normal state of an idle account.
    What separates the two is the connect state: it says whether anything is
    playing at all, and whether a track change happened that the feed then
    failed to record."""

    TRACK_A = "spotify:track:aaaaaaaaaaaaaaaaaaaaaa"
    TRACK_B = "spotify:track:bbbbbbbbbbbbbbbbbbbbbb"

    def _poll(self, listener, now, state, onStale):
        listener.sp.lastPlayedManager.manager._state = state
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=now):
            return listener._checkOnce(MagicMock(), onStale=onStale)

    def _listener(self):
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0
        return listener

    def _pastTimeout(self, extra=1):
        return 100.0 + LISTENER_STALE_TIMEOUT_SECONDS + extra

    def test_nothing_playing_is_idle_not_stale(self):
        listener, onStale = self._listener(), MagicMock()

        stillRunning = self._poll(listener, self._pastTimeout(), {"is_playing": False}, onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_paused_playback_is_idle_not_stale(self):
        listener, onStale = self._listener(), MagicMock()

        stillRunning = self._poll(listener, self._pastTimeout(),
                                  _playingState(self.TRACK_A, isPaused=True), onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_one_long_track_playing_throughout_is_not_stale(self):
        """The feed only gains an entry when a track ENDS, so a 40-minute mix
        legitimately produces nothing for the whole window."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, 200.0, _playingState(self.TRACK_A), onStale)
        stillRunning = self._poll(listener, self._pastTimeout(), _playingState(self.TRACK_A), onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_a_track_change_the_feed_never_recorded_is_stale(self):
        """The genuine failure this watchdog exists for: playback moved on, so
        the finished track should have reached the feed - and didn't."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, 200.0, _playingState(self.TRACK_A), onStale)
        self._poll(listener, 300.0, _playingState(self.TRACK_B), onStale)
        stillRunning = self._poll(listener, self._pastTimeout(), _playingState(self.TRACK_B), onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_first_sighting_of_a_track_is_not_a_change(self):
        """A listener rebuilt mid-track sees that track for the first time -
        that is not evidence anything was missed, or every rebuild would
        immediately justify the next one."""
        listener, onStale = self._listener(), MagicMock()

        stillRunning = self._poll(listener, self._pastTimeout(), _playingState(self.TRACK_A), onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_non_track_playback_is_not_a_track_change(self):
        """Ads and podcast episodes never reach the recently-played feed, so
        moving between them proves nothing about the feed's health."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, 200.0, _playingState("spotify:episode:aaaaaaaaaaaaaaaaaaaaaa"), onStale)
        stillRunning = self._poll(listener, self._pastTimeout(),
                                  _playingState("spotify:ad:bbbbbbbbbbbbbbbbbbbbbb"), onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_track_change_already_reflected_in_the_feed_is_not_stale(self):
        """The change was observed BEFORE the feed's last update, i.e. the feed
        did record it - only a change the feed never caught up with counts."""
        listener, onStale = self._listener(), MagicMock()
        listener._lastPlayingUri = self.TRACK_A
        listener._lastPlayingChangeTime = 50.0  #< before _lastChangeTime (100.0)

        stillRunning = self._poll(listener, self._pastTimeout(), _playingState(self.TRACK_A), onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_missing_connect_state_still_rebuilds(self):
        """No connect state at all means the websocket tick that feeds it isn't
        running either - no evidence of life, so keep the old behaviour."""
        listener, onStale = self._listener(), MagicMock()

        stillRunning = self._poll(listener, self._pastTimeout(), None, onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_hard_ceiling_rebuilds_even_when_idle(self):
        """The one failure the idle check cannot see is a connect state that
        keeps answering while its own tick is wedged, so a quiet feed still
        gets recycled eventually - just hours apart instead of every 30
        minutes."""
        listener, onStale = self._listener(), MagicMock()

        stillRunning = self._poll(listener, 100.0 + LISTENER_STALE_HARD_TIMEOUT_SECONDS + 1,
                                  {"is_playing": False}, onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_idle_listener_keeps_polling_below_the_ceiling(self):
        """Sanity bound on the ceiling: an idle account is left alone for hours,
        not minutes."""
        listener, onStale = self._listener(), MagicMock()

        stillRunning = self._poll(listener, 100.0 + LISTENER_STALE_HARD_TIMEOUT_SECONDS - 60,
                                  {"is_playing": False}, onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()
        self.assertGreater(LISTENER_STALE_HARD_TIMEOUT_SECONDS, LISTENER_STALE_TIMEOUT_SECONDS)

    def test_an_active_feed_never_reaches_the_stale_check(self):
        """Regression guard: the playback observation must not disturb the
        normal path where the feed IS changing."""
        listener, onStale = self._listener(), MagicMock()
        listener.sp.current_user_recently_played.return_value = [{"played_at": 1}, {"played_at": 2}]
        callback = MagicMock()

        listener.sp.lastPlayedManager.manager._state = _playingState(self.TRACK_B)
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=self._pastTimeout()):
            stillRunning = listener._checkOnce(callback, onStale=onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()
        callback.assert_called_once()
        self.assertEqual(listener._lastChangeTime, self._pastTimeout())


class TestAuthErrorDetection(unittest.TestCase):
    def test_loginerror_is_detected_as_auth_error(self):
        exc = Exception("spotapi.exceptions.errors.LoginError: Could not GET ...")
        self.assertTrue(_is_auth_error(exc))

    def test_401_status_is_detected_as_auth_error(self):
        exc = Exception("HTTP 401 Unauthorized")
        self.assertTrue(_is_auth_error(exc))

    def test_403_status_is_detected_as_auth_error(self):
        exc = Exception("HTTP 403 Forbidden")
        self.assertTrue(_is_auth_error(exc))

    def test_expired_session_is_detected_as_auth_error(self):
        exc = Exception("Session expired")
        self.assertTrue(_is_auth_error(exc))

    def test_invalid_token_is_detected_as_auth_error(self):
        exc = Exception("Invalid access token")
        self.assertTrue(_is_auth_error(exc))

    def test_503_error_is_not_detected_as_auth_error(self):
        exc = Exception("HTTP 503 Service Unavailable")
        self.assertFalse(_is_auth_error(exc))

    def test_timeout_error_is_not_detected_as_auth_error(self):
        exc = Exception("Connection timeout")
        self.assertFalse(_is_auth_error(exc))


class TestRateLimitErrorDetection(unittest.TestCase):
    """Characterization of _is_rate_limit_error (the transient bucket), which
    had no direct tests before. Pins today's string heuristic so a later
    narrowing pass is a conscious, test-visible change."""

    def test_429_is_transient(self):
        self.assertTrue(_is_rate_limit_error(Exception("HTTP 429 Too Many Requests")))

    def test_rate_limit_phrase_is_transient(self):
        self.assertTrue(_is_rate_limit_error(Exception("Rate limit exceeded, slow down")))

    def test_malformed_json_wording_is_transient(self):
        # The "json" substring is what today catches Spotify answering with a
        # non-JSON bot-check page instead of the profile JSON.
        self.assertTrue(_is_rate_limit_error(Exception("Invalid JSON in response body")))

    def test_503_is_not_transient(self):
        self.assertFalse(_is_rate_limit_error(Exception("HTTP 503 Service Unavailable")))

    def test_timeout_is_not_transient(self):
        self.assertFalse(_is_rate_limit_error(Exception("Connection timeout")))


class TestClassifyListenerError(unittest.TestCase):
    """classifyListenerError is the single seam behind both predicates; its
    (isAuth, isTransient) pair must stay INDEPENDENT - some errors are both,
    and call-site precedence relies on that rather than one flag winning."""

    def test_pure_auth_error(self):
        self.assertEqual(classifyListenerError(Exception("HTTP 401 Unauthorized")), (True, False))

    def test_pure_transient_error(self):
        self.assertEqual(classifyListenerError(Exception("HTTP 429 Too Many Requests")), (False, True))

    def test_neither_bucket(self):
        self.assertEqual(classifyListenerError(Exception("HTTP 503 Service Unavailable")), (False, False))

    def test_error_that_is_both_auth_and_transient(self):
        # A rate-limited login failure matches both; the flags must stay
        # independent so each call site's own precedence still applies.
        self.assertEqual(classifyListenerError(Exception("LoginError: 429 rate limited")), (True, True))

    def test_predicates_are_thin_wrappers_over_the_pair(self):
        exc = Exception("Invalid access token")
        isAuth, isTransient = classifyListenerError(exc)
        self.assertEqual(_is_auth_error(exc), isAuth)
        self.assertEqual(_is_rate_limit_error(exc), isTransient)


class TestClassifyRealSpotapiExceptions(unittest.TestCase):
    """The classifier must handle actual spotapi exception instances, not just
    Exception('...text...'): LoginError is classified by its type name even
    when its message carries no auth keyword."""

    def test_spotapi_loginerror_is_auth_by_type_name(self):
        from spotapi.exceptions.errors import LoginError
        # Message has NO auth keyword - the type name is what classifies it.
        self.assertEqual(classifyListenerError(LoginError("Could not GET recently played")), (True, False))

    def test_spotapi_requesterror_429_is_transient(self):
        from spotapi.exceptions.errors import RequestError
        self.assertEqual(classifyListenerError(RequestError("Got status 429 from server")), (False, True))

    def test_spotapi_requesterror_503_is_neither(self):
        from spotapi.exceptions.errors import RequestError
        self.assertEqual(classifyListenerError(RequestError("Got status 503 from server")), (False, False))


class TestClassificationDiagnostic(unittest.TestCase):
    """The FLASK_DEBUG-gated diagnostic that records the concrete exception type
    at each classification - the observability a real misclassification report
    needs before the heuristics can be safely narrowed. Off by default so it
    never spams production logs."""

    _LOGGER = "Database.Listeners.spotifyListener"

    def test_logs_type_and_flags_when_flask_debug_enabled(self):
        with patch("Database.Listeners.spotifyListener._flaskDebugEnabled", return_value=True):
            with self.assertLogs(self._LOGGER, level="INFO") as cm:
                classifyListenerError(Exception("HTTP 401 Unauthorized"))
        joined = "\n".join(cm.output)
        self.assertIn("isAuth=True", joined)
        self.assertIn("isTransient=False", joined)
        self.assertIn("builtins.Exception", joined)   #< the fully-qualified type

    def test_silent_when_flask_debug_disabled(self):
        import logging
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record)
        moduleLogger = logging.getLogger(self._LOGGER)
        moduleLogger.addHandler(handler)
        try:
            with patch("Database.Listeners.spotifyListener._flaskDebugEnabled", return_value=False):
                classifyListenerError(Exception("HTTP 401 Unauthorized"))
        finally:
            moduleLogger.removeHandler(handler)
        self.assertEqual([r for r in records if "classified" in r.getMessage()], [])


class TestValidateCurrentUser(unittest.TestCase):
    def test_valid_user_returns_true(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.return_value = {"id": "user1"}
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            self.assertTrue(listener._validateCurrentUser())

    def test_mismatched_user_returns_false(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.return_value = {"id": "user2"}
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            self.assertFalse(listener._validateCurrentUser())

    def test_auth_error_returns_false_and_does_not_raise(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.side_effect = Exception("HTTP 401 Unauthorized")
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            self.assertFalse(listener._validateCurrentUser())

    def test_transient_error_bubbles_up(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.side_effect = Exception("HTTP 503 Service Unavailable")
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            with self.assertRaises(Exception) as ctx:
                listener._validateCurrentUser()
        self.assertIn("503", str(ctx.exception))

    def test_first_check_runs_even_with_a_low_monotonic_clock(self):
        """Regression test: _last_user_validation_time must start as None
        ("never validated"), not 0, so the very first check always performs
        a real validation - even on a host where time.monotonic() itself is
        still small (e.g. shortly after boot), which previously made a
        freshly constructed Listener look like it had already validated
        "recently" and silently return the unvalidated cached default."""
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.return_value = {"id": "user1"}

        lowUptimeMonotonic = 1.0  # smaller than USER_VALIDATION_CACHE_SECONDS
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=lowUptimeMonotonic):
            listener._validateCurrentUser()

        listener.sp.current_user.assert_called_once()


class TestValidationErrorCooldown(unittest.TestCase):
    """A refused profile call now starts its own cooldown.

    Before, the transient branch raised without recording anything, so the
    freshness cache stayed empty and the very next poll after the loop's 60s
    backoff called current_user() again - a 60-second cadence in place of a
    5-minute one, at the exact moment Spotify was pushing back."""

    _TRANSIENT = "Invalid JSON (Status: 200, Type: str)"

    def _listener(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        return listener

    def _validateAt(self, listener, when):
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=when):
            return listener._validateCurrentUser()

    def _failOnceAt(self, listener, when):
        listener.sp.current_user.side_effect = Exception(self._TRANSIENT)
        with self.assertRaisesRegex(Exception, "Invalid JSON"):
            self._validateAt(listener, when)
        listener.sp.current_user.reset_mock()
        listener.sp.current_user.side_effect = None
        listener.sp.current_user.return_value = {"id": "user1"}

    def test_a_refusal_still_raises_so_the_caller_backs_off(self):
        listener = self._listener()
        listener.sp.current_user.side_effect = Exception(self._TRANSIENT)

        with self.assertRaisesRegex(Exception, "Invalid JSON"):
            self._validateAt(listener, _MONOTONIC_NOW)

    def test_the_next_poll_inside_the_cooldown_makes_no_request(self):
        """This is the whole fix: the poll that follows the 60s backoff must
        not walk straight back into the endpoint that just refused."""
        listener = self._listener()
        self._failOnceAt(listener, _MONOTONIC_NOW)

        justAfterTheBackoff = _MONOTONIC_NOW + RATE_LIMIT_ERROR_BACKOFF_SECONDS + 1
        self._validateAt(listener, justAfterTheBackoff)

        listener.sp.current_user.assert_not_called()

    def test_the_cooldown_keeps_reporting_the_last_known_answer(self):
        listener = self._listener()
        listener.sp.current_user.return_value = {"id": "user2"}   #< a real mismatch
        self.assertFalse(self._validateAt(listener, _MONOTONIC_NOW))

        laterFailure = _MONOTONIC_NOW + USER_VALIDATION_CACHE_SECONDS + 1
        self._failOnceAt(listener, laterFailure)

        self.assertFalse(self._validateAt(listener, laterFailure + 1))

    def test_validation_resumes_once_the_cooldown_expires(self):
        """Shorter than the success cache on purpose - recovery is worth
        noticing promptly."""
        listener = self._listener()
        self._failOnceAt(listener, _MONOTONIC_NOW)

        self.assertTrue(self._validateAt(
            listener, _MONOTONIC_NOW + USER_VALIDATION_ERROR_COOLDOWN_SECONDS + 1))
        listener.sp.current_user.assert_called_once()

    def test_the_cooldown_is_shorter_than_the_success_cache(self):
        self.assertLess(USER_VALIDATION_ERROR_COOLDOWN_SECONDS, USER_VALIDATION_CACHE_SECONDS)

    def test_a_successful_check_clears_the_cooldown(self):
        """Otherwise a stale refusal would keep suppressing checks long after
        the endpoint recovered."""
        listener = self._listener()
        self._failOnceAt(listener, _MONOTONIC_NOW)

        recoveredAt = _MONOTONIC_NOW + USER_VALIDATION_ERROR_COOLDOWN_SECONDS + 1
        self._validateAt(listener, recoveredAt)

        self.assertIsNone(listener._last_validation_error_time)

    def test_an_auth_error_starts_no_cooldown(self):
        """An auth failure is an answer, not a refusal - it must keep returning
        False on every poll so the reconnect fires."""
        listener = self._listener()
        listener.sp.current_user.side_effect = Exception("HTTP 401 Unauthorized")

        self.assertFalse(self._validateAt(listener, _MONOTONIC_NOW))
        self.assertIsNone(listener._last_validation_error_time)
        self.assertFalse(self._validateAt(listener, _MONOTONIC_NOW + 1))
        self.assertEqual(listener.sp.current_user.call_count, 2)


class TestPollIntervalJitter(unittest.TestCase):
    """Every listener polling at exactly the same rate means two accounts that
    start near each other arrive together on every tick, forever. The shared
    limiter would then de-align them by blocking a thread; spreading them here
    means it rarely has to."""

    def test_the_interval_stays_within_the_declared_band(self):
        for _ in range(50):
            interval = _pollIntervalWithJitter()
            self.assertGreaterEqual(interval, LISTENER_POLL_INTERVAL_SECONDS)
            self.assertLessEqual(
                interval, LISTENER_POLL_INTERVAL_SECONDS + LISTENER_POLL_INTERVAL_JITTER_SECONDS)

    def test_separate_draws_do_not_all_land_on_the_same_value(self):
        self.assertGreater(len({_pollIntervalWithJitter() for _ in range(50)}), 1)

    def test_the_base_cadence_is_a_named_constant(self):
        """It used to be a bare `refreshInterval=6` default - the single
        biggest lever on how much Spotify traffic this app generates, with no
        name to find it by."""
        self.assertEqual(LISTENER_POLL_INTERVAL_SECONDS, 6)

    def _build(self, **kwargs):
        sp = MagicMock()
        sp.isLoggedIn.return_value = True
        sp.current_user.return_value = {"id": "u1", "email": None}
        sp.current_user_recently_played.return_value = []
        with patch("Database.Listeners.spotifyListener.Spotify", return_value=sp):
            listener = Listener(cookiesFile="unused.json", **kwargs)
        return listener, sp

    def test_a_listener_polls_at_its_own_jittered_interval(self):
        listener, sp = self._build()

        sp.startRecentlyPlayedListener.assert_called_once_with(
            refreshInterval=listener.refreshInterval)
        self.assertGreaterEqual(listener.refreshInterval, LISTENER_POLL_INTERVAL_SECONDS)
        self.assertLessEqual(
            listener.refreshInterval,
            LISTENER_POLL_INTERVAL_SECONDS + LISTENER_POLL_INTERVAL_JITTER_SECONDS)

    def test_an_explicit_interval_is_honoured_verbatim(self):
        """So a test can pin the cadence without fighting the jitter."""
        listener, sp = self._build(refreshInterval=2)

        self.assertEqual(listener.refreshInterval, 2)
        sp.startRecentlyPlayedListener.assert_called_once_with(refreshInterval=2)


class TestRateLimitBackoffLogging(unittest.TestCase):
    """The routine rate-limit backoff is one line. The exception that caused it
    was already logged by the path that raised it (e.g. _validateCurrentUser's
    transient branch), and Spotify's rate-limit reply is an HTML fallback page,
    so repeating it here only doubled the noise. FLASK_DEBUG brings it back for
    the paths that raise without logging first."""

    _LOGGER = "Database.Listeners.spotifyListener"
    _ERROR_TEXT = "Invalid JSON (Status: 200, Type: str)"

    def _runOneRateLimitedIteration(self, user="7kevinegger"):
        listener = _bareListener()
        listener.user = user
        listener.contaminationDetected = False
        listener._stop_event = MagicMock()
        listener._stop_event.is_set.side_effect = [False, True]  #< one iteration, then stop
        listener._checkOnce = MagicMock(side_effect=Exception(self._ERROR_TEXT))

        with self.assertLogs(self._LOGGER, level="WARNING") as logCapture:
            listener.startListener(MagicMock())
        return listener, logCapture

    def test_routine_backoff_logs_one_line_without_the_exception(self):
        with patch("Database.Listeners.spotifyListener._flaskDebugEnabled", return_value=False):
            listener, logCapture = self._runOneRateLimitedIteration()

        self.assertEqual(len(logCapture.output), 1)
        emitted = logCapture.output[0]
        self.assertIn("Rate limit error detected", emitted)
        self.assertIn(str(RATE_LIMIT_ERROR_BACKOFF_SECONDS), emitted)
        self.assertNotIn("Invalid JSON", emitted)
        # The backoff itself is unchanged - only the logging got quieter.
        listener._stop_event.wait.assert_any_call(RATE_LIMIT_ERROR_BACKOFF_SECONDS)

    def test_flask_debug_restores_the_exception_detail(self):
        with patch("Database.Listeners.spotifyListener._flaskDebugEnabled", return_value=True):
            _, logCapture = self._runOneRateLimitedIteration()

        emitted = "\n".join(logCapture.output)
        self.assertIn("Rate limit error detected", emitted)
        self.assertIn(self._ERROR_TEXT, emitted)

    def test_the_backoff_line_names_the_user(self):
        """Which account hit the limit was the one thing these lines never
        said - with four listeners logging identically there was no way to
        tell one struggling account from an instance-wide problem."""
        with patch("Database.Listeners.spotifyListener._flaskDebugEnabled", return_value=False):
            _, logCapture = self._runOneRateLimitedIteration(user="7kevinegger")

        self.assertIn("7kevinegger", "\n".join(logCapture.output))


class TestRateLimitPausesEveryListener(unittest.TestCase):
    """The listener's own 60s wait only ever paused this thread - whose sole
    rate-limited call is the 5-minutely _validateCurrentUser, since the
    recently-played read is a local cache hit. The traffic that actually
    provokes Spotify (~10 connect-state requests a minute PER USER) runs on
    spotapi's thread and never saw it. So the backoff that matters is the
    process-wide one."""

    _LOGGER = "Database.Listeners.spotifyListener"
    _ERROR_TEXT = "Invalid JSON (Status: 200, Type: str)"

    def _runOneRateLimitedIteration(self):
        listener = _bareListener()
        listener.user = "7kevinegger"
        listener.contaminationDetected = False
        listener._stop_event = MagicMock()
        listener._stop_event.is_set.side_effect = [False, True]  #< one iteration, then stop
        listener._checkOnce = MagicMock(side_effect=Exception(self._ERROR_TEXT))
        with self.assertLogs(self._LOGGER, level="WARNING"):
            listener.startListener(MagicMock())
        return listener

    def test_a_rate_limit_opens_a_process_wide_backoff(self):
        from Database.rate_limit import SPOTIFY_LIMITER
        from Database.Listeners.spotifyListener import RATE_LIMIT_REASON_LISTENER_POLL

        self._runOneRateLimitedIteration()

        snapshot = SPOTIFY_LIMITER.snapshot()
        self.assertEqual(snapshot["backoffs"], 1)
        self.assertEqual(snapshot["lastReason"], RATE_LIMIT_REASON_LISTENER_POLL)
        self.assertGreater(snapshot["backoffRemainingSeconds"], 0)

    def test_the_local_wait_is_kept_as_well(self):
        """Retrying validation inside a window we just opened would be pointless
        traffic, so this thread still stands down too."""
        from Database.Listeners.spotifyListener import RATE_LIMIT_ERROR_BACKOFF_SECONDS

        listener = self._runOneRateLimitedIteration()

        listener._stop_event.wait.assert_any_call(RATE_LIMIT_ERROR_BACKOFF_SECONDS)

    def test_an_ordinary_error_pauses_nothing(self):
        """Only rate limits are instance-wide. A plain listener bug must not
        stall every other user's tracking."""
        from Database.rate_limit import SPOTIFY_LIMITER

        listener = _bareListener()
        listener.contaminationDetected = False
        listener._stop_event = MagicMock()
        listener._stop_event.is_set.side_effect = [False, True]
        listener._checkOnce = MagicMock(side_effect=Exception("something else broke"))

        with self.assertLogs("Database.Listeners.spotifyListener", level="ERROR"):
            listener.startListener(MagicMock())

        self.assertEqual(SPOTIFY_LIMITER.snapshot()["backoffs"], 0)


if __name__ == "__main__":
    unittest.main()
