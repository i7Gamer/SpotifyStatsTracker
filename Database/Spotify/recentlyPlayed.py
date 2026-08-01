# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Watches one user's playback and reports each finished play - the owned
replacement for spotipyFree's LastPlayedManger plus the update-loop machinery
Database/patches.py used to graft onto it (dependencyRewritePlan.md Phase 1.4).

Two modes behind one entry point. Polling (the default) reads manager.state
every refreshInterval; it tolerates a state or state.timestamp of None, and
only escalates a persistent "Could not get player state" streak to a websocket
reconnect. Push mode, when the admin toggle is on, listens for the
connect-state the dealer websocket already delivers, and hands back to polling
at the first sign of doubt.

Everything here was written for this application (eventDrivenConnectStatePlan.md
Phase 2 landed it as monkey-patches); moving it into a class we own removes the
patch indirection, nothing else. The loop bodies stay module-level functions
taking `self` explicitly so they can be tested directly instead of through a
thread.

Until the Phase 1.5 cutover, Database/patches.py carries its own copy of this
machinery to drive the still-installed spotipyFree wrapper - that copy, and the
whole spotipyFree patch layer, is deleted when call sites move here.
"""

import copy
import datetime
import logging
import threading
import time

import spotapi.status

from Database.rate_limit import (
    SPOTIFY_LIMITER, SPOTIFY_ACQUIRE_TIMEOUT_SECONDS,
    SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS,
)

logger = logging.getLogger(__name__)

ENDPOINT_CONNECT_STATE = "connect-state"  #< shared-limiter backoff reason label, shown on /admin

# Reading manager.state PUTs to connect-state every refreshInterval - ~10
# requests a minute PER USER, and by far the largest source of Spotify traffic
# this app generates. But connect_device() registers our Spotify-Connection-Id
# with the dealer websocket, which is what makes Spotify PUSH state changes down
# a socket that is already open. Phase 0 measured 10 hours of that channel: one
# HTTP request in total, against the ~5,500 the poll would have made.
#
# Push mode is off by default. It replaces how plays are detected - the app's
# core job - and Phase 0's sample was 9 minutes of listening, enough to clear
# every structural unknown but not to call push reliable. Turn it on
# deliberately, watch it, and the poll loop is still there underneath.

# How often to re-PUT connect_device. Phase 0 saw the subscription survive
# 9h46m untouched, so this is belt-and-braces rather than a requirement - but it
# doubles as the "is the subscription still real?" probe, which silence alone
# cannot answer. At 15 minutes it is 4 requests an hour against ~600.
CONNECT_STATE_RESUBSCRIBE_SECONDS = 15 * 60

# Fall back to polling after this long with NO frame of any kind.
#
# Deliberately keyed on any frame, not on state pushes: spotapi's keepalive
# draws a pong every 60s, so the socket has a once-a-minute liveness beat, while
# genuine state silence ran to 3.75 hours overnight. A watchdog on state pushes
# would either false-positive constantly or be too slow to be worth having.
PUSH_FRAME_SILENCE_FALLBACK_SECONDS = 5 * 60

PUSH_RECV_TIMEOUT_SECONDS = 1.0     #< how long each read waits before the loop re-checks its stop flags
PUSH_RESUBSCRIBE_MAX_FAILURES = 3   #< consecutive re-subscribe errors before handing back to polling

# How many consecutive "Could not get player state" failures the poll loop
# tolerates before escalating to a websocket reconnect.
STATE_FAILURE_RECONNECT_THRESHOLD = 5

UPDATE_LOOP_ERROR_SLEEP_SECONDS = 10  #< back off after an unexpected updateLoop error before reconnecting

# Frames arrive for things other than playback (keepalive pongs,
# social-connect/v2/broadcast_status_update). A connect-state frame is
# identified by carrying a cluster with a player_state, rather than by matching
# the uri string, so a URI rename upstream degrades to "ignored" rather than to
# a silently dead listener.
_pushListenerEnabledHook = None


def setPushListenerEnabledHook(hook) -> None:
    """Install the callable that says whether push mode is on instance-wide.

    A hook rather than a constructor argument because the decision is made three
    layers above where it is needed (Database.startListener -> Listener ->
    Spotify.startRecentlyPlayedListener -> RecentlyPlayedManager.start, which
    starts the thread itself), and the setting is instance-wide anyway - there
    is nothing per-listener to thread through. app.py installs it at startup;
    without it, push mode stays off."""
    global _pushListenerEnabledHook
    _pushListenerEnabledHook = hook


def _pushListenerEnabled() -> bool:
    """Never let a failing settings lookup decide to change how plays are
    recorded - an unreadable toggle means keep polling."""
    if _pushListenerEnabledHook is None:
        return False
    try:
        return bool(_pushListenerEnabledHook())
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read the push-listener setting, staying on polling: %s", e)
        return False


def _clusterFromPacket(packet) -> dict | None:
    """The connect-state cluster carried by a dealer frame, or None if this
    frame isn't one."""
    if not isinstance(packet, dict):
        return None
    payloads = packet.get("payloads")
    if not isinstance(payloads, list):
        return None
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        cluster = payload.get("cluster")
        if isinstance(cluster, dict) and isinstance(cluster.get("player_state"), dict):
            return cluster
    return None


