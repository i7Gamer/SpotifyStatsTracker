# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import datetime
import logging
import os
import re
import signal
import threading
import time
import websockets.sync.client
import websockets.exceptions
import spotapi.exceptions
import spotapi.status
import spotapi.websocket

from Database.rate_limit import (
    SPOTIFY_LIMITER, SPOTIFY_ACQUIRE_TIMEOUT_SECONDS,
    SPOTIFY_TRACK_ACQUIRE_TIMEOUT_SECONDS, SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS,
    SpotifyLocallyRateLimitedError,
)

logger = logging.getLogger(__name__)

# Endpoint labels for the shared limiter's backoff reason - short enough for
# the /admin card, specific enough to say WHICH Spotify surface pushed back.
ENDPOINT_ACCOUNT_PROFILE = "account-settings/profile"
ENDPOINT_ACCOUNT_PLAN = "account/plan"
ENDPOINT_CONNECT_STATE = "connect-state"
ENDPOINT_TRACK_INFO = "track metadata"

# HTTP statuses that are Spotify explicitly throttling rather than failing.
# 503 is included because Spotify's edge answers sustained pressure with it
# before it ever reaches a 429.
SPOTIFY_THROTTLE_STATUS_CODES = frozenset({429, 503})


def _looksThrottled(statusCode, response) -> bool:
    """Whether a reply is Spotify pushing back, as opposed to an ordinary
    failure. Two signals, both observed live:

    - an explicit throttle status, or
    - ANY HTML body. These endpoints only ever answer JSON, so a web page
      means the request was diverted to Spotify's bot-check/error fallback -
      which arrives as HTTP 200 with 46 KB of markup titled "Oh nein!", i.e.
      invisible to a status-code check alone.
    """
    if statusCode in SPOTIFY_THROTTLE_STATUS_CODES:
        return True
    if response is None:
        return False
    try:
        return _looksLikeHtml(str(response))
    except Exception:
        # Never let the throttle heuristic raise: every caller is already on
        # an error path, where this failing would replace a logged warning
        # (and a raised UserError) with an unrelated exception.
        return False


def _backoffIfThrottled(statusCode, response, endpoint: str) -> bool:
    """Pause EVERY Spotify request in this process if `response` shows Spotify
    pushing back. Returns whether a backoff was applied.

    Process-wide is the whole point: the traffic that trips Spotify's limits
    is the sum of every user's listener, and until this existed the only
    reaction to a rate limit was one listener's poll thread sleeping - which
    paused about 0.3 requests a minute while the other users' connect-state
    polls carried on at ~10 a minute each."""
    if not _looksThrottled(statusCode, response):
        return False
    SPOTIFY_LIMITER.applyBackoff(SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS, reason=endpoint)
    logger.warning(
        "Spotify pushed back on %s (status=%s) - holding every Spotify request "
        "in this process for %ds", endpoint, statusCode, SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS)
    return True

# 1. Monkey patch websockets.sync.client.connect to disable the built-in keepalive ping
# that causes ConnectionClosedError during CPU blockages / imports.
original_connect = websockets.sync.client.connect

def patched_connect(*args, **kwargs):
    # Disable built-in keepalive ping by default
    kwargs.setdefault("ping_interval", None)
    kwargs.setdefault("ping_timeout", None)
    return original_connect(*args, **kwargs)

websockets.sync.client.connect = patched_connect

# Also patch it in spotapi.websocket in case it was already imported
if hasattr(spotapi.websocket, "connect"):
    spotapi.websocket.connect = patched_connect


