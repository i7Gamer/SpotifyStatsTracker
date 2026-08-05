"""Behavior tests for Database.Spotify.recentlyPlayed - the poll loop, the
push loop, mode selection, shutdown, and the shared played-duration maths.

These began life in tests/test_patches.py, written against the update loop
Database/patches.py grafted onto spotipyFree's LastPlayedManger; the Phase 1.5
cutover re-pointed them at the owned RecentlyPlayedManager
(dependencyRewritePlan.md).
"""

import unittest
from unittest.mock import MagicMock, patch

import spotapi.status


class _ScriptedStateManager:
    """Stands in for LastPlayedManger.manager: each `state` access consumes the
    next scripted result - Exception instances are raised, anything else is
    returned. reconnect() is a plain MagicMock for call assertions."""
    def __init__(self, results):
        self._results = list(results)
        self.reconnect = MagicMock()

    @property
    def state(self):
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _StatelessButHealthyManager:
    """A manager whose connect-state PUT SUCCEEDS while the returned cluster
    carries no player_state - an account with no live Connect session. The
    patched renew_state stamps stateRenewalSucceededAt before the state
    property raises spotapi's usual ValueError, which is the only thing that
    distinguishes this from a genuinely failing PUT."""
    def __init__(self, stamps):
        self._stamps = list(stamps)
        self.reconnect = MagicMock()
        self.stateRenewalSucceededAt = None

    @property
    def state(self):
        self.stateRenewalSucceededAt = self._stamps.pop(0)
        raise ValueError("Could not get player state")


def makeIdleState():
    """A state the update loop skips gracefully (no timestamp/track) - fetching
    it still counts as a successful poll."""
    state = MagicMock()
    state.timestamp = None
    state.track = None
    return state


def makePlayingState(uid="uid-1", uri="spotify:track:aaa"):
    """A state _applyStateToTracking treats as an active track - two of these
    with different uids make the poll loop fire the play callback."""
    state = MagicMock()
    state.timestamp = 1700000000000
    state.track = MagicMock()
    state.track.uid = uid
    state.track.uri = uri
    state.context_uri = "spotify:playlist:ctx"
    state.position_as_of_timestamp = 1000
    state.is_paused = False
    state.duration = 200000
    return state


class TestPollLoop(unittest.TestCase):
    """The poll loop must tolerate degraded states, escalate a failure streak
    to exactly one reconnect, and reset the streak on success."""

    def test_a_poll_loop_that_exits_stops_vouching_for_its_last_renewal(self):
        """stateRenewalSucceededAt is what tells the stale check that an absent
        player_state is a real idle account rather than a dead tick. It is
        stamped by the tick, so once the tick is gone it must stop answering -
        exactly the rule _runPushLoop's finally already applies to
        pushChannelAliveAt and subscriptionRenewedAt.

        Without it: the poll thread dies (a closed-session error sets run=False
        and returns) while startListener keeps looping, and for the next
        LISTENER_POLL_RENEWAL_FRESH_SECONDS the listener reads 'idle' from a
        stamp left by a thread that no longer exists."""
        from Database.Spotify.recentlyPlayed import RecentlyPlayedManager

        manager = MagicMock()
        manager._deliberate_close = True   #< the simplest of the loop's exits
        manager.stateRenewalSucceededAt = 1234.5

        with patch("spotapi.status.PlayerStatus"):
            lpm = RecentlyPlayedManager(MagicMock())
        lpm.manager = manager
        lpm.run = True

        lpm.updateLoop(MagicMock(), refreshInterval=1)

        self.assertIsNone(manager.stateRenewalSucceededAt)

    def test_patched_update_loop_handles_none_timestamp_gracefully(self):
        """updateLoop should sleep and continue without raising or calling reconnect when state or timestamp is None."""
        from Database.Spotify.recentlyPlayed import RecentlyPlayedManager
        import time
        
        manager = MagicMock()
        manager._deliberate_close = False  #< a bare MagicMock's auto-attribute is truthy = deliberate close
        callback = MagicMock()

        state_none_timestamp = MagicMock()
        state_none_timestamp.timestamp = None
        state_none_timestamp.track = None

        manager.state = state_none_timestamp
        
        with patch("spotapi.status.PlayerStatus"):
            lpm = RecentlyPlayedManager(MagicMock())
        lpm.manager = manager
        lpm.run = True
        
        def mock_sleep(secs):
            lpm.run = False
            
        with patch("time.sleep", side_effect=mock_sleep):
            lpm.updateLoop(callback, refreshInterval=1)
            
        callback.assert_not_called()
        manager.reconnect.assert_not_called()

    def _runUpdateLoopIterations(self, manager, iterations):
        """Run updateLoop for exactly `iterations` passes (each pass
        ends in one time.sleep call), returning the callback mock."""
        from Database.Spotify.recentlyPlayed import RecentlyPlayedManager

        callback = MagicMock()
        with patch("spotapi.status.PlayerStatus"):
            lpm = RecentlyPlayedManager(MagicMock())
        lpm.manager = manager
        lpm.run = True

        sleepCount = [0]

        def mockSleep(_secs):
            sleepCount[0] += 1
            if sleepCount[0] >= iterations:
                lpm.run = False

        with patch("time.sleep", side_effect=mockSleep):
            lpm.updateLoop(callback, refreshInterval=1)
        return callback

    def test_patched_update_loop_transient_state_valueerror_does_not_reconnect(self):
        """A ValueError from manager.state (spotapi's 'Could not get player state')
        below the escalation threshold must be treated like state=None: warn
        concisely and retry - no reconnect, no callback, no ERROR-level spam."""
        from Database.Spotify.recentlyPlayed import STATE_FAILURE_RECONNECT_THRESHOLD

        failures = STATE_FAILURE_RECONNECT_THRESHOLD - 1
        manager = _ScriptedStateManager(
            [ValueError("Could not get player state")] * failures
        )

        with self.assertLogs("Database.Spotify.recentlyPlayed", level="WARNING") as cm:
            callback = self._runUpdateLoopIterations(manager, failures)

        manager.reconnect.assert_not_called()
        callback.assert_not_called()
        self.assertTrue(all(record.levelname == "WARNING" for record in cm.records))
        self.assertEqual(len(cm.records), failures)

    def test_patched_update_loop_reconnects_after_consecutive_state_failures(self):
        """Once manager.state fails STATE_FAILURE_RECONNECT_THRESHOLD times in a
        row, the loop must escalate to exactly one manager.reconnect()."""
        from Database.Spotify.recentlyPlayed import STATE_FAILURE_RECONNECT_THRESHOLD

        manager = _ScriptedStateManager(
            [ValueError("Could not get player state")] * STATE_FAILURE_RECONNECT_THRESHOLD
        )

        self._runUpdateLoopIterations(manager, STATE_FAILURE_RECONNECT_THRESHOLD)

        manager.reconnect.assert_called_once_with()

    def test_a_stateless_account_is_not_a_failure_streak(self):
        """An account with no live Connect session answers every poll with a
        successful PUT and no player_state. Counting those as failures put the
        PROCESS-WIDE limiter into a 30s backoff and reconnected the websocket
        roughly every minute, for as long as the user simply wasn't casting
        anything - the request storm the limiter exists to prevent, with every
        other user's tracking paused in 30-second slices."""
        from Database.Spotify.recentlyPlayed import STATE_FAILURE_RECONNECT_THRESHOLD

        polls = STATE_FAILURE_RECONNECT_THRESHOLD + 2
        manager = _StatelessButHealthyManager(range(1, polls + 1))

        with patch("Database.Spotify.recentlyPlayed.SPOTIFY_LIMITER") as limiter:
            callback = self._runUpdateLoopIterations(manager, polls)

        manager.reconnect.assert_not_called()
        limiter.applyBackoff.assert_not_called()
        callback.assert_not_called()

    def test_a_failing_put_still_escalates_when_the_stamp_stands_still(self):
        """The distinction is the stamp MOVING: a PUT that failed leaves it
        where it was, and that streak must still reach exactly one reconnect."""
        from Database.Spotify.recentlyPlayed import STATE_FAILURE_RECONNECT_THRESHOLD

        frozenStamp = [7.0] * STATE_FAILURE_RECONNECT_THRESHOLD
        manager = _StatelessButHealthyManager(frozenStamp)
        # renew_state only stamps on success, so a failing PUT leaves the
        # previous success's value standing - which is what "stands still" is.
        manager.stateRenewalSucceededAt = 7.0

        with patch("Database.Spotify.recentlyPlayed.SPOTIFY_LIMITER") as limiter:
            self._runUpdateLoopIterations(manager, STATE_FAILURE_RECONNECT_THRESHOLD)

        manager.reconnect.assert_called_once_with()
        limiter.applyBackoff.assert_called_once()

    def test_patched_update_loop_successful_poll_resets_failure_counter(self):
        """A successful state fetch between failure streaks must reset the
        consecutive-failure counter, so two sub-threshold streaks separated by a
        success never trigger a reconnect."""
        from Database.Spotify.recentlyPlayed import STATE_FAILURE_RECONNECT_THRESHOLD

        streak = STATE_FAILURE_RECONNECT_THRESHOLD - 1
        results = (
            [ValueError("Could not get player state")] * streak
            + [makeIdleState()]
            + [ValueError("Could not get player state")] * streak
        )
        manager = _ScriptedStateManager(results)

        self._runUpdateLoopIterations(manager, len(results))

        manager.reconnect.assert_not_called()

    def test_a_raising_callback_does_not_reconnect(self):
        """A play callback that raises (track()'s retry ladder re-raising, a
        rate limit) is a metadata failure, not a transport failure - the state
        was read fine. Escalating it to manager.reconnect() rebuilt the
        websocket AND the session every poll tick for as long as the callback
        kept failing (worst during a TOTP rotation, where the reconnect's own
        get_session() also fails and bumps the auth-failure counter). Push got
        this containment in _applyPushedState; this pins poll's equivalent."""
        stateA = makePlayingState(uid="uid-1", uri="spotify:track:aaa")
        stateB = makePlayingState(uid="uid-2", uri="spotify:track:bbb")
        manager = _ScriptedStateManager([stateA, stateB])
        callbackCalls = []

        def failingCallback(*args):
            callbackCalls.append(args)
            raise RuntimeError("track lookup blew up")

        from Database.Spotify.recentlyPlayed import RecentlyPlayedManager
        with patch("spotapi.status.PlayerStatus"):
            lpm = RecentlyPlayedManager(MagicMock())
        lpm.manager = manager
        lpm.run = True

        sleepCount = [0]

        def mockSleep(_secs):
            sleepCount[0] += 1
            if sleepCount[0] >= 2:
                lpm.run = False

        with self.assertLogs("Database.Spotify.recentlyPlayed", level="WARNING"), \
                patch("time.sleep", side_effect=mockSleep):
            lpm.updateLoop(failingCallback, refreshInterval=1)

        manager.reconnect.assert_not_called()
        self.assertEqual(len(callbackCalls), 1)
        self.assertEqual(callbackCalls[0][0], "spotify:track:aaa")   #< the PREVIOUS track

    def test_a_failed_play_is_retried_on_the_next_poll(self):
        """Containment must not turn into dropping: lastPlayedUid only advances
        after the callback returns, so the next poll of the same state retries
        the same play - mirroring the push loop exactly."""
        stateA = makePlayingState(uid="uid-1", uri="spotify:track:aaa")
        stateB = makePlayingState(uid="uid-2", uri="spotify:track:bbb")
        manager = _ScriptedStateManager([stateA, stateB, stateB])
        callback = MagicMock(side_effect=RuntimeError("still failing"))

        from Database.Spotify.recentlyPlayed import RecentlyPlayedManager
        with patch("spotapi.status.PlayerStatus"):
            lpm = RecentlyPlayedManager(MagicMock())
        lpm.manager = manager
        lpm.run = True

        sleepCount = [0]

        def mockSleep(_secs):
            sleepCount[0] += 1
            if sleepCount[0] >= 3:
                lpm.run = False

        with self.assertLogs("Database.Spotify.recentlyPlayed", level="WARNING"), \
                patch("time.sleep", side_effect=mockSleep):
            lpm.updateLoop(callback, refreshInterval=1)

        manager.reconnect.assert_not_called()
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(callback.call_args[0][0], "spotify:track:aaa")   #< same previous track, retried