def _adoptCluster(manager, cluster: dict) -> bool:
    """Write a pushed cluster into PlayerStatus's caches.

    The push carries the same shape connect_device() replies with -
    player_state / devices / active_device_id - so this is exactly what
    player_status_renew_state does, minus the HTTP request. Keeping _device_dump
    in step matters: device_ids/active_device_id read it."""
    state = cluster.get("player_state")
    if not isinstance(state, dict):
        return False
    manager._device_dump = cluster
    manager._state = state
    manager._devices = cluster.get("devices")
    return True


SESSION_CLOSED_ERROR_MARKER = "session is closed"  #< curl_cffi's "Session is closed, cannot send
                                                    #  request." - the HTTP session backing this manager
                                                    #  was closed (listener stop or GC) and can never
                                                    #  serve another request


def _isSessionClosedError(exc: BaseException | None) -> bool:
    """True when an exception (or anything reachable through its .error detail
    attribute or __cause__/__context__ chain) reports curl_cffi's closed-session
    state - a dead transport no amount of retrying can revive. spotapi wraps the
    curl_cffi error in RequestError("Failed to complete request.", error=...),
    so str(exc) alone is not enough."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if SESSION_CLOSED_ERROR_MARKER in str(exc).lower():
            return True
        detail = getattr(exc, "error", None)
        if detail is not None and SESSION_CLOSED_ERROR_MARKER in str(detail).lower():
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _connectStateInt(value) -> int:
    """Connect-state numbers arrive as strings ("duration": "215000"); 0 for
    anything missing or malformed."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _observedPlayback(state):
    """(positionMs, stateTimestampMs, isPaused, durationMs) for a player state,
    or None when it carries no usable position."""
    stateTimestampMs = _connectStateInt(getattr(state, "timestamp", None))
    if stateTimestampMs <= 0:
        return None
    return (
        _connectStateInt(getattr(state, "position_as_of_timestamp", None)),
        stateTimestampMs,
        bool(getattr(state, "is_paused", False)),
        _connectStateInt(getattr(state, "duration", None)),
    )