# 2. Add a robust reconnect method to spotapi.status.PlayerStatus.
# This prevents AttributeError: 'PlayerStatus' object has no attribute 'reconnect'
# when the websocket drops and LastPlayedManger attempts to reconnect.
def player_status_reconnect(self):
    logger.info("Reconnecting PlayerStatus websocket...")

    # Close old connection if possible
    try:
        if hasattr(self, "ws") and self.ws:
            self.ws.close()
    except Exception:  # noqa: S110 - the old socket is being replaced; a failure to close
        pass           #  one that is already dead must not block the reconnect

    # Renew session and client token
    try:
        self.base.get_session()
        self.base.get_client_token()
    except Exception as e:
        logger.warning("Failed to renew session: %s", e)
    
    # Establish new websocket connection using the patched connect function
    uri = f"wss://dealer.spotify.com/?access_token={self.base.access_token}"
    self.ws = websockets.sync.client.connect(
        uri,
        user_agent_header="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    
    # Update connection ID
    self.connection_id = self.get_init_packet()
    
    # Register and connect device
    self.register_device()
    self.connect_device()
    
    # Restart the keep_alive thread if it is dead
    if hasattr(self, "keep_alive_thread") and not self.keep_alive_thread.is_alive():
        self.keep_alive_thread = threading.Thread(target=self.keep_alive, daemon=True)
        self.keep_alive_thread.start()

    logger.info("PlayerStatus websocket reconnected successfully.")

# Inject the reconnect method into PlayerStatus class
spotapi.status.PlayerStatus.reconnect = player_status_reconnect


# Patch renew_state to:
# 1. Avoid KeyError: 'devices' or KeyError: 'player_state' on API failures.
# 2. Note: we do NOT deepcopy here - see the state/saved_state property patches
#    below for where the mutation is actually prevented.
def player_status_renew_state(self):
    try:
        self._device_dump = self.connect_device()
        if isinstance(self._device_dump, dict):
            self._state = self._device_dump.get("player_state")
            self._devices = self._device_dump.get("devices")
        else:
            self._state = None
            self._devices = None
    except Exception as e:
        # spotapi's ParentException keeps the HTTP detail (e.g. the status code)
        # in .error, not in str(e) - surface it or the log can't distinguish
        # throttling from a real outage.
        errorDetail = getattr(e, "error", None)
        if errorDetail:
            logger.warning("Error renewing player state: %s (%s)", e, errorDetail)
        else:
            logger.warning("Error renewing player state: %s", e)
        self._state = None
        self._devices = None

spotapi.status.PlayerStatus.renew_state = player_status_renew_state


# Patch the `state` and `saved_state` properties to pass a shallow copy of
# _state to PlayerState.from_dict, preventing Track.from_dict from corrupting
# the cached _state dict.
#
# Root cause: spotapi's Track.from_dict() mutates its input dict in-place:
#   data["metadata"] = Metadata.from_dict(metadata)
# PlayerState.from_dict(_state) passes _state["track"] directly to
# Track.from_dict, so without a copy, _state["track"]["metadata"] becomes a
# Metadata dataclass. The next call to getConnectPlayerState() then reads
# _state["track"]["metadata"] and tries to call .get("title") on a Metadata
# object -> AttributeError.
#
# copy.copy(_state) is a shallow copy of the outer dict; that's enough because
# Track.from_dict only replaces the "metadata" key on the track dict (a
# nested value), which means we also need to copy the nested "track" dict.
# We use copy.deepcopy for correctness, but only on the _state snapshot -
# this is called once per 3-second poll, so the overhead is negligible.
def _player_status_state_property(self):
    """Gets the current state of the player (patched to prevent _state mutation)."""
    self.renew_state()
    if self._state is None:
        raise ValueError("Could not get player state")
    return spotapi.status.PlayerState.from_dict(copy.deepcopy(self._state))


def _player_status_saved_state_property(self):
    """Gets the last saved state of the player (patched to prevent _state mutation)."""
    if self._state is None:
        self.renew_state()
    if self._state is None:
        raise ValueError("Could not get player state")
    return spotapi.status.PlayerState.from_dict(copy.deepcopy(self._state))


spotapi.status.PlayerStatus.state = property(_player_status_state_property)
spotapi.status.PlayerStatus.saved_state = property(_player_status_saved_state_property)


# 3. Prevent WebsocketStreamer.__init__ from hijacking the process's SIGINT handler.
# It unconditionally does `signal.signal(signal.SIGINT, self.handle_interrupt)`, whose
# handler just does `self.ws.close(); exit(0)`. That overwrites Flask/Werkzeug's normal
# Ctrl+C handling, and since it can fire while a background listener thread (see
# LastPlayed.py's updateLoop) is mid-request, it leads to noisy/broken shutdowns instead
# of a clean KeyboardInterrupt. Restore whatever SIGINT handler was registered before
# spotapi's own __init__ ran.
original_websocket_streamer_init = spotapi.websocket.WebsocketStreamer.__init__

def patched_websocket_streamer_init(self, *args, **kwargs):
    previousSigintHandler = signal.getsignal(signal.SIGINT)
    original_websocket_streamer_init(self, *args, **kwargs)
    try:
        signal.signal(signal.SIGINT, previousSigintHandler)
    except ValueError:
        pass  # signal.signal only works in main thread; silently skip if in worker thread

spotapi.websocket.WebsocketStreamer.__init__ = patched_websocket_streamer_init


# 4. Patch WebsocketStreamer.keep_alive to handle websockets.exceptions.ConnectionClosed.
# The original keep_alive only catches ConnectionError and KeyboardInterrupt, so a
# ConnectionClosed crashed the ping thread with a full traceback - after which pings
# silently stopped and the feed stayed frozen until the listener's 30-minute stale-feed
# detector rebuilt the session. Instead: a deliberate close (spotifyListener.stop() sets
# _deliberate_close before closing the ws, and a clean close handshake raises
# ConnectionClosedOK) ends the loop quietly, while an unexpected drop logs one concise
# line (no traceback) and retries self.reconnect() (injected in patch 2 above) so the
# feed recovers within a ping interval instead of half an hour.
WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES = 3
WS_KEEP_ALIVE_RECONNECT_BACKOFF_SECONDS = 10

original_keep_alive = spotapi.websocket.WebsocketStreamer.keep_alive

def patched_keep_alive(self):
    consecutiveFailures = 0
    while True:
        try:
            original_keep_alive(self)
            return  #< original loop exited on its own (ConnectionError/KeyboardInterrupt)
        except websockets.exceptions.ConnectionClosedOK:
            logger.info("Websocket closed cleanly, stopping keep-alive pings.")
            return
        except websockets.exceptions.ConnectionClosed as e:
            if getattr(self, "_deliberate_close", False):
                logger.info("Websocket closed on shutdown, stopping keep-alive pings.")
                return
            reconnect = getattr(self, "reconnect", None)
            if reconnect is None:
                logger.warning("Websocket connection lost (%s) and no reconnect() available, stopping keep-alive pings.", e)
                return
            logger.warning("Websocket connection lost (%s), attempting reconnect...", e)
            try:
                reconnect()
                consecutiveFailures = 0
            except Exception as reconnectError:
                consecutiveFailures += 1
                logger.warning(
                    "Websocket reconnect failed (%d/%d): %s",
                    consecutiveFailures, WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES, reconnectError,
                )
                if consecutiveFailures >= WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES:
                    logger.error(
                        "Giving up websocket reconnects after %d attempts; the stale-feed detector will rebuild the session.",
                        WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES,
                    )
                    return
                time.sleep(WS_KEEP_ALIVE_RECONNECT_BACKOFF_SECONDS)

spotapi.websocket.WebsocketStreamer.keep_alive = patched_keep_alive


# 5. Patch WebsocketStreamer.get_packet so a push channel can actually be read.
#
# spotapi's version holds self.rlock across an UNBOUNDED self.ws.recv(), and
# keep_alive above needs that same lock to send its 60-second ping. On a quiet
# account the receive loop parks inside recv() holding the lock, the ping never
# goes out, and Spotify drops the connection on its idle timeout - recovered by
# a full re-login, the most bot-detection-sensitive path this app has. That is
# almost certainly why LastPlayedManger polls connect-state every few seconds
# instead of listening, and it means adopting spotapi's EventManager as-is
# would be worse than the polling it would replace.
#
# Its `except Exception` is the other half: a plain timeout - the NORMAL state
# of a push channel, which measured 3.75 h between updates overnight - was
# treated as a reason to reconnect, forever, on a 1-second sleep.
#
# Nothing in this application calls get_packet yet; PlayerStatus only sends
# pings and never reads. This lands on its own because it is a prerequisite for
# reading the dealer pushes (eventDrivenConnectStatePlan.md Phase 2) and is
# strictly safer than what it replaces even if that never happens.
WS_RECV_TIMEOUT_SECONDS = 1.0    #< ceiling on how long rlock may be held, so the ping thread
                                  #  waits at most this long for its turn
WS_RECV_MAX_RECONNECT_FAILURES = 3  #< then stand down and let the stale-feed detector rebuild


def patched_get_packet(self, timeout: float = WS_RECV_TIMEOUT_SECONDS):
    """The next websocket frame as a dict, or None.

    None means "nothing to act on, call again": the wait elapsed, the socket is
    gone, shutdown was requested, or a frame arrived that wasn't a JSON object.
    Callers loop, which also gives them a place to check their own stop flags -
    spotapi's EventManager._listen already skips a None event, so its contract
    is unchanged."""
    if getattr(self, "_deliberate_close", False):
        return None

    try:
        with self.rlock:
            ws = self.ws        #< re-read under the lock: a concurrent reconnect swaps it
            if ws is None:
                return None
            raw = ws.recv(timeout=timeout)
    except TimeoutError:
        return None             #< silence is the normal state of a push channel, not a fault
    except websockets.exceptions.ConnectionClosedOK:
        logger.info("Websocket closed cleanly; no more packets to read.")
        return None
    except websockets.exceptions.ConnectionClosed as e:
        _reconnectAfterDroppedPacket(self, e)
        return None
    except Exception as e:  # noqa: BLE001 - anything else is diagnosed, never silently retried
        logger.warning("Unexpected error reading websocket packet: %s: %s",
                       type(e).__name__, e)
        return None

    try:
        packet = json.loads(raw)
    except Exception:
        logger.warning("Dropping unparsable websocket frame (%d bytes)", len(raw))
        return None
    if not isinstance(packet, dict):
        # dict(json.loads(...)) is what spotapi did, so a JSON array or scalar
        # raised straight into its reconnect-on-anything handler.
        logger.warning("Dropping non-object websocket frame of type %s", type(packet).__name__)
        return None

    self.ws_dump = packet
    return packet


def _reconnectAfterDroppedPacket(self, closeError) -> None:
    """Bounded recovery for a dropped receive socket.

    Mirrors patched_keep_alive's shape - one concise line per attempt, a hard
    ceiling, and an immediate stop once the underlying HTTP session is closed
    for good - rather than spotapi's unbounded `while True` + sleep(1), which
    turns an unreachable endpoint into a reconnect storm against the same
    endpoint that just refused us."""
    if getattr(self, "_deliberate_close", False):
        return
    reconnect = getattr(self, "reconnect", None)
    if reconnect is None:
        logger.warning("Websocket dropped (%s) and no reconnect() available.", closeError)
        return

    failures = getattr(self, "_recvReconnectFailures", 0)
    if failures >= WS_RECV_MAX_RECONNECT_FAILURES:
        return  #< already gave up and said so; stay quiet rather than log per packet

    logger.warning("Websocket dropped while reading (%s), attempting reconnect...", closeError)
    try:
        reconnect()
        _setRecvReconnectFailures(self, 0)
    except Exception as reconnectError:
        if _isSessionClosedError(reconnectError):
            logger.error("Websocket receive giving up: the HTTP session is closed and "
                         "cannot be revived: %s", reconnectError)
            _setRecvReconnectFailures(self, WS_RECV_MAX_RECONNECT_FAILURES)
            return
        failures += 1
        _setRecvReconnectFailures(self, failures)
        logger.warning("Websocket reconnect failed (%d/%d): %s",
                       failures, WS_RECV_MAX_RECONNECT_FAILURES, reconnectError)
        if failures >= WS_RECV_MAX_RECONNECT_FAILURES:
            logger.error(
                "Giving up websocket receive reconnects after %d attempts; the "
                "stale-feed detector will rebuild the session.",
                WS_RECV_MAX_RECONNECT_FAILURES)


def _setRecvReconnectFailures(self, count: int) -> None:
    """Remember the failure count on the streamer, tolerating an instance that
    won't take new attributes.

    WebsocketStreamer declares __slots__ without this name. Every object this
    app actually builds is a PlayerStatus, which defines no __slots__ of its own
    and so still carries a __dict__ - the same assumption signalStop already
    makes for _deliberate_close, with the same guard. If a future/bare instance
    ever refuses the write, the ceiling below stops applying, so say so once
    rather than silently reverting to spotapi's unbounded retries."""
    try:
        self._recvReconnectFailures = count
    except AttributeError:
        logger.warning(
            "Cannot track websocket receive-reconnect failures on a %s "
            "(__slots__); the %d-attempt ceiling will not apply.",
            type(self).__name__, WS_RECV_MAX_RECONNECT_FAILURES)


spotapi.websocket.WebsocketStreamer.get_packet = patched_get_packet


import json
import sys


try:
    from Database.db import RESTRICTED_FALLBACK_REASON, UNKNOWN_TRACK_NAME, UNKNOWN_ALBUM_NAME
except ModuleNotFoundError:
    from db import RESTRICTED_FALLBACK_REASON, UNKNOWN_TRACK_NAME, UNKNOWN_ALBUM_NAME

# tracks.availability_reason value for a track Spotify wouldn't describe, sitting
# alongside its own COUNTRY_RESTRICTED/PAYWALL_CONTENT reasons - so the UI's
# "May be unavailable" badge covers this case with no template change.
TRACK_INFO_UNAVAILABLE_REASON = "TRACK_INFO_UNAVAILABLE"

# How many extra attempts an incomplete song_info response gets before the
# caller degrades to a fallback record. Kept low and on a SHORT FIXED delay
# rather than the transient ladder's 1/2/4s exponential backoff: this runs
# inside the poll loop's callback, so during a burst every affected track pays
# the wait. One extra attempt is enough to ride out the sub-second gap that
# produced the observed cluster without making a genuinely-gone track slow.
INCOMPLETE_TRACK_INFO_RETRIES = 1
INCOMPLETE_TRACK_INFO_RETRY_DELAY_SECONDS = 2


class IncompleteTrackInfoError(Exception):
    """spotapi answered, but not with a usable track.

    Three degraded shapes were seen in Database/Data/app.log on 2026-07-16, all
    of which used to escape as raw TypeErrors/KeyErrors:

      {"data": None}                     -> TypeError on ["trackUnion"]
      {"data": {"trackUnion": None}}     -> TypeError downstream
      a trackUnion dict with no "uri"    -> KeyError: 'uri', raised deep inside
                                            SpotipyFree/Formatter.py, where the
                                            track id is no longer in scope

    Distinguishing this from a transport failure matters because the two need
    different budgets, not because one is unrecoverable: an incomplete response
    is usually a symptom of a degraded session rather than a fact about the
    track. All 11 of these in 11 days of app.log fall inside one 4m47s window
    (2026-07-16 12:59:44-13:04:31), alongside 14 session/websocket failures - so
    they get a short bounded retry, and only then does the caller degrade rather
    than lose the play."""


def _extractTrackUnion(payload, trackId: str) -> dict:
    """The trackUnion out of a song_info payload, or IncompleteTrackInfoError.

    Validates the shape rather than just the nullness of each hop: a trackUnion
    can be a perfectly good dict that happens to lack "uri", which no null check
    catches and which SpotipyFree's formatter then dies on."""
    data = payload.get("data") if isinstance(payload, dict) else None
    trackUnion = data.get("trackUnion") if isinstance(data, dict) else None
    if not isinstance(trackUnion, dict):
        raise IncompleteTrackInfoError(
            f"song_info returned no track data for {trackId} "
            f"(data={type(data).__name__}, trackUnion={type(trackUnion).__name__})")
    if not trackUnion.get("uri"):
        raise IncompleteTrackInfoError(
            f"song_info returned a track without a uri for {trackId}")
    return trackUnion


def _fallbackTrackRecord(trackId: str) -> dict:
    """A minimal stand-in for a track Spotify wouldn't describe, in the shape
    SpotifyFormatter.formatTrack would have produced.

    Invents no *facts*: the duration stays 0 and no artists are claimed, because
    a made-up number would read as real metadata downstream. The title is the
    shared UNKNOWN_TRACK_NAME placeholder rather than "" - a blank name rendered
    as an empty row in every list the track appeared in, and it costs nothing,
    since upsertTrack replaces a fallback row's name unconditionally once real
    metadata arrives.

    The id IS real, so the Spotify link is real too - only fabricated ids carry
    an empty url in this codebase.

    The album's ID is per track (album_<trackId>, the same convention the
    importer's fallbacks use), because a single fabricated album id would collect
    every undescribable track from every user into one page of unrelated songs.
    Its NAME is the shared UNKNOWN_ALBUM_NAME placeholder for the same reason the
    title is: it used to pass "", and _formatAlbum's own "Unknown album" default
    never applied because the key was present, so albums.name was stored empty and
    rendered blank on the detail page and in every album link."""
    return {
        "name": UNKNOWN_TRACK_NAME,
        "track_id": trackId,
        "id": trackId,
        "disc_number": 0,
        "track_number": 0,
        "duration_ms": 0,
        "artists": [],
        "album": {"id": f"album_{trackId}", "name": UNKNOWN_ALBUM_NAME, "images": [],
                  "external_urls": {"spotify": ""}, "total_tracks": 0},
        "explicit": False,
        "external_urls": {"spotify": f"https://open.spotify.com/track/{trackId}"},
        "popularity": 0,
        "type": "track",
        "external_ids": {"isrc": ""},
        "playability": {"playable": False, "reason": TRACK_INFO_UNAVAILABLE_REASON},
        "created_reason": RESTRICTED_FALLBACK_REASON,
    }


def _get_track_info_with_retry(trackId: str, max_retries: int = 3):
    """Fetch track info from spotapi with retry logic for transient failures.

    Args:
        trackId: Spotify track ID
        max_retries: Maximum number of retry attempts

    Returns:
        Validated trackUnion dict from spotapi.Public.song_info()["data"]["trackUnion"]

    Raises:
        IncompleteTrackInfoError: If Spotify kept answering without a usable track
        Exception: If all retries fail
    """
    # Two failure modes, two separate budgets. An incomplete response early in a
    # fetch must not eat the transient ladder's attempts, or a Spotify blip would
    # silently shorten the recovery window for an unrelated rate limit.
    incompleteAttempts = 0
    attempt = 0
    while attempt < max_retries:
        try:
            # Waits out a whole penalty window rather than the short polling
            # timeout the loops use - see SPOTIFY_TRACK_ACQUIRE_TIMEOUT_SECONDS
            # for why giving up here is the expensive option.
            if not SPOTIFY_LIMITER.acquire(timeout=SPOTIFY_TRACK_ACQUIRE_TIMEOUT_SECONDS):
                raise SpotifyLocallyRateLimitedError(
                    f"Spotify rate limit backoff in progress - skipped {ENDPOINT_TRACK_INFO} for {trackId}")
            return _extractTrackUnion(spotapi.Public.song_info(trackId), trackId)
        except IncompleteTrackInfoError as e:
            if incompleteAttempts >= INCOMPLETE_TRACK_INFO_RETRIES:
                raise
            incompleteAttempts += 1
            logger.debug(
                "Incomplete track info for %s (attempt %d/%d), retrying in %ds: %s",
                trackId, incompleteAttempts, INCOMPLETE_TRACK_INFO_RETRIES + 1,
                INCOMPLETE_TRACK_INFO_RETRY_DELAY_SECONDS, e,
            )
            time.sleep(INCOMPLETE_TRACK_INFO_RETRY_DELAY_SECONDS)
            continue  #< deliberately does not advance `attempt`
        except Exception as e:
            error_str = str(e).lower()
            # Our own limiter refusing a slot: nothing was sent, so this is the
            # most transient failure there is - matched by type rather than by
            # message, like SongError below. Tested FIRST, and excluded from
            # is_rate_limit below, because its message says "rate limit" too:
            # counting it as Spotify's would re-arm the very window that
            # refused this call (see SpotifyLocallyRateLimitedError).
            is_locally_paused = isinstance(e, SpotifyLocallyRateLimitedError)
            is_rate_limit = not is_locally_paused and (
                "429" in error_str or ("rate" in error_str and "limit" in error_str))
            is_session_error = "could not get session" in error_str or "session" in error_str
            # spotapi raises SongError from exactly one place in
            # Song.get_track_info: `if resp.fail`, i.e. the HTTP request itself
            # failed. That is a transport blip - the class this ladder exists
            # for - but it says neither "rate limit" nor "session", so the
            # substring tests above missed it and it was re-raised on the FIRST
            # attempt. That propagates through SpotipyFree's
            # _addToRecentlyPlayed into the poll loop's catch-all and drops the
            # whole iteration, losing a play that really happened (5 of the 11
            # such losses in 11 days of app.log). Matched by type, not by
            # message: the message is spotapi's to change.
            is_failed_request = isinstance(e, spotapi.exceptions.SongError)

            # Only retry on transient errors (rate limit, session issues, a
            # failed request), not on real 404s
            if not (is_rate_limit or is_session_error or is_failed_request or is_locally_paused):
                raise

            if is_rate_limit:
                # Spotify said so explicitly: hold the whole process, not just
                # this call's private 1/2/4s ladder.
                SPOTIFY_LIMITER.applyBackoff(SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS,
                                             reason=ENDPOINT_TRACK_INFO)

            if attempt < max_retries - 1:
                backoff_secs = 2 ** attempt  # 1, 2, 4 seconds
                logger.warning("Track fetch failed (attempt %d/%d), backing off %ds: %s", attempt + 1, max_retries, backoff_secs, e)
                time.sleep(backoff_secs)
                attempt += 1
            else:
                logger.warning("Track fetch failed after %d attempts: %s", max_retries, e)
                raise


def patch_spotipy_free() -> bool:
    """Patch SpotipyFree.Spotify to store email on initialization and use it during
    login, instead of always hardcoding the first session in the cookies file.
    Also patches Spotify.track() to fetch metadata through spotapi.Public's locked
    client pool instead of spotapi.Song()'s process-wide shared default client.

    This is called automatically below at import time, but it's also exposed as a
    plain function (rather than only running once as module-level code) so callers
    can re-invoke it deliberately - e.g. a test module that needs the real
    SpotipyFree.Spotify patched can call this itself instead of depending on which
    other test module happened to import Database.patches first. Module-level code
    only ever runs once per process, so if it first ran while some other test's
    sys.modules["SpotipyFree"] mock was still in place, the real module would never
    get patched for the rest of the process without a way to retry.

    Returns True if the patch was applied, False if SpotipyFree is currently mocked
    or not installed.
    """
    # Skip if SpotipyFree is currently a mock rather than the real module.
    if "SpotipyFree" in sys.modules:
        sf = sys.modules["SpotipyFree"]
        if sf.__class__.__name__ in ("MagicMock", "Mock"):
            return False

    try:
        import SpotipyFree

        original_spotify_init = SpotipyFree.Spotify.__init__

        def patched_spotify_init(self, *args, **kwargs):
            # Retrieve email from args (4th argument, index 3 in args) or kwargs
            email = kwargs.get("email", None)
            if email is None and len(args) >= 4:
                email = args[3]
            self.email = email
            original_spotify_init(self, *args, **kwargs)

        SpotipyFree.Spotify.__init__ = patched_spotify_init

        def patched_spotify_login(self, cookiesFile=None):
            if cookiesFile is None:
                cookiesFile = SpotipyFree.getCookiesFile()
            try:
                # spotapi.Config's `client` field defaults via `field(default=TLSClient(...))`
                # rather than `field(default_factory=...)` - dataclasses only reject known
                # mutable defaults (list/dict/set), so that TLSClient instance is built once
                # at import time and silently shared as the default for every Config() call
                # that doesn't pass client= explicitly. Since Login stores cookies directly
                # on cfg.client (a curl_cffi Session), every user's Login object was sharing
                # one process-wide cookie jar - concurrent logins/reconnects would clobber
                # each other's session cookies, causing current_user() to return whichever
                # user's cookies happened to be in the jar at request time (the cross-user
                # contamination bug). Passing a fresh TLSClient per login isolates each
                # user's cookies, mirroring the fix already applied to spotapi.Song()'s
                # identical shared-default footgun below (patched_spotify_track).
                cfg = spotapi.Config(
                    logger=spotapi.Logger(),
                    client=spotapi.TLSClient("chrome120", "", auto_retries=3),
                )
                saver = spotapi.saver.JSONSaver(cookiesFile)
                try:
                    with open(cookiesFile, "r") as f:
                        sessions = json.load(f)

                    identifier = None
                    if hasattr(self, "email") and self.email:
                        for s in sessions:
                            if s.get("identifier") == self.email:
                                identifier = s["identifier"]
                                break

                    if not identifier and sessions:
                        identifier = sessions[0]["identifier"]
                except Exception as e:
                    logger.error("Error loading cookies file: %s", e)
                    return False

                self.user_auth = spotapi.Login.from_saver(saver, cfg, identifier)
            except Exception as e:
                logger.error("Failed to login user %s: %s", identifier if 'identifier' in locals() else 'unknown', e)
                return False
            return True

        SpotipyFree.Spotify.login = patched_spotify_login

        # spotapi.Song() (used by the original Spotify.track()) defaults its
        # `client` argument to a single TLSClient instance shared by every Song
        # created in the process (spotapi/song.py's `client: TLSClient =
        # TLSClient(...)` default is evaluated once, at import time). Every
        # spotapi.Song() construction re-points that shared client's
        # `.authenticate`/`.on_auth_failure` callbacks at itself, so when
        # multiple threads call Spotify.track() concurrently (as the importer's
        # metadata pre-fetch does), an in-flight request from one thread can get
        # authenticated using another thread's auth state, causing intermittent
        # wrong/failed track lookups. spotapi.Public already avoids this for
        # search/album/playlist lookups by checking a TLSClient out of a
        # lock-protected pool per call; route track-by-id lookups through the
        # same pool (spotapi.Public.song_info) instead of spotapi.Song()
        # directly, keeping the rest of the method's behavior unchanged.
        from SpotipyFree.Formatter import SpotifyFormatter

        def patched_spotify_track(self, trackId, *args, **kwargs):
            if self.isUrl(trackId):
                trackId = self.urlToId(trackId)

            try:
                raw = _get_track_info_with_retry(trackId)
            except IncompleteTrackInfoError as e:
                # Raising here propagates through SpotipyFree's
                # _addToRecentlyPlayed into the poll loop's catch-all, which
                # drops the whole iteration - so a play that genuinely happened
                # is lost because Spotify wouldn't describe the track (11 times
                # over 11 days in app.log). A marked fallback keeps the play;
                # the metadata is repaired the next time the same id is looked
                # up successfully, since upsertTrack lets real metadata replace
                # a fallback row and its marker.
                logger.warning("No usable track info for %s, recording a fallback record: %s", trackId, e)
                return _fallbackTrackRecord(trackId)
            try:
                artists = raw["firstArtist"]["items"]
                artists.extend(raw["otherArtists"]["items"])
            except Exception:
                artists = ["Not Found"]
            formattedArtists = SpotifyFormatter.formatArtists(artists)
            track = SpotifyFormatter.formatTrack(raw, formattedArtists)
            # SpotifyFormatter drops playability; pass it through so downstream
            # formatting can record why a track isn't playable (e.g.
            # COUNTRY_RESTRICTED on region-blocked tracks with blanked metadata).
            track["playability"] = raw.get("playability")
            if self.getIsrc:
                track["external_ids"] = {"isrc": self._getIsrc(track["track_id"])}
            return track

        SpotipyFree.Spotify.track = patched_spotify_track
        return True
    except (ModuleNotFoundError, ImportError):
        return False


RESPONSE_SNIPPET_MAX_LEN = 1000
RESPONSE_ERROR_SNIPPET_MAX_LEN = 200

# How much of a response body survives into a log line / error message while
# FLASK_DEBUG is off: enough to read a short API error verbatim, far too little
# to bury the line under a page of markup.
RESPONSE_SUMMARY_MAX_LEN = 120
HTML_SNIFF_LEN = 200      #< leading chars examined to decide "web page, not an API reply"
HTML_TITLE_MAX_LEN = 60   #< the page's <title>, kept as its identity

TRUTHY_DEBUG_VALUES = {"1", "true"}  #< FLASK_DEBUG values that enable verbose diagnostics (mirrors Database.database)

HTML_BODY_MARKERS = ("<!doctype", "<html")
_HTML_TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _flaskDebugEnabled() -> bool:
    return os.environ.get("FLASK_DEBUG", "").lower() in TRUTHY_DEBUG_VALUES


def _looksLikeHtml(text: str) -> bool:
    return text.lstrip()[:HTML_SNIFF_LEN].lower().startswith(HTML_BODY_MARKERS)


def _htmlTitle(text: str) -> str | None:
    match = _HTML_TITLE_PATTERN.search(text)
    if match is None:
        return None
    return " ".join(match.group(1).split())[:HTML_TITLE_MAX_LEN] or None


def _describeResponseBody(response, maxLen: int) -> str | None:
    """What of a response body may be written to a log line or error message.

    Spotify answers a rate-limited or bot-checked request to these endpoints
    with its full HTML fallback page, so logging the body verbatim wrote a
    kilobyte of markup and CSS per occurrence - and three times per flake, once
    here and twice more when the listener re-logged the exception carrying it.
    With FLASK_DEBUG off a page collapses to its size and <title> (the part
    that tells a Spotify fallback apart from a Cloudflare challenge, which is
    what these diagnostics exist to identify), while short plain-text bodies -
    the case where the body IS the diagnostic - are kept verbatim. Turn
    FLASK_DEBUG on to get the raw snippet back for a bug report."""
    if response is None:
        return None
    text = str(response)
    if _flaskDebugEnabled():
        return text[:maxLen]
    if _looksLikeHtml(text):
        title = _htmlTitle(text)
        titlePart = f", title={title!r}" if title else ""
        return f"<html page, {len(text)} chars{titlePart}>"
    if len(text) <= RESPONSE_SUMMARY_MAX_LEN:
        return text
    return f"{text[:RESPONSE_SUMMARY_MAX_LEN]}... ({len(text)} chars total)"


# Response headers that may be written to the log. Spotify's replies to these
# endpoints carry live session credentials - __Host-sp_csrf_sid (a one-hour
# session cookie) and x-csrf-token - so the header dict must never be logged
# wholesale. This is an allowlist rather than a denylist on purpose: a header
# nobody anticipated defaults to dropped, so a credential-bearing header added
# upstream can't leak in the window before anyone notices it exists.
LOGGABLE_RESPONSE_HEADERS = frozenset({
    "content-type", "content-length", "content-encoding",
    "date", "server", "retry-after",
    "cf-ray", "cf-cache-status",
})
# Prefix-matched alongside the exact names above - the rate-limit family varies
# by endpoint (x-ratelimit-remaining / -limit / -reset) and all of it is useful.
LOGGABLE_RESPONSE_HEADER_PREFIXES = ("x-ratelimit-",)


def _safeResponseHeaders(headers) -> dict:
    """The subset of `headers` that is safe to write to the log.

    Takes the raw header mapping (or anything at all) and returns a plain dict
    holding only allowlisted entries, preserving their original casing. Never
    raises: every caller is already on an error path, where a failure to
    format the diagnostic would replace a logged warning with an exception."""
    try:
        return {
            name: value
            for name, value in headers.items()
            if (lowered := str(name).lower()) in LOGGABLE_RESPONSE_HEADERS
            or lowered.startswith(LOGGABLE_RESPONSE_HEADER_PREFIXES)
        }
    except Exception:
        # Not a mapping at all (None, a bare object, a test double whose
        # .items() isn't iterable) - an unloggable header set is not a reason
        # to lose the warning it was going to be attached to.
        return {}


def patch_spotapi_user() -> bool:
    """Patch spotapi.user.User methods to log detailed response information
    on JSON deserialization failure, helping identify rate-limiting or
    Cloudflare blocks.
    """
    try:
        import spotapi.user
        from spotapi.exceptions import UserError
        from collections.abc import Mapping
        from typing import Any


        def patched_get_user_info(self) -> Mapping[str, Any]:
            url = "https://www.spotify.com/api/account-settings/v1/profile"
            if not SPOTIFY_LIMITER.acquire(timeout=SPOTIFY_ACQUIRE_TIMEOUT_SECONDS):
                raise SpotifyLocallyRateLimitedError(
                    f"Spotify rate limit backoff in progress - skipped {ENDPOINT_ACCOUNT_PROFILE}")
            resp = self.login.client.get(url)

            if resp.fail:
                logger.warning(
                    "spotapi.User.get_user_info HTTP request failed: endpoint=%s, status=%s, error=%s, response=%s, headers=%s",
                    ENDPOINT_ACCOUNT_PROFILE,
                    resp.status_code,
                    resp.error.string if hasattr(resp.error, "string") else None,
                    _describeResponseBody(resp.response, RESPONSE_SNIPPET_MAX_LEN),
                    _safeResponseHeaders(getattr(getattr(resp, "raw", None), "headers", None))
                )
                _backoffIfThrottled(resp.status_code, resp.response, ENDPOINT_ACCOUNT_PROFILE)
                raise UserError("Could not get user info", error=resp.error.string)

            if not isinstance(resp.response, Mapping):
                logger.warning(
                    "spotapi.User.get_user_info returned non-Mapping response: endpoint=%s, status=%s, type=%s, response=%s, headers=%s",
                    ENDPOINT_ACCOUNT_PROFILE,
                    resp.status_code,
                    type(resp.response).__name__,
                    _describeResponseBody(resp.response, RESPONSE_SNIPPET_MAX_LEN),
                    _safeResponseHeaders(getattr(getattr(resp, "raw", None), "headers", None))
                )
                _backoffIfThrottled(resp.status_code, resp.response, ENDPOINT_ACCOUNT_PROFILE)
                raise UserError(
                    f"Invalid JSON (Status: {resp.status_code}, Type: {type(resp.response).__name__}, "
                    f"Response: {_describeResponseBody(resp.response, RESPONSE_ERROR_SNIPPET_MAX_LEN)})"
                )

            self.csrf_token = resp.raw.headers.get("X-Csrf-Token")
            return resp.response

        def patched_get_plan_info(self) -> Mapping[str, Any]:
            url = "https://www.spotify.com/ca-en/api/account/v2/plan/"
            if not SPOTIFY_LIMITER.acquire(timeout=SPOTIFY_ACQUIRE_TIMEOUT_SECONDS):
                raise SpotifyLocallyRateLimitedError(
                    f"Spotify rate limit backoff in progress - skipped {ENDPOINT_ACCOUNT_PLAN}")
            resp = self.login.client.get(url)

            if resp.fail:
                logger.warning(
                    "spotapi.User.get_plan_info HTTP request failed: endpoint=%s, status=%s, error=%s, response=%s, headers=%s",
                    ENDPOINT_ACCOUNT_PLAN,
                    resp.status_code,
                    resp.error.string if hasattr(resp.error, "string") else None,
                    _describeResponseBody(resp.response, RESPONSE_SNIPPET_MAX_LEN),
                    _safeResponseHeaders(getattr(getattr(resp, "raw", None), "headers", None))
                )
                _backoffIfThrottled(resp.status_code, resp.response, ENDPOINT_ACCOUNT_PLAN)
                raise UserError("Could not get user plan info", error=resp.error.string)

            if not isinstance(resp.response, Mapping):
                logger.warning(
                    "spotapi.User.get_plan_info returned non-Mapping response: endpoint=%s, status=%s, type=%s, response=%s, headers=%s",
                    ENDPOINT_ACCOUNT_PLAN,
                    resp.status_code,
                    type(resp.response).__name__,
                    _describeResponseBody(resp.response, RESPONSE_SNIPPET_MAX_LEN),
                    _safeResponseHeaders(getattr(getattr(resp, "raw", None), "headers", None))
                )
                _backoffIfThrottled(resp.status_code, resp.response, ENDPOINT_ACCOUNT_PLAN)
                raise UserError(
                    f"Invalid JSON (Status: {resp.status_code}, Type: {type(resp.response).__name__}, "
                    f"Response: {_describeResponseBody(resp.response, RESPONSE_ERROR_SNIPPET_MAX_LEN)})"
                )

            return resp.response

        spotapi.user.User.get_user_info = patched_get_user_info
        spotapi.user.User.get_plan_info = patched_get_plan_info
        return True
    except (ModuleNotFoundError, ImportError):
        return False


# Reading manager.state PUTs to Spotify's connect-state endpoint every poll, and a
# single failed PUT (usually throttling) surfaces as ValueError("Could not get
# player state") from spotapi's state property. Reconnecting the whole websocket -
# session renewal included - for each one just adds churn that can itself trip rate
# limits, so escalate to a reconnect only after this many consecutive failures
# (~15s of outage at the default 3s poll interval).
STATE_FAILURE_RECONNECT_THRESHOLD = 5

UPDATE_LOOP_ERROR_SLEEP_SECONDS = 10  #< back off after an unexpected updateLoop error before reconnecting

# --- Event-driven connect-state (eventDrivenConnectStatePlan.md Phase 2) -------
#
# Reading manager.state PUTs to connect-state every refreshInterval - ~10
# requests a minute PER USER, and by far the largest source of Spotify traffic
# this app generates. But connect_device() registers our Spotify-Connection-Id
# with the dealer websocket, which is what makes Spotify PUSH state changes down
# a socket that is already open. Phase 0 measured 10 hours of that channel: one
# HTTP request in total, against the ~5,500 the poll would have made.
#
# Off by default. It replaces how plays are detected - the app's core job - and
# Phase 0's sample was 9 minutes of listening, enough to clear every structural
# unknown but not to call push reliable. Turn it on deliberately, watch it, and
# the poll loop is still there underneath.

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
    Spotify.startRecentlyPlayedListener -> LastPlayedManger.start, which starts
    the thread itself), and the setting is instance-wide anyway - there is
    nothing per-listener to thread through. app.py installs it at startup;
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