class TestSessionClosedDetection(unittest.TestCase):
    """_isSessionClosedError must recognize curl_cffi's dead-session state in a
    raw message, in spotapi's .error detail attribute, and through the
    __cause__/__context__ chain - and nothing else."""

    def test_detects_direct_message(self):
        from Database.Spotify.recentlyPlayed import _isSessionClosedError
        self.assertTrue(_isSessionClosedError(
            RuntimeError("Session is closed, cannot send request.")))

    def test_detects_error_detail_attribute(self):
        """spotapi's RequestError keeps the underlying curl_cffi detail in
        .error, not in str(e)."""
        from Database.Spotify.recentlyPlayed import _isSessionClosedError
        from spotapi.exceptions import RequestError
        exc = RequestError("Failed to complete request.",
                           error="Session is closed, cannot send request.")
        self.assertTrue(_isSessionClosedError(exc))

    def test_detects_chained_cause(self):
        from Database.Spotify.recentlyPlayed import _isSessionClosedError
        try:
            try:
                raise RuntimeError("Session is closed, cannot send request.")
            except RuntimeError as inner:
                raise ValueError("outer wrapper") from inner
        except ValueError as outer:
            self.assertTrue(_isSessionClosedError(outer))

    def test_ignores_unrelated_errors_and_none(self):
        from Database.Spotify.recentlyPlayed import _isSessionClosedError
        self.assertFalse(_isSessionClosedError(RuntimeError("429: rate limited")))
        self.assertFalse(_isSessionClosedError(None))

    def test_survives_self_referencing_chain(self):
        from Database.Spotify.recentlyPlayed import _isSessionClosedError
        exc = RuntimeError("nope")
        exc.__context__ = exc  #< pathological cycle must not hang the check
        self.assertFalse(_isSessionClosedError(exc))