def _playedMsForOutgoingTrack(self, observed) -> int:
    """How long the track being left was actually PLAYED.

    The old answer was wall-clock since it became current, which silently counts
    pause time: app.log 2026-07-30 10:14:59 recorded a track paused overnight as
    35,377,992ms - 154x its own length - and only
    LISTENER_DURATION_CORRUPTION_FACTOR stopped that landing in the database.
    The clamp hid the worse case: a track paused 30 seconds in and abandoned was
    rewritten to its full duration and counted as a complete listen, and
    time_played is what plays.is_skip is materialised from.

    The connect state carries the real playback position, in both modes (poll
    and push fill _state from the same shape), so the answer is "how far into
    the track it had got", capped at the track's own length. Wall-clock remains
    the fallback for a state that carries no position.

    Deliberately NOT also capped at how long we had been watching the track. A
    listener joins mid-track on every rebuild, and its lastPlayedAt is then the
    moment it FIRST SAW the track rather than the moment playback started - so
    that ceiling would throw away the position it joined at and turn full
    listens into skips on every reconnect. The trade is that seeking forward
    reads as progress, which is rarer and smaller than under-reporting every
    reconnect.

    Same position maths as Database.workers.listener.getNowPlaying, which has
    read these fields for the dashboard since the now-playing strip landed."""
    if observed is None:
        return max(0, int((time.time() - self.lastPlayedAt.timestamp()) * 1000))

    positionMs, stateTimestampMs, isPaused, durationMs = observed
    if isPaused:
        playedMs = positionMs
    else:
        # The state only updates on play/pause/seek/track change, so the live
        # position is its snapshot plus the time since that snapshot.
        playedMs = positionMs + max(0, int(time.time() * 1000) - stateTimestampMs)
    if durationMs > 0:
        playedMs = min(playedMs, durationMs)
    return max(0, int(playedMs))


def _applyStateToTracking(self, state, callback) -> None:
    """Turn an observed player state into a recorded play, if the track changed.

    Shared by BOTH modes: whatever push does differently, it must not be this.
    The callback contract (previous track's uri, its start time, its context,
    and the played ms since it started) is what _addToRecentlyPlayed and every
    downstream play row depend on.

    `self` is a RecentlyPlayedManager, taken explicitly rather than bound:
    these helpers sit at module level so they can be tested directly instead of
    through a closure."""
    if (state is None
            or getattr(state, "timestamp", None) is None
            or getattr(state, "track", None) is None
            or getattr(state.track, "uid", None) is None):
        return

    timestamp = int(state.timestamp) / 1000
    if self.lastPlayedUid != state.track.uid:
        if self.lastTrackUri is not None:
            # Measured from the LAST observation of the outgoing track, not
            # from this one - this state already describes its replacement.
            timePlayed = _playedMsForOutgoingTrack(self, getattr(self, "_lastObservedPlayback", None))
            callback(self.lastTrackUri, self.lastPlayedAtText, self.lastContextUri, timePlayed)
        self.lastTrackUri = state.track.uri
        self.lastPlayedAt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
        self.lastPlayedAtText = self.lastPlayedAt.isoformat().replace("+00:00", "Z")
        self.lastContextUri = state.context_uri
        self.lastPlayedUid = state.track.uid
    # Every observation refreshes it, not just changes: this is what the NEXT
    # track change measures against, and mid-track pauses/seeks are exactly the
    # updates that make it accurate.
    self._lastObservedPlayback = _observedPlayback(state)


def _applyPushedState(self, manager, callback) -> None:
    """Build a PlayerState from the cached dict and run track detection.

    Deep-copied for the same reason the state property is: spotapi's
    Track.from_dict REPLACES data["metadata"] in place, so handing it the cached
    _state would corrupt what getConnectPlayerState (Now Playing, the
    missed-track cross-check) reads next.

    Track detection sits INSIDE the guard: the callback resolves track
    metadata, so it can raise (track()'s retry ladder re-raising, a rate
    limit), and the push loop has no other containment - an escape here killed
    the thread with `run` still True, freezing _state so the account read as
    idle until the 6h stale hard-timeout rebuilt it. The poll loop guards its
    _applyStateToTracking call the same way (its catch-all would otherwise
    escalate a callback failure into a full reconnect). The failed play
    is retried, not dropped: lastPlayedUid only advances after the callback
    returns (see _applyStateToTracking).

    Nothing adopted yet is a no-op, not a fault, and the guard lives HERE
    rather than at the call sites: connect_device() can reply with no
    player_state at all (an account with no live Connect session), _adoptCluster
    leaves the caches untouched when it does, and spotapi's from_dict does
    `key in data` - so a None _state raised "argument of type 'NoneType' is not
    iterable". The seeding call guarded it and the periodic re-subscribe did
    not, which is why it surfaced as one warning every
    CONNECT_STATE_RESUBSCRIBE_SECONDS instead of at connect time."""
    cached = manager._state
    if not isinstance(cached, dict):
        return
    try:
        state = spotapi.status.PlayerState.from_dict(copy.deepcopy(cached))
        _applyStateToTracking(self, state, callback)
    except Exception as e:  # noqa: BLE001 - a malformed push or failing callback must not kill the loop
        logger.warning("[Spotify] Could not apply a pushed player state: %s", e)