def _applyStateToTracking(self, state, callback) -> None:
    """Turn an observed player state into a recorded play, if the track changed.

    Lifted verbatim out of the poll loop so BOTH modes run the same code:
    whatever push does differently, it must not be this. The callback contract
    (previous track's uri, its start time, its context, and the wall-clock ms
    since it started) is what _addToRecentlyPlayed and every downstream play row
    depend on.

    `self` is a LastPlayedManger, taken explicitly rather than bound: these
    helpers sit at module level so they can be tested directly instead of
    through a closure."""
    if (state is None
            or getattr(state, "timestamp", None) is None
            or getattr(state, "track", None) is None
            or getattr(state.track, "uid", None) is None):
        return

    timestamp = int(state.timestamp) / 1000
    if self.lastPLayed != state.track.uid:
        if self.lastTrackUri is not None:
            timePlayed = max(0, int((time.time() - self.lastPlayedAt.timestamp()) * 1000))
            callback(self.lastTrackUri, self.lastPlayedAtText, self.lastContextUri, timePlayed)
        self.lastTrackUri = state.track.uri
        self.lastPlayedAt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
        self.lastPlayedAtText = self.lastPlayedAt.isoformat().replace("+00:00", "Z")
        self.lastContextUri = state.context_uri
        self.lastPLayed = state.track.uid