class TestUpdateLoopShutdown(unittest.TestCase):
    """updateLoop must self-terminate when its transport is gone
    for good (deliberate close / closed HTTP session) instead of retrying
    forever - a leftover loop kept spamming reconnect errors every few seconds
    after Ctrl+C during the 2026-07-17 shutdown hang."""

    def _makeLpm(self, manager):
        from Database.Spotify.recentlyPlayed import RecentlyPlayedManager
        with patch("spotapi.status.PlayerStatus"):
            lpm = RecentlyPlayedManager(MagicMock())
        lpm.manager = manager
        lpm.run = True
        return lpm

    def test_exits_on_deliberate_close_without_polling(self):
        manager = _ScriptedStateManager([])  #< any state access would IndexError
        manager._deliberate_close = True
        lpm = self._makeLpm(manager)

        lpm.updateLoop(MagicMock(), refreshInterval=1)

        self.assertFalse(lpm.run)
        manager.reconnect.assert_not_called()

    def test_no_single_sleep_outlasts_the_join_that_waits_for_this_loop(self):
        """Listener.stop() sets `run = False` and then joins this thread with
        LISTENER_STOP_JOIN_TIMEOUT_SECONDS. `run` is only re-read at the top of
        the loop, so a sleep longer than that join means the join expires
        rather than the thread exiting cleanly.

        The normal inter-poll sleep is the problem, not an edge case: live it
        is LISTENER_POLL_INTERVAL_SECONDS plus jitter (6-7s) against a 5s
        join, and the sleep dominates the loop - so the join timed out on
        essentially every shutdown of a poll-mode listener. The push loop
        never had this: its recv timeout re-checks `run` every second.

        Asserted on the durations requested, not on elapsed time."""
        from Database.Spotify.recentlyPlayed import POLL_STOP_POLL_SECONDS
        from Database.Listeners.spotifyListener import (
            LISTENER_POLL_INTERVAL_SECONDS, LISTENER_STOP_JOIN_TIMEOUT_SECONDS)

        self.assertLess(POLL_STOP_POLL_SECONDS, LISTENER_STOP_JOIN_TIMEOUT_SECONDS,
                        "a sleeping loop must notice the stop flag inside the join window")

        lpm = self._makeLpm(_ScriptedStateManager([makeIdleState()] * 10))
        sleeps = []

        def recordAndStop(seconds):
            sleeps.append(seconds)
            lpm.run = False   #< as Listener.stop() does, mid-sleep

        with patch("time.sleep", side_effect=recordAndStop):
            lpm.updateLoop(MagicMock(), refreshInterval=LISTENER_POLL_INTERVAL_SECONDS)

        self.assertTrue(sleeps, "the loop should have slept at least once")
        self.assertLessEqual(
            max(sleeps), POLL_STOP_POLL_SECONDS,
            f"a {max(sleeps)}s sleep cannot be interrupted by the "
            f"{LISTENER_STOP_JOIN_TIMEOUT_SECONDS}s join waiting on this thread")

    def test_the_error_backoff_sleep_is_interruptible_too(self):
        """The catch-all's backoff is UPDATE_LOOP_ERROR_SLEEP_SECONDS - twice
        the join - so a loop that errored on its way into shutdown was the
        worst case of all."""
        from Database.Spotify.recentlyPlayed import (
            POLL_STOP_POLL_SECONDS, UPDATE_LOOP_ERROR_SLEEP_SECONDS)

        manager = _ScriptedStateManager([RuntimeError("boom")] * 5)
        lpm = self._makeLpm(manager)
        sleeps = []

        def recordAndStop(seconds):
            sleeps.append(seconds)
            lpm.run = False

        with patch("time.sleep", side_effect=recordAndStop):
            lpm.updateLoop(MagicMock(), refreshInterval=1)

        self.assertTrue(sleeps)
        self.assertLessEqual(max(sleeps), POLL_STOP_POLL_SECONDS,
                             f"the {UPDATE_LOOP_ERROR_SLEEP_SECONDS}s error backoff must be "
                             "interruptible")

    def test_a_stop_during_the_error_backoff_skips_the_reconnect(self):
        """Rebuilding the websocket on the way out is exactly what the
        _deliberate_close guard exists to prevent (a leftover loop spamming
        reconnect errors through the 2026-07-17 shutdown). Reaching the
        backoff and being stopped inside it is the same situation."""
        manager = _ScriptedStateManager([RuntimeError("boom")] * 5)
        lpm = self._makeLpm(manager)

        def stopMidSleep(_seconds):
            lpm.run = False

        with patch("time.sleep", side_effect=stopMidSleep):
            lpm.updateLoop(MagicMock(), refreshInterval=1)

        manager.reconnect.assert_not_called()

    def test_stops_when_reconnect_hits_closed_session(self):
        """Once the HTTP session is closed, reconnect can never succeed - the
        loop must stop itself instead of cycling warn/error forever."""
        from Database.Spotify.recentlyPlayed import STATE_FAILURE_RECONNECT_THRESHOLD
        from spotapi.exceptions import RequestError

        manager = _ScriptedStateManager(
            [ValueError("Could not get player state")] * STATE_FAILURE_RECONNECT_THRESHOLD
        )
        manager.reconnect = MagicMock(side_effect=RequestError(
            "Failed to complete request.",
            error="Session is closed, cannot send request."))
        lpm = self._makeLpm(manager)

        with patch("time.sleep"):  #< the loop must terminate on its own
            lpm.updateLoop(MagicMock(), refreshInterval=1)

        self.assertFalse(lpm.run)
        manager.reconnect.assert_called_once_with()

    def test_keeps_retrying_on_other_reconnect_errors(self):
        """A non-closed-session reconnect failure keeps the retry loop alive -
        transient outages must still recover once Spotify is reachable again."""
        from Database.Spotify.recentlyPlayed import STATE_FAILURE_RECONNECT_THRESHOLD

        from Database.rate_limit import SPOTIFY_LIMITER

        script = [ValueError("Could not get player state")] * (STATE_FAILURE_RECONNECT_THRESHOLD + 1)
        manager = _ScriptedStateManager(script)
        manager.reconnect = MagicMock(side_effect=RuntimeError("Spotify unreachable"))
        lpm = self._makeLpm(manager)

        sleepCount = [0]

        def mockSleep(_secs):
            sleepCount[0] += 1
            if sleepCount[0] >= len(script):
                lpm.run = False

        # Escalating to a reconnect also opens a process-wide backoff window
        # (see TestConnectStatePollLimiting), which would hold the poll after
        # it - correct in production, but here it would stop the loop before
        # it could prove it survived the failed reconnect. Grant every slot so
        # this test stays about the reconnect path alone.
        with patch.object(SPOTIFY_LIMITER, "acquire", return_value=True):
            with patch("time.sleep", side_effect=mockSleep):
                lpm.updateLoop(MagicMock(), refreshInterval=1)

        manager.reconnect.assert_called_once_with()
        self.assertEqual(manager._results, [])  #< loop survived past the failed reconnect


def pushedCluster(trackUri="spotify:track:aaa", uid="uid-1", timestampMs="1785367061986",
                  contextUri="spotify:playlist:ctx", isPaused=False):
    """A connect-state cluster in the exact shape the dealer pushes - verified
    against the raw frames Phase 0 captured (payloads[].cluster.player_state,
    same keys connect_device's reply carries)."""
    return {
        "timestamp": timestampMs,
        "active_device_id": "device-1",
        "devices": {"device-1": {"name": "laptop"}},
        "player_state": {
            "timestamp": timestampMs,
            "context_uri": contextUri,
            "is_playing": True,
            "is_paused": isPaused,
            "duration": "200000",
            "position_as_of_timestamp": "1000",
            "prev_tracks": [],
            "next_tracks": [],
            "track": {"uri": trackUri, "uid": uid, "metadata": {"title": "Song"}},
        },
    }


def pushFrame(cluster, updateReason="DEVICE_STATE_CHANGED"):
    return {"headers": {}, "type": "message", "uri": "hm://connect-state/v1/cluster",
            "payloads": [{"update_reason": updateReason, "cluster": cluster}]}


class _PushManager:
    """Stands in for PlayerStatus during a push loop: hands out scripted frames
    from get_packet and records connect_device calls.

    Starts with EMPTY caches on purpose. spotapi's connect_device() only returns
    the cluster - it never writes _state/_device_dump (renew_state is what
    normally does that), so a double that pre-filled them hid the fact that the
    push loop began with no state at all."""

    def __init__(self, frames, initialCluster=None):
        self._frames = list(frames)
        self._deliberate_close = False
        self._device_dump = None
        self._state = None
        self._devices = None
        self.cluster = initialCluster      #< what connect_device replies with
        self.connectCalls = 0
        self.connectError = None
        self.onExhausted = None

    def connect_device(self):
        self.connectCalls += 1
        if self.connectError is not None:
            raise self.connectError
        return self.cluster

    def get_packet(self, timeout=None):
        if self._frames:
            return self._frames.pop(0)
        if self.onExhausted is not None:
            self.onExhausted()
        return None


def _pushLastPlayed(manager):
    from Database.Spotify.recentlyPlayed import RecentlyPlayedManager
    with patch("spotapi.status.PlayerStatus"):
        lpm = RecentlyPlayedManager(MagicMock())
    lpm.manager = manager
    lpm.run = True
    return lpm