def _subscribeConnectState(manager):
    """Re-register with connect-state. True on success, False on a real failure,
    None when the shared limiter refused a slot.

    None is NOT a failure - nothing was sent, and the pause it reports is one we
    are already applying. Counting it would let our own backoff window push this
    listener off the push channel entirely: the same conflation that turned one
    rate-limit event into one per listener (see SpotifyLocallyRateLimitedError)."""
    if not SPOTIFY_LIMITER.acquire(timeout=SPOTIFY_ACQUIRE_TIMEOUT_SECONDS):
        return None
    try:
        dump = manager.connect_device()
    except Exception as e:  # noqa: BLE001
        logger.warning("[Spotify] connect-state subscribe failed: %s", e)
        return False

    # The reply IS the current cluster - same shape a push carries - so adopting
    # it makes every subscribe double as a poll. That is what gives push mode a
    # floor: connect_device() itself never touches the caches (renew_state is
    # what normally does), so without this the loop would start with no state at
    # all, and a subscription that silently stopped delivering while the socket
    # stayed healthy would freeze _state indefinitely. Now it refreshes at least
    # every CONNECT_STATE_RESUBSCRIBE_SECONDS.
    if isinstance(dump, dict):
        _adoptCluster(manager, dump)
    return True


def _runPushLoop(self, callback) -> str:
    """Listen for pushed player states. Returns "stopped" when the loop is done,
    or "fallback" when the caller should hand back to polling.

    Falling back is always safe and never silent: polling is the known-good
    path, so any doubt about the push channel resolves that way rather than into
    missing plays."""
    manager = self.manager

    subscribed = None
    while self.run and subscribed is None:
        if getattr(manager, "_deliberate_close", False):
            return "stopped"
        subscribed = _subscribeConnectState(manager)   #< None = paused, keep waiting
    if not self.run:
        return "stopped"
    if not subscribed:
        logger.warning("[Spotify] Could not subscribe to connect-state pushes; polling instead")
        return "fallback"

    # _subscribeConnectState adopted the reply, so the channel starts seeded -
    # no separate poll needed to know what is playing right now. A reply that
    # carried no player_state leaves nothing to apply; _applyPushedState is the
    # one place that decides so.
    _applyPushedState(self, manager, callback)

    logger.info("[Spotify] Listening for connect-state pushes (polling disabled)")
    lastFrameAt = time.monotonic()
    lastSubscribeAt = lastFrameAt
    resubscribeFailures = 0
    # From here the listener's stale check must read "idle account" rather than
    # "dead session" out of an empty connect state - see pushChannelAliveAt.
    self.pushChannelAliveAt = lastFrameAt

    try:
        while self.run:
            if getattr(manager, "_deliberate_close", False):
                logger.info("[Spotify] Push loop exiting: websocket was closed deliberately")
                self.run = False
                return "stopped"

            packet = manager.get_packet(timeout=PUSH_RECV_TIMEOUT_SECONDS)
            now = time.monotonic()

            if packet is not None:
                # ANY frame proves the socket is alive - keepalive pongs included,
                # which is what makes the watchdog below usable at all.
                lastFrameAt = now
                self.pushChannelAliveAt = now
                cluster = _clusterFromPacket(packet)
                if cluster is not None and _adoptCluster(manager, cluster):
                    _applyPushedState(self, manager, callback)

            if now - lastFrameAt >= PUSH_FRAME_SILENCE_FALLBACK_SECONDS:
                logger.warning(
                    "[Spotify] No websocket frame of any kind for %ds (not even a keepalive "
                    "pong) - the push channel looks dead, returning to polling",
                    PUSH_FRAME_SILENCE_FALLBACK_SECONDS)
                return "fallback"

            if now - lastSubscribeAt >= CONNECT_STATE_RESUBSCRIBE_SECONDS:
                outcome = _subscribeConnectState(manager)
                if outcome is None:
                    continue            #< paused, not failed; retry on a later pass
                lastSubscribeAt = now
                if outcome:
                    resubscribeFailures = 0
                    # The refreshed state runs through track detection too, so a
                    # change missed while pushes were silently dead is recovered
                    # here rather than lost. Idempotent: an unchanged track uid is
                    # a no-op, so this can never double-record.
                    _applyPushedState(self, manager, callback)
                else:
                    resubscribeFailures += 1
                    if resubscribeFailures >= PUSH_RESUBSCRIBE_MAX_FAILURES:
                        logger.warning(
                            "[Spotify] connect-state re-subscribe failed %d times; "
                            "returning to polling", resubscribeFailures)
                        return "fallback"

        return "stopped"
    finally:
        # Every exit, including the fallback and an escaping exception: once
        # this loop is not the one feeding _state, poll mode's "an empty
        # connect state means the tick is dead" reasoning is true again.
        self.pushChannelAliveAt = None