def _applyPushedState(self, manager, callback) -> None:
    """Build a PlayerState from the cached dict and run track detection.

    Deep-copied for the same reason the state property is: spotapi's
    Track.from_dict REPLACES data["metadata"] in place, so handing it the cached
    _state would corrupt what getConnectPlayerState (Now Playing, the
    missed-track cross-check) reads next."""
    try:
        state = spotapi.status.PlayerState.from_dict(copy.deepcopy(manager._state))
    except Exception as e:  # noqa: BLE001 - a malformed push must not kill the loop
        logger.warning("[SpotipyFree] Could not read a pushed player state: %s", e)
        return
    _applyStateToTracking(self, state, callback)


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
        logger.warning("[SpotipyFree] connect-state subscribe failed: %s", e)
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
        logger.warning("[SpotipyFree] Could not subscribe to connect-state pushes; polling instead")
        return "fallback"

    # _subscribeConnectState adopted the reply, so the channel starts seeded -
    # no separate poll needed to know what is playing right now.
    if isinstance(manager._state, dict):
        _applyPushedState(self, manager, callback)

    logger.info("[SpotipyFree] Listening for connect-state pushes (polling disabled)")
    lastFrameAt = time.monotonic()
    lastSubscribeAt = lastFrameAt
    resubscribeFailures = 0

    while self.run:
        if getattr(manager, "_deliberate_close", False):
            logger.info("[SpotipyFree] Push loop exiting: websocket was closed deliberately")
            self.run = False
            return "stopped"

        packet = manager.get_packet(timeout=PUSH_RECV_TIMEOUT_SECONDS)
        now = time.monotonic()

        if packet is not None:
            # ANY frame proves the socket is alive - keepalive pongs included,
            # which is what makes the watchdog below usable at all.
            lastFrameAt = now
            cluster = _clusterFromPacket(packet)
            if cluster is not None and _adoptCluster(manager, cluster):
                _applyPushedState(self, manager, callback)

        if now - lastFrameAt >= PUSH_FRAME_SILENCE_FALLBACK_SECONDS:
            logger.warning(
                "[SpotipyFree] No websocket frame of any kind for %ds (not even a keepalive "
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
                        "[SpotipyFree] connect-state re-subscribe failed %d times; "
                        "returning to polling", resubscribeFailures)
                    return "fallback"

    return "stopped"