class TestPushedClusterHandling(unittest.TestCase):
    """A dealer frame carries more than playback - keepalive pongs and
    social-connect broadcasts share the socket. Identifying a connect-state
    frame by its CONTENT (a cluster with a player_state) rather than by its uri
    string means an upstream rename degrades to 'ignored', not to a listener
    that silently records nothing."""

    def test_a_connect_state_frame_yields_its_cluster(self):
        from Database.Spotify.recentlyPlayed import _clusterFromPacket

        cluster = pushedCluster()
        self.assertIs(_clusterFromPacket(pushFrame(cluster)), cluster)

    def test_a_keepalive_pong_is_not_a_cluster(self):
        from Database.Spotify.recentlyPlayed import _clusterFromPacket

        self.assertIsNone(_clusterFromPacket({"type": "pong"}))

    def test_a_social_connect_broadcast_is_not_a_cluster(self):
        """Seen live in Phase 0: uri social-connect/v2/broadcast_status_update,
        payloads present, no cluster."""
        from Database.Spotify.recentlyPlayed import _clusterFromPacket

        frame = {"type": "message", "uri": "social-connect/v2/broadcast_status_update",
                 "payloads": [{"deviceBroadcastStatus": {}}]}
        self.assertIsNone(_clusterFromPacket(frame))

    def test_a_cluster_without_a_player_state_is_ignored(self):
        from Database.Spotify.recentlyPlayed import _clusterFromPacket

        frame = {"payloads": [{"cluster": {"devices": {}}}]}
        self.assertIsNone(_clusterFromPacket(frame))

    def test_adopting_a_cluster_fills_the_same_caches_renew_state_does(self):
        """getConnectPlayerState (Now Playing, the missed-track cross-check)
        reads _state, and device_ids reads _device_dump - a push has to leave
        all of them exactly as a connect_device reply would."""
        from Database.Spotify.recentlyPlayed import _adoptCluster

        manager = _PushManager([])
        cluster = pushedCluster()

        self.assertTrue(_adoptCluster(manager, cluster))
        self.assertIs(manager._device_dump, cluster)
        self.assertIs(manager._state, cluster["player_state"])
        self.assertEqual(manager._devices, cluster["devices"])


class TestPlayedDuration(unittest.TestCase):
    """time_played used to be wall-clock since the track became current, which
    counts pause time. app.log 2026-07-30 10:14:59 recorded a track paused
    overnight as 35,377,992ms - 154x its length - and only the corruption guard
    stopped it landing as a full play. Worse: a track paused 30 seconds in and
    abandoned got clamped to its duration and counted as a COMPLETE listen,
    which is also what decides plays.is_skip.

    This is on the shared path, so it applies to polling as well as push."""

    def _state(self, uid="uid-1", positionMs="30000", timestampMs="1785000000000",
               durationMs="200000", isPaused=False, trackUri="spotify:track:aaa"):
        cluster = pushedCluster(trackUri=trackUri, uid=uid, timestampMs=timestampMs,
                                isPaused=isPaused)
        cluster["player_state"]["position_as_of_timestamp"] = positionMs
        cluster["player_state"]["duration"] = durationMs
        return spotapi.status.PlayerState.from_dict(cluster["player_state"])

    def _observe(self, lpm, state, callback, nowEpoch):
        from Database.Spotify.recentlyPlayed import _applyStateToTracking
        with patch("Database.Spotify.recentlyPlayed.time.time", return_value=nowEpoch):
            _applyStateToTracking(lpm, state, callback)

    def test_a_paused_track_records_its_paused_position(self):
        """The regression: paused 30s in at 1785000000, changed 5 hours later.
        The old code recorded 5 hours."""
        lpm = _pushLastPlayed(_PushManager([]))
        callback = MagicMock()

        self._observe(lpm, self._state(uid="uid-1", positionMs="30000", isPaused=True),
                      callback, nowEpoch=1785000000)
        self._observe(lpm, self._state(uid="uid-2", trackUri="spotify:track:bbb"),
                      callback, nowEpoch=1785000000 + 5 * 3600)

        callback.assert_called_once()
        self.assertEqual(callback.call_args[0][3], 30000)

    def test_a_playing_track_counts_position_plus_elapsed(self):
        lpm = _pushLastPlayed(_PushManager([]))
        callback = MagicMock()

        #< snapshot says 30s in at t=1785000000; 10s later it is 40s in
        self._observe(lpm, self._state(uid="uid-1", positionMs="30000",
                                       timestampMs="1785000000000"),
                      callback, nowEpoch=1785000000)
        self._observe(lpm, self._state(uid="uid-2", trackUri="spotify:track:bbb"),
                      callback, nowEpoch=1785000010)

        self.assertEqual(callback.call_args[0][3], 40000)

    def test_it_never_exceeds_the_track_duration(self):
        lpm = _pushLastPlayed(_PushManager([]))
        callback = MagicMock()

        self._observe(lpm, self._state(uid="uid-1", positionMs="30000", durationMs="200000"),
                      callback, nowEpoch=1785000000)
        self._observe(lpm, self._state(uid="uid-2", trackUri="spotify:track:bbb"),
                      callback, nowEpoch=1785000000 + 3600)

        self.assertLessEqual(callback.call_args[0][3], 200000)

    def test_joining_mid_track_counts_where_playback_actually_was(self):
        """A listener joins mid-track on EVERY rebuild, and lastPlayedAt is then
        its first sighting, not the real start. Measuring from that sighting -
        which is what capping at wall-clock would do - reports 10s for a track
        already 3 minutes in, and a full listen looks like a skip."""
        lpm = _pushLastPlayed(_PushManager([]))
        callback = MagicMock()

        self._observe(lpm, self._state(uid="uid-1", positionMs="180000", isPaused=True),
                      callback, nowEpoch=1785000000)
        self._observe(lpm, self._state(uid="uid-2", trackUri="spotify:track:bbb"),
                      callback, nowEpoch=1785000010)

        self.assertEqual(callback.call_args[0][3], 180000)

    def test_a_state_without_a_position_falls_back_to_wall_clock(self):
        """Old behaviour, kept for anything that doesn't carry the fields."""
        from Database.Spotify.recentlyPlayed import _playedMsForOutgoingTrack
        import datetime as dt

        lpm = _pushLastPlayed(_PushManager([]))
        lpm.lastPlayedAt = dt.datetime.fromtimestamp(1785000000, tz=dt.timezone.utc)

        with patch("Database.Spotify.recentlyPlayed.time.time", return_value=1785000060):
            self.assertEqual(_playedMsForOutgoingTrack(lpm, None), 60000)

    def test_the_observation_is_refreshed_without_a_track_change(self):
        """A mid-track pause is exactly the update that makes the next
        measurement accurate, so it must be recorded even though nothing
        changed tracks."""
        from Database.Spotify.recentlyPlayed import _applyStateToTracking

        lpm = _pushLastPlayed(_PushManager([]))
        callback = MagicMock()

        self._observe(lpm, self._state(uid="uid-1", positionMs="1000"), callback,
                      nowEpoch=1785000000)
        self._observe(lpm, self._state(uid="uid-1", positionMs="95000", isPaused=True),
                      callback, nowEpoch=1785000094)

        callback.assert_not_called()
        self.assertEqual(lpm._lastObservedPlayback[0], 95000)
        self.assertTrue(lpm._lastObservedPlayback[2])