def _runPollLoop(self, callback, refreshInterval=3):
    consecutiveStateFailures = 0
    while self.run:
        if getattr(self.manager, "_deliberate_close", False):
            # Listener.stop()/signalStop() closed this websocket on
            # purpose - exit instead of hammering a connection that is
            # gone for good (a leftover loop kept spamming reconnect
            # errors every few seconds through the 2026-07-17 shutdown).
            logger.info("[Spotify] Player-state loop exiting: websocket was closed deliberately")
            self.run = False
            return
        # Take a slot from the process-wide Spotify budget BEFORE
        # touching the network. This loop is what that budget exists
        # for: reading manager.state PUTs to the connect-state
        # endpoint, so at refreshInterval=6 it is ~10 requests a
        # minute PER USER - dwarfing every other Spotify call in the
        # process, and until now the one thing a rate-limit backoff
        # could not pause (the listener's own backoff sleeps a
        # different thread entirely; see Database/Listeners/
        # spotifyListener.py's startListener).
        if not SPOTIFY_LIMITER.acquire(timeout=SPOTIFY_ACQUIRE_TIMEOUT_SECONDS):
            # Held back locally. Loop rather than sleep, so the stop
            # flags above are re-checked every acquire timeout instead
            # of after a whole penalty window, and leave
            # consecutiveStateFailures alone: nothing was asked of
            # Spotify, so nothing failed - counting it would escalate
            # a backoff into a websocket reconnect, which is more
            # traffic, not less.
            continue
        try:
            try:
                state = self.manager.state
            except ValueError as stateError:
                consecutiveStateFailures += 1
                if consecutiveStateFailures < STATE_FAILURE_RECONNECT_THRESHOLD:
                    logger.warning(
                        "[Spotify] Player state unavailable (%d/%d), retrying: %s",
                        consecutiveStateFailures, STATE_FAILURE_RECONNECT_THRESHOLD, stateError,
                    )
                else:
                    logger.error(
                        "[Spotify] Player state unavailable %d times in a row, reconnecting websocket: %s",
                        consecutiveStateFailures, stateError,
                    )
                    consecutiveStateFailures = 0
                    # A whole streak of failed connect-state PUTs is
                    # the throttling signal this endpoint gives us -
                    # there is no status code to read here, spotapi
                    # collapses it to ValueError. Applied at the
                    # escalation threshold, not per failure, so a
                    # single blip doesn't pause every user.
                    SPOTIFY_LIMITER.applyBackoff(SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS,
                                                 reason=ENDPOINT_CONNECT_STATE)
                    try:
                        self.manager.reconnect()
                    except Exception as reconnect_err:
                        if _isSessionClosedError(reconnect_err):
                            logger.error(
                                "[Spotify] Player-state loop exiting: the HTTP session is "
                                "closed and cannot be revived: %s", reconnect_err,
                            )
                            self.run = False
                            return
                        logger.error("[Spotify] Websocket reconnect failed; will keep retrying: %s", reconnect_err, exc_info=True)
                time.sleep(refreshInterval)
                continue

            consecutiveStateFailures = 0
            # Shared with the push path - a state that can't be read
            # (inactive device, no track) is a no-op there too, so the
            # sleep below runs either way, exactly as it used to.
            #
            # Guarded separately from the catch-all below for the same reason
            # _applyPushedState guards it: the callback resolves track metadata
            # and can raise (track()'s retry ladder re-raising, a rate limit),
            # and that is a metadata failure, not a transport failure - the
            # state was just read fine. Letting it reach the catch-all rebuilt
            # the websocket AND the session every poll tick for as long as the
            # callback kept failing; during a TOTP rotation each of those
            # reconnects failed its own get_session() and bumped the
            # auth-failure counter. The failed play is retried, not dropped:
            # lastPlayedUid only advances after the callback returns.
            try:
                _applyStateToTracking(self, state, callback)
            except Exception as applyError:  # noqa: BLE001 - a failing play callback must not escalate to a reconnect
                logger.warning("[Spotify] Could not record the observed play state: %s", applyError)
            time.sleep(refreshInterval)
        except Exception as e:
            logger.error("[Spotify] Error in Recently Played: %s", e, exc_info=True)
            time.sleep(UPDATE_LOOP_ERROR_SLEEP_SECONDS)
            try:
                self.manager.reconnect()
            except Exception as reconnect_err:
                if _isSessionClosedError(reconnect_err):
                    logger.error(
                        "[Spotify] Player-state loop exiting: the HTTP session is "
                        "closed and cannot be revived: %s", reconnect_err,
                    )
                    self.run = False
                    return
                logger.error("[Spotify] Websocket reconnect failed; will keep retrying: %s", reconnect_err, exc_info=True)