def patch_last_played() -> bool:
    """Patch SpotipyFree.LastPlayed.LastPlayedManger.updateLoop.

    Two modes behind one entry point. Polling (the default) handles a state or
    state.timestamp of None without raising, and only escalates a persistent
    "Could not get player state" streak to a websocket reconnect (see
    STATE_FAILURE_RECONNECT_THRESHOLD). Push mode, when the admin toggle is on,
    listens for the connect-state the dealer websocket already delivers, and
    hands back to polling at the first sign of doubt.
    """
    try:
        from SpotipyFree.LastPlayed import LastPlayedManger

        def patched_update_loop(self, callback, refreshInterval=3):
            if _pushListenerEnabled():
                if _runPushLoop(self, callback) == "stopped":
                    return
                # One-way: once the push channel has disappointed us this
                # listener stays on polling until it is rebuilt. Flapping
                # between the two would be harder to reason about than either.
                logger.warning("[SpotipyFree] Falling back to connect-state polling")
            _runPollLoop(self, callback, refreshInterval)

        def _runPollLoop(self, callback, refreshInterval=3):
            consecutiveStateFailures = 0
            while self.run:
                if getattr(self.manager, "_deliberate_close", False):
                    # Listener.stop()/signalStop() closed this websocket on
                    # purpose - exit instead of hammering a connection that is
                    # gone for good (a leftover loop kept spamming reconnect
                    # errors every few seconds through the 2026-07-17 shutdown).
                    logger.info("[SpotipyFree] Player-state loop exiting: websocket was closed deliberately")
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
                                "[SpotipyFree] Player state unavailable (%d/%d), retrying: %s",
                                consecutiveStateFailures, STATE_FAILURE_RECONNECT_THRESHOLD, stateError,
                            )
                        else:
                            logger.error(
                                "[SpotipyFree] Player state unavailable %d times in a row, reconnecting websocket: %s",
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
                                        "[SpotipyFree] Player-state loop exiting: the HTTP session is "
                                        "closed and cannot be revived: %s", reconnect_err,
                                    )
                                    self.run = False
                                    return
                                logger.error("[SpotipyFree] Websocket reconnect failed; will keep retrying: %s", reconnect_err, exc_info=True)
                        time.sleep(refreshInterval)
                        continue

                    consecutiveStateFailures = 0
                    # Shared with the push path - a state that can't be read
                    # (inactive device, no track) is a no-op there too, so the
                    # sleep below runs either way, exactly as it used to.
                    _applyStateToTracking(self, state, callback)
                    time.sleep(refreshInterval)
                except Exception as e:
                    logger.error("[SpotipyFree] Error in Recently Played: %s", e, exc_info=True)
                    time.sleep(UPDATE_LOOP_ERROR_SLEEP_SECONDS)
                    try:
                        self.manager.reconnect()
                    except Exception as reconnect_err:
                        if _isSessionClosedError(reconnect_err):
                            logger.error(
                                "[SpotipyFree] Player-state loop exiting: the HTTP session is "
                                "closed and cannot be revived: %s", reconnect_err,
                            )
                            self.run = False
                            return
                        logger.error("[SpotipyFree] Websocket reconnect failed; will keep retrying: %s", reconnect_err, exc_info=True)

        LastPlayedManger.updateLoop = patched_update_loop
        return True
    except (ModuleNotFoundError, ImportError):
        return False


patch_spotipy_free()
patch_spotapi_user()
patch_last_played()