class TestPushLoop(unittest.TestCase):
    """The push loop must record plays through exactly the same code the poll
    loop uses (_applyStateToTracking), and must hand back to polling rather
    than ever record nothing silently."""

    def _run(self, manager, lpm=None):
        from Database.Spotify.recentlyPlayed import _runPushLoop

        lpm = lpm or _pushLastPlayed(manager)
        callback = MagicMock()
        manager.onExhausted = lambda: setattr(lpm, "run", False)
        return _runPushLoop(lpm, callback), callback, lpm

    def test_a_pushed_track_change_records_the_previous_track_once(self):
        first, second = pushedCluster(uid="uid-1"), pushedCluster(
            trackUri="spotify:track:bbb", uid="uid-2")
        manager = _PushManager([pushFrame(first), pushFrame(second)], initialCluster=first)

        outcome, callback, _ = self._run(manager)

        self.assertEqual(outcome, "stopped")
        callback.assert_called_once()
        self.assertEqual(callback.call_args[0][0], "spotify:track:aaa")   #< the PREVIOUS track

    def test_a_repeated_push_for_the_same_track_records_nothing(self):
        cluster = pushedCluster(uid="uid-1")
        manager = _PushManager([pushFrame(cluster), pushFrame(cluster), pushFrame(cluster)],
                               initialCluster=cluster)

        _, callback, _ = self._run(manager)

        callback.assert_not_called()

    def test_a_pause_push_is_not_a_track_change(self):
        playing = pushedCluster(uid="uid-1")
        paused = pushedCluster(uid="uid-1", isPaused=True)
        manager = _PushManager([pushFrame(playing), pushFrame(paused)], initialCluster=playing)

        _, callback, _ = self._run(manager)

        callback.assert_not_called()

    def test_the_pushed_state_reaches_getConnectPlayerState(self):
        """Now Playing and the missed-track cross-check read manager._state
        directly - if push didn't keep it current they would freeze."""
        first = pushedCluster(uid="uid-1")
        second = pushedCluster(trackUri="spotify:track:bbb", uid="uid-2")
        manager = _PushManager([pushFrame(second)], initialCluster=first)

        self._run(manager)

        self.assertEqual(manager._state["track"]["uri"], "spotify:track:bbb")

    def test_a_raising_callback_does_not_kill_the_push_loop(self):
        """The poll loop's catch-all contains a raising play-finished callback
        (log, sleep, reconnect); push had no guard, so the same failure - e.g.
        track() re-raising after its retry ladder - killed the thread with
        `run` still True and _state frozen. A frozen state reads as IDLE to
        _staleFeedBrokenReason, so nothing rebuilt for the 6h hard timeout and
        every play in the window was lost, silently."""
        first = pushedCluster(uid="uid-1")
        second = pushedCluster(trackUri="spotify:track:bbb", uid="uid-2")
        manager = _PushManager([pushFrame(second)], initialCluster=first)
        lpm = _pushLastPlayed(manager)
        manager.onExhausted = lambda: setattr(lpm, "run", False)
        callback = MagicMock(side_effect=RuntimeError("track lookup blew up"))

        from Database.Spotify.recentlyPlayed import _runPushLoop
        with self.assertLogs("Database.Spotify.recentlyPlayed", level="WARNING"):
            outcome = _runPushLoop(lpm, callback)   #< must not raise

        self.assertEqual(outcome, "stopped")
        callback.assert_called_once()

    def test_a_failed_play_is_retried_on_the_next_observation(self):
        """Containment must not turn into dropping: when the callback raises,
        lastPlayedUid stays un-advanced (the callback runs before the state
        update), so the next pushed frame retries the same play - mirroring
        the poll loop exactly."""
        first = pushedCluster(uid="uid-1")
        second = pushedCluster(trackUri="spotify:track:bbb", uid="uid-2")
        manager = _PushManager([pushFrame(second), pushFrame(second)], initialCluster=first)
        lpm = _pushLastPlayed(manager)
        manager.onExhausted = lambda: setattr(lpm, "run", False)
        callback = MagicMock(side_effect=RuntimeError("still failing"))

        from Database.Spotify.recentlyPlayed import _runPushLoop
        with self.assertLogs("Database.Spotify.recentlyPlayed", level="WARNING"):
            _runPushLoop(lpm, callback)

        self.assertEqual(callback.call_count, 2)
        self.assertEqual(callback.call_args[0][0], "spotify:track:aaa")   #< same previous track, retried

    def test_a_deliberate_close_stops_without_falling_back(self):
        manager = _PushManager([], initialCluster=pushedCluster())
        lpm = _pushLastPlayed(manager)
        manager._deliberate_close = True

        outcome, callback, _ = self._run(manager, lpm=lpm)

        self.assertEqual(outcome, "stopped")
        callback.assert_not_called()

    def test_total_frame_silence_falls_back_to_polling(self):
        """Keyed on ANY frame, pongs included: genuine state silence ran to
        3.75 h in Phase 0, so only a socket with no traffic at all is dead."""
        from Database.Spotify.recentlyPlayed import PUSH_FRAME_SILENCE_FALLBACK_SECONDS

        manager = _PushManager([], initialCluster=pushedCluster())
        manager.onExhausted = None   #< never stop; the watchdog must be what ends it
        lpm = _pushLastPlayed(manager)
        clock = [1000.0]

        def advancingMonotonic():
            clock[0] += PUSH_FRAME_SILENCE_FALLBACK_SECONDS / 2
            return clock[0]

        from Database.Spotify.recentlyPlayed import _runPushLoop
        with patch("Database.Spotify.recentlyPlayed.time.monotonic", side_effect=advancingMonotonic):
            with self.assertLogs("Database.Spotify.recentlyPlayed", level="WARNING"):
                outcome = _runPushLoop(lpm, MagicMock())

        self.assertEqual(outcome, "fallback")

    def test_a_keepalive_pong_keeps_the_channel_alive(self):
        """A pong carries no state but proves the socket works, so it must
        reset the watchdog. Phase 0 saw 3.75 h between state pushes against a
        pong every 60s - without this an idle account would flap back to
        polling every few minutes.

        The clock is driven past the fallback threshold deliberately: a test
        that merely feeds two pongs in real time passes whether or not the
        reset happens."""
        from Database.Spotify.recentlyPlayed import PUSH_FRAME_SILENCE_FALLBACK_SECONDS, _runPushLoop

        pongs = 4
        manager = _PushManager([{"type": "pong"} for _ in range(pongs)],
                               initialCluster=pushedCluster())
        lpm = _pushLastPlayed(manager)
        manager.onExhausted = lambda: setattr(lpm, "run", False)

        clock = [1000.0]

        def steppingMonotonic():
            #< each pass advances half the threshold, so two unreset passes trip it
            clock[0] += PUSH_FRAME_SILENCE_FALLBACK_SECONDS / 2
            return clock[0]

        callback = MagicMock()
        #< the re-subscribe interval is held out of the way so this measures the
        #  frame watchdog alone. It is 5 minutes now - the same value as
        #  PUSH_FRAME_SILENCE_FALLBACK_SECONDS - so at this clock step it fires
        #  inside the window, and its extra monotonic() reads advance the clock
        #  enough to trip the very fallback the test says must not happen. The
        #  two thresholds are independent and merely happen to be equal; nothing
        #  in the loop links them.
        with patch("Database.Spotify.recentlyPlayed.time.monotonic", side_effect=steppingMonotonic), \
             patch("Database.Spotify.recentlyPlayed.CONNECT_STATE_RESUBSCRIBE_SECONDS", 24 * 3600):
            outcome = _runPushLoop(lpm, callback)

        self.assertEqual(outcome, "stopped")   #< never fell back despite the elapsed time
        callback.assert_not_called()

    def test_the_subscribe_reply_seeds_the_state(self):
        """connect_device() returns the cluster but never writes the caches -
        renew_state is what normally does that, and push mode does not call it.
        Without adopting the reply the loop would start blind, and the first
        real push would look like a track change from nothing."""
        manager = _PushManager([], initialCluster=pushedCluster(uid="uid-1"))

        self._run(manager)

        self.assertEqual(manager._state["track"]["uid"], "uid-1")
        self.assertIsInstance(manager._device_dump, dict)

    def test_a_first_push_after_seeding_is_not_a_phantom_change(self):
        """Seeded with uid-1, pushed uid-1: nothing played, nothing recorded."""
        cluster = pushedCluster(uid="uid-1")
        manager = _PushManager([pushFrame(cluster)], initialCluster=cluster)

        _, callback, _ = self._run(manager)

        callback.assert_not_called()

    def test_push_failover_window_stays_within_a_quarter_hour(self):
        """How long a user's plays can go unrecorded before push gives up.

        lastSubscribeAt is stamped whatever the re-subscribe returned - on
        purpose, so a failing endpoint is not retried once per loop pass - which
        makes the re-subscribe interval the RETRY interval too. The window is
        therefore the product of the two constants, and neither one says so on
        its own: at the original 15-minute interval, three failures took 45
        minutes to reach the poll loop that would have recorded those plays.

        Asserted as a bound rather than an equality: raising either constant is
        allowed, but not past the point where the fallback stops being a
        fallback."""
        from Database.Spotify.recentlyPlayed import (
            CONNECT_STATE_RESUBSCRIBE_SECONDS, PUSH_RESUBSCRIBE_MAX_FAILURES,
        )

        window = CONNECT_STATE_RESUBSCRIBE_SECONDS * PUSH_RESUBSCRIBE_MAX_FAILURES

        self.assertLessEqual(window, 15 * 60,
                             "push now takes %d minutes to fall back to polling; every play in that "
                             "window is lost for a user with no Web API credentials"
                             % (window // 60))

    def test_a_periodic_resubscribe_refreshes_the_state(self):
        """The floor under push mode: if the subscription silently stops
        delivering while the socket stays healthy, pongs keep the watchdog
        quiet and _state would freeze forever. Re-subscribing re-reads it, and
        a change that happened meanwhile is recovered rather than lost."""
        from Database.Spotify.recentlyPlayed import CONNECT_STATE_RESUBSCRIBE_SECONDS, _runPushLoop

        # Pongs throughout: the socket is healthy, so the frame watchdog stays
        # quiet - which is exactly the failure it cannot see, and why the
        # re-subscribe has to be the probe.
        manager = _PushManager([{"type": "pong"}] * 10,
                               initialCluster=pushedCluster(uid="uid-1"))
        lpm = _pushLastPlayed(manager)
        manager.onExhausted = lambda: setattr(lpm, "run", False)

        clock = [1000.0]
        step = CONNECT_STATE_RESUBSCRIBE_SECONDS / 2

        def steppingMonotonic():
            clock[0] += step
            return clock[0]

        original = manager.connect_device

        def connectThenAdvanceTheTrack():
            result = original()
            #< after the seeding call, Spotify reports a different track: the
            #  change that arrived while pushes were silently dead
            manager.cluster = pushedCluster(trackUri="spotify:track:bbb", uid="uid-2")
            if manager.connectCalls >= 2:
                lpm.run = False
            return result
        manager.connect_device = connectThenAdvanceTheTrack

        callback = MagicMock()
        with patch("Database.Spotify.recentlyPlayed.time.monotonic", side_effect=steppingMonotonic):
            _runPushLoop(lpm, callback)

        self.assertGreaterEqual(manager.connectCalls, 2)     #< it really re-subscribed
        callback.assert_called_once()                         #< and caught the missed change
        self.assertEqual(callback.call_args[0][0], "spotify:track:aaa")

    def test_the_initial_subscribe_takes_a_limiter_slot(self):
        from Database.rate_limit import SPOTIFY_LIMITER

        manager = _PushManager([], initialCluster=pushedCluster())
        with patch.object(SPOTIFY_LIMITER, "acquire", return_value=True) as acquire:
            self._run(manager)

        acquire.assert_called()
        self.assertEqual(manager.connectCalls, 1)

    def test_a_failed_initial_subscribe_falls_back(self):
        manager = _PushManager([], initialCluster=pushedCluster())
        manager.connectError = RuntimeError("connect-state refused")

        with self.assertLogs("Database.Spotify.recentlyPlayed", level="WARNING"):
            outcome, callback, _ = self._run(manager)

        self.assertEqual(outcome, "fallback")

    def test_a_locally_paused_slot_is_not_a_subscribe_failure(self):
        """Same conflation that turned one rate-limit event into one per
        listener: our own backoff window is not evidence about Spotify, and
        must not push us off the push channel."""
        from Database.rate_limit import SPOTIFY_LIMITER

        manager = _PushManager([], initialCluster=pushedCluster())
        lpm = _pushLastPlayed(manager)
        with patch.object(SPOTIFY_LIMITER, "acquire", side_effect=[False, False, True]):
            outcome, _, _ = self._run(manager, lpm=lpm)

        self.assertEqual(outcome, "stopped")     #< kept waiting, never fell back
        self.assertEqual(manager.connectCalls, 1)


class TestPushSubscriptionStamp(unittest.TestCase):
    """subscriptionRenewedAt: monotonic stamp of the last SUCCESSFUL
    connect-state subscribe. A successful re-PUT re-registers the subscription
    and runs the returned cluster through track detection, so a fresh stamp
    proves the subscription cannot be silently wedged - which is what lets the
    listener's stale check defer its 6h hard ceiling instead of re-logging-in
    every idle user four times a day (live app.log 2026-08-01 to 08-04, one
    rebuild per user per 6h on the dot). Stamp-not-flag, same as
    pushChannelAliveAt: a dead loop leaves its last value behind, and only a
    fresh one vouches."""

    def test_a_fresh_manager_has_no_subscription_stamp(self):
        lpm = _pushLastPlayed(_PushManager([]))
        self.assertIsNone(lpm.subscriptionRenewedAt)

    def test_a_successful_subscribe_stamps_and_the_exit_clears(self):
        from Database.Spotify.recentlyPlayed import _runPushLoop

        manager = _PushManager([], initialCluster=pushedCluster())
        lpm = _pushLastPlayed(manager)
        seen = []
        manager.onExhausted = lambda: (seen.append(lpm.subscriptionRenewedAt),
                                       setattr(lpm, "run", False))

        _runPushLoop(lpm, MagicMock())

        self.assertIsInstance(seen[0], float)          #< stamped while the loop ran
        self.assertIsNone(lpm.subscriptionRenewedAt)   #< and stopped vouching on exit

    def test_a_periodic_resubscribe_advances_the_stamp(self):
        from Database.Spotify.recentlyPlayed import CONNECT_STATE_RESUBSCRIBE_SECONDS, _runPushLoop

        manager = _PushManager([{"type": "pong"}] * 10, initialCluster=pushedCluster())
        lpm = _pushLastPlayed(manager)
        manager.onExhausted = lambda: setattr(lpm, "run", False)

        clock = [1000.0]

        def steppingMonotonic():
            clock[0] += CONNECT_STATE_RESUBSCRIBE_SECONDS / 2
            return clock[0]

        stamps = []
        original = manager.connect_device

        def recordingConnect():
            result = original()
            stamps.append(lpm.subscriptionRenewedAt)   #< value BEFORE this call's stamp lands
            if manager.connectCalls >= 2:
                lpm.run = False
            return result
        manager.connect_device = recordingConnect

        with patch("Database.Spotify.recentlyPlayed.time.monotonic", side_effect=steppingMonotonic):
            _runPushLoop(lpm, MagicMock())

        self.assertGreaterEqual(manager.connectCalls, 2)
        #< the re-subscribe saw the initial stamp already set, then advanced it
        self.assertIsInstance(stamps[1], float)

    def test_a_frame_silence_fallback_clears_the_stamp(self):
        from Database.Spotify.recentlyPlayed import PUSH_FRAME_SILENCE_FALLBACK_SECONDS, _runPushLoop

        manager = _PushManager([], initialCluster=pushedCluster())
        manager.onExhausted = None   #< never stop; the watchdog must end it
        lpm = _pushLastPlayed(manager)
        clock = [1000.0]

        def advancingMonotonic():
            clock[0] += PUSH_FRAME_SILENCE_FALLBACK_SECONDS / 2
            return clock[0]

        with patch("Database.Spotify.recentlyPlayed.time.monotonic", side_effect=advancingMonotonic):
            with self.assertLogs("Database.Spotify.recentlyPlayed", level="WARNING"):
                outcome = _runPushLoop(lpm, MagicMock())

        self.assertEqual(outcome, "fallback")
        self.assertIsNone(lpm.subscriptionRenewedAt)


class TestPushLoopWithoutAnyState(unittest.TestCase):
    """connect_device() can reply with no player_state at all - an account with
    no live Connect session - and _adoptCluster leaves the caches untouched when
    it does. The seeding call guarded that; the periodic re-subscribe did not,
    so PlayerState.from_dict(None) raised "argument of type 'NoneType' is not
    iterable" once every CONNECT_STATE_RESUBSCRIBE_SECONDS, forever (live
    app.log 2026-08-01 12:37-15:07, one warning per 901s)."""

    def _stateless(self):
        """A push loop whose subscribe reply carries no player_state, driven
        past one re-subscribe boundary."""
        from Database.Spotify.recentlyPlayed import CONNECT_STATE_RESUBSCRIBE_SECONDS

        manager = _PushManager([{"type": "pong"}] * 10, initialCluster={"devices": {}})
        lpm = _pushLastPlayed(manager)
        manager.onExhausted = lambda: setattr(lpm, "run", False)

        clock = [1000.0]

        def steppingMonotonic():
            clock[0] += CONNECT_STATE_RESUBSCRIBE_SECONDS / 2
            return clock[0]

        original = manager.connect_device

        def stopAfterTheResubscribe():
            result = original()
            if manager.connectCalls >= 2:
                lpm.run = False
            return result
        manager.connect_device = stopAfterTheResubscribe
        return manager, lpm, steppingMonotonic

    def test_a_resubscribe_without_any_adopted_state_is_silent(self):
        from Database.Spotify.recentlyPlayed import _runPushLoop

        manager, lpm, steppingMonotonic = self._stateless()
        callback = MagicMock()

        with patch("Database.Spotify.recentlyPlayed.time.monotonic", side_effect=steppingMonotonic):
            with self.assertNoLogs("Database.Spotify.recentlyPlayed", level="WARNING"):
                _runPushLoop(lpm, callback)

        self.assertGreaterEqual(manager.connectCalls, 2)   #< it really re-subscribed
        self.assertIsNone(manager._state)                  #< and still had nothing to apply
        callback.assert_not_called()

    def test_an_idle_account_still_reports_a_live_push_channel(self):
        """The knock-on the warning was only the visible half of: with _state
        never populated, getConnectPlayerState() returns None forever, which
        _staleFeedBrokenReason reads as a dead session - a full spotapi re-login
        every LISTENER_STALE_TIMEOUT_SECONDS, the exact regression that check
        was written to stop. A live channel has to be visible some other way."""
        from Database.Spotify.recentlyPlayed import _runPushLoop

        manager, lpm, steppingMonotonic = self._stateless()
        seen = []
        original = manager.get_packet

        def recordAliveStamp(timeout=None):
            seen.append(lpm.pushChannelAliveAt)
            return original(timeout)
        manager.get_packet = recordAliveStamp

        with patch("Database.Spotify.recentlyPlayed.time.monotonic", side_effect=steppingMonotonic):
            _runPushLoop(lpm, MagicMock())

        self.assertTrue(all(isinstance(stamp, float) for stamp in seen), seen)


class TestPushChannelLiveness(unittest.TestCase):
    """pushChannelAliveAt is how the listener's stale check tells "idle account
    on a working push channel" from "the tick that feeds _state is dead". It is
    a monotonic stamp rather than a flag so a thread that died still reads as
    not-alive once the stamp ages out."""

    def test_a_frame_refreshes_the_stamp(self):
        cluster = pushedCluster(uid="uid-1")
        manager = _PushManager([pushFrame(cluster)], initialCluster=cluster)
        lpm = _pushLastPlayed(manager)
        seen = []
        original = manager.get_packet
        # The clock only moves between reads, so the stamps are exact rather
        # than "whatever the host managed between two loop passes". A scripted
        # side_effect list can't be used: patching this module's time.monotonic
        # patches the time module itself, so every other module that reads the
        # clock during the loop (the shared limiter) draws from it too.
        clock = [10.0]

        def recordAfterEachRead(timeout=None):
            packet = original(timeout)
            seen.append(lpm.pushChannelAliveAt)
            clock[0] += 10.0
            return packet
        manager.get_packet = recordAfterEachRead
        manager.onExhausted = lambda: setattr(lpm, "run", False)

        from Database.Spotify.recentlyPlayed import _runPushLoop
        with patch("Database.Spotify.recentlyPlayed.time.monotonic", side_effect=lambda: clock[0]):
            _runPushLoop(lpm, MagicMock())

        self.assertEqual(seen[0], 10.0)   #< seeded by the subscribe, before any frame
        self.assertEqual(seen[1], 20.0)   #< the frame moved it on

    def test_leaving_the_push_loop_clears_the_stamp(self):
        """Poll mode's "no connect state means the tick is dead" reasoning is
        true again the moment this loop is not the one feeding _state - so a
        fallback must not leave a stamp behind that keeps the stale check
        permanently reassured."""
        manager = _PushManager([], initialCluster=pushedCluster())
        lpm = _pushLastPlayed(manager)
        manager.onExhausted = lambda: setattr(lpm, "run", False)

        from Database.Spotify.recentlyPlayed import _runPushLoop
        _runPushLoop(lpm, MagicMock())

        self.assertIsNone(lpm.pushChannelAliveAt)

    def test_a_raising_push_loop_still_clears_the_stamp(self):
        manager = _PushManager([], initialCluster=pushedCluster())
        lpm = _pushLastPlayed(manager)
        manager.get_packet = MagicMock(side_effect=RuntimeError("socket exploded"))

        from Database.Spotify.recentlyPlayed import _runPushLoop
        with self.assertRaises(RuntimeError):
            _runPushLoop(lpm, MagicMock())

        self.assertIsNone(lpm.pushChannelAliveAt)

    def test_a_fresh_manager_is_not_on_a_push_channel(self):
        manager = _PushManager([])
        self.assertIsNone(_pushLastPlayed(manager).pushChannelAliveAt)


class TestUpdateLoopModeSelection(unittest.TestCase):
    """The toggle is read once per loop entry, and anything unreadable keeps
    polling - a settings lookup must never be what changes how plays are
    recorded."""

    def tearDown(self):
        from Database.Spotify.recentlyPlayed import setPushListenerEnabledHook
        setPushListenerEnabledHook(None)

    def test_no_hook_means_polling(self):
        from Database.Spotify.recentlyPlayed import _pushListenerEnabled, setPushListenerEnabledHook

        setPushListenerEnabledHook(None)
        self.assertFalse(_pushListenerEnabled())

    def test_a_raising_hook_means_polling(self):
        from Database.Spotify.recentlyPlayed import _pushListenerEnabled, setPushListenerEnabledHook

        setPushListenerEnabledHook(MagicMock(side_effect=RuntimeError("db down")))
        with self.assertLogs("Database.Spotify.recentlyPlayed", level="WARNING"):
            self.assertFalse(_pushListenerEnabled())

    def test_the_hook_enables_push(self):
        from Database.Spotify.recentlyPlayed import _pushListenerEnabled, setPushListenerEnabledHook

        setPushListenerEnabledHook(lambda: True)
        self.assertTrue(_pushListenerEnabled())

    def test_with_push_off_the_loop_never_reads_the_socket(self):
        from Database.Spotify.recentlyPlayed import setPushListenerEnabledHook
        from Database.Spotify.recentlyPlayed import RecentlyPlayedManager

        setPushListenerEnabledHook(lambda: False)
        manager = _ScriptedStateManager([makeIdleState()])
        manager.get_packet = MagicMock()
        with patch("spotapi.status.PlayerStatus"):
            lpm = RecentlyPlayedManager(MagicMock())
        lpm.manager = manager
        lpm.run = True

        with patch("time.sleep", side_effect=lambda _s: setattr(lpm, "run", False)):
            lpm.updateLoop(MagicMock(), refreshInterval=1)

        manager.get_packet.assert_not_called()

    def test_a_push_fallback_hands_over_to_polling(self):
        """The fallback has to actually reach the poll loop, or 'falls back
        automatically' is just a log line."""
        from Database.Spotify.recentlyPlayed import setPushListenerEnabledHook
        from Database.Spotify.recentlyPlayed import RecentlyPlayedManager

        setPushListenerEnabledHook(lambda: True)
        manager = _ScriptedStateManager([makeIdleState()])
        manager._deliberate_close = False
        manager.get_packet = MagicMock(return_value=None)
        manager.connect_device = MagicMock(side_effect=RuntimeError("no subscription"))
        with patch("spotapi.status.PlayerStatus"):
            lpm = RecentlyPlayedManager(MagicMock())
        lpm.manager = manager
        lpm.run = True

        with self.assertLogs("Database.Spotify.recentlyPlayed", level="WARNING"):
            with patch("time.sleep", side_effect=lambda _s: setattr(lpm, "run", False)):
                lpm.updateLoop(MagicMock(), refreshInterval=1)

        self.assertEqual(manager._results, [])   #< the poll loop consumed the scripted state


class TestConnectStatePollLimiting(unittest.TestCase):
    """The connect-state poll loop is the dominant Spotify traffic in this
    process (~10 requests/minute per user at refreshInterval=6) and was the one
    caller no backoff could reach - it runs on spotapi's own thread, not the
    listener's."""

    def _lastPlayedManager(self, manager):
        from Database.Spotify.recentlyPlayed import RecentlyPlayedManager
        with patch("spotapi.status.PlayerStatus"):
            lpm = RecentlyPlayedManager(MagicMock())
        lpm.manager = manager
        lpm.run = True
        return lpm

    def _runIterations(self, manager, iterations):
        lpm = self._lastPlayedManager(manager)
        callback = MagicMock()
        sleepCount = [0]

        def mockSleep(_secs):
            sleepCount[0] += 1
            if sleepCount[0] >= iterations:
                lpm.run = False

        with patch("time.sleep", side_effect=mockSleep):
            lpm.updateLoop(callback, refreshInterval=1)
        return callback

    def test_each_poll_takes_a_slot(self):
        from Database.rate_limit import SPOTIFY_LIMITER

        manager = _ScriptedStateManager([makeIdleState(), makeIdleState()])

        with patch.object(SPOTIFY_LIMITER, "acquire", return_value=True) as acquire:
            self._runIterations(manager, 2)

        self.assertEqual(acquire.call_count, 2)

    def test_a_refused_slot_skips_the_poll_without_counting_a_failure(self):
        """Being held back locally is not a Spotify failure: counting it would
        escalate a backoff into a websocket reconnect, i.e. MORE traffic."""
        from Database.rate_limit import SPOTIFY_LIMITER

        manager = _ScriptedStateManager([])   #< any state access would IndexError
        lpm = self._lastPlayedManager(manager)
        refusals = [0]

        def refuseThenStop(*args, **kwargs):
            refusals[0] += 1
            if refusals[0] >= 3:
                lpm.run = False
            return False

        with patch.object(SPOTIFY_LIMITER, "acquire", side_effect=refuseThenStop):
            lpm.updateLoop(MagicMock(), refreshInterval=1)

        manager.reconnect.assert_not_called()

    def test_a_state_failure_streak_pauses_every_spotify_request(self):
        """spotapi collapses a throttled connect-state PUT to a bare
        ValueError, so a whole streak of them is the only throttling signal
        this endpoint gives us."""
        from Database.Spotify.recentlyPlayed import STATE_FAILURE_RECONNECT_THRESHOLD, ENDPOINT_CONNECT_STATE
        from Database.rate_limit import SPOTIFY_LIMITER

        manager = _ScriptedStateManager(
            [ValueError("Could not get player state")] * STATE_FAILURE_RECONNECT_THRESHOLD
        )

        self._runIterations(manager, STATE_FAILURE_RECONNECT_THRESHOLD)

        snapshot = SPOTIFY_LIMITER.snapshot()
        self.assertEqual(snapshot["backoffs"], 1)
        self.assertEqual(snapshot["lastReason"], ENDPOINT_CONNECT_STATE)

    def test_a_sub_threshold_streak_pauses_nothing(self):
        """One blip must not stall every user - only a sustained streak does."""
        from Database.Spotify.recentlyPlayed import STATE_FAILURE_RECONNECT_THRESHOLD
        from Database.rate_limit import SPOTIFY_LIMITER

        failures = STATE_FAILURE_RECONNECT_THRESHOLD - 1
        manager = _ScriptedStateManager([ValueError("Could not get player state")] * failures)

        with self.assertLogs("Database.Spotify.recentlyPlayed", level="WARNING"):
            self._runIterations(manager, failures)

        self.assertEqual(SPOTIFY_LIMITER.snapshot()["backoffs"], 0)


class TestPushTimingConstantsAgree(unittest.TestCase):
    """The listener's two push-freshness windows are sized against timings that
    live in recentlyPlayed, but are spelled as local constants there to avoid
    the import - so nothing connected the two, and changing the cadence from 15
    to 5 minutes silently invalidated the arithmetic one of them was derived
    from ("two cadences plus slack"; 35 minutes is seven of them now).

    Tests are under no import-avoidance constraint, so this is where the two
    halves can be held together. Both assertions state the property the windows
    exist to have, not the numbers they currently hold - a re-timing that keeps
    the property is free, and one that breaks it fails here instead of in a
    comment nobody re-reads.
    """

    def test_the_subscription_window_outlasts_the_push_loop_giving_up(self):
        """subscriptionRenewedAt defers the stale check's 6h ceiling, and the
        push loop clears that stamp in its finally when it hands back to
        polling. So the window only has to survive until the loop gives up:
        shorter, and the ceiling would stop deferring for a loop that is still
        retrying and about to succeed."""
        from Database.Listeners.spotifyListener import LISTENER_PUSH_SUBSCRIPTION_FRESH_SECONDS
        from Database.Spotify.recentlyPlayed import (
            CONNECT_STATE_RESUBSCRIBE_SECONDS, PUSH_RESUBSCRIBE_MAX_FAILURES,
        )

        givesUpAfter = PUSH_RESUBSCRIBE_MAX_FAILURES * CONNECT_STATE_RESUBSCRIBE_SECONDS

        self.assertGreater(
            LISTENER_PUSH_SUBSCRIPTION_FRESH_SECONDS, givesUpAfter,
            "the freshness window must outlast the loop's own give-up point")

    def test_one_missed_resubscribe_does_not_forfeit_the_deferral(self):
        """The original intent, kept as a property: a single failed attempt is
        not evidence of a wedged subscription."""
        from Database.Listeners.spotifyListener import LISTENER_PUSH_SUBSCRIPTION_FRESH_SECONDS
        from Database.Spotify.recentlyPlayed import CONNECT_STATE_RESUBSCRIBE_SECONDS

        self.assertGreater(LISTENER_PUSH_SUBSCRIPTION_FRESH_SECONDS,
                           2 * CONNECT_STATE_RESUBSCRIBE_SECONDS)

    def test_the_channel_window_outlasts_the_frame_silence_fallback(self):
        """pushChannelAliveAt's twin rule, and the one whose comment stayed
        true: a loop mid-fallback must not read as a dead channel."""
        from Database.Listeners.spotifyListener import LISTENER_PUSH_CHANNEL_ALIVE_SECONDS
        from Database.Spotify.recentlyPlayed import PUSH_FRAME_SILENCE_FALLBACK_SECONDS

        self.assertGreaterEqual(LISTENER_PUSH_CHANNEL_ALIVE_SECONDS,
                                2 * PUSH_FRAME_SILENCE_FALLBACK_SECONDS)


if __name__ == "__main__":
    unittest.main()