class RecentlyPlayedManager:
    """Owns the PlayerStatus (websocket + connect-state caches) for one session
    and the background thread that watches it.

    The attribute set (manager / run / thread / pushChannelAliveAt) is a
    contract: spotifyListener's shutdown path reaches in via getattr chains -
    sp.lastPlayedManager.manager._deliberate_close, .run, .thread - to signal
    and join without importing this module, and its stale check reads
    pushChannelAliveAt the same way."""

    def __init__(self, login):
        self.login = login
        self.manager = spotapi.status.PlayerStatus(login)
        self.thread = None
        self.run = False
        # time.monotonic() of the last proof the push channel was alive, or
        # None whenever this listener is not on one (poll mode, or a push loop
        # that has exited). Only _runPushLoop writes it; the listener's stale
        # check reads it to tell an idle account apart from a dead session,
        # which in push mode an empty connect state cannot answer on its own.
        self.pushChannelAliveAt = None
        # Track-change detection state, read/written by _applyStateToTracking.
        self.lastPlayedUid = ""     #< uid of the currently-observed track ("" = none yet)
        self.lastTrackUri = None
        self.lastPlayedAt = None
        self.lastPlayedAtText = None
        self.lastContextUri = None
        self._lastObservedPlayback = None

    def updateLoop(self, callback, refreshInterval=3):
        if _pushListenerEnabled():
            if _runPushLoop(self, callback) == "stopped":
                return
            # One-way: once the push channel has disappointed us this
            # listener stays on polling until it is rebuilt. Flapping
            # between the two would be harder to reason about than either.
            logger.warning("[Spotify] Falling back to connect-state polling")
        _runPollLoop(self, callback, refreshInterval)

    def start(self, callback, refreshInterval):
        self.run = True
        self.thread = threading.Thread(
            target=self.updateLoop, args=(callback, refreshInterval), daemon=True)
        self.thread.start()

    # Deliberately NO stop() method. Shutdown goes through spotifyListener's
    # signalStop()/stop(), which set `run` and join `thread` with a bounded
    # timeout via the attribute contract above. A convenience stop() here
    # carried an unbounded join - against a loop that sleeps up to 10s and
    # retries reconnects, that is the 2026-07-17 shutdown hang waiting for its
    # first caller.
