# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import json
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
    SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS,
    SpotifyLocallyRateLimitedError,
)
# The dead-transport detector moved to the owned client package with the loop
# machinery (Phase 1.5); the websocket receive path here still needs it.
from Database.Spotify.recentlyPlayed import _isSessionClosedError
# Rotation recovery reads the current secret from Spotify's own web player -
# see the pinned-secret block below, and Database/Spotify/totpSecret.py.
from Database.Spotify.totpSecret import fetchWebPlayerSecrets

logger = logging.getLogger(__name__)

# Endpoint labels for the shared limiter's backoff reason - short enough for
# the /admin card, specific enough to say WHICH Spotify surface pushed back.
# (The connect-state and track-metadata labels live with their callers in
# Database/Spotify/ since the Phase 1.5 cutover.)
ENDPOINT_ACCOUNT_PROFILE = "account-settings/profile"
ENDPOINT_ACCOUNT_PLAN = "account/plan"

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


# --- Pinned TOTP secret ---------------------------------------------------------
#
# Spotify's web player proves itself to open.spotify.com/api/token with a TOTP
# derived from a secret Spotify rotates occasionally. spotapi does not ship that
# secret as configuration - it FETCHES it, from a third-party mirror
# (code.thetadev.de), on every cold start and every 15 minutes after, and falls
# back SILENTLY to its own hardcoded copy when the fetch fails. That put a host
# this project does not control, cannot pin and did not choose directly in the
# login path of every user.
#
# Verified 2026-07-30: the mirror's newest entry (version 61) is byte-identical
# to spotapi's own _FALLBACK_SECRET. The request being removed here was
# returning a value the library already had compiled in, so pinning changes
# nothing about what is sent to Spotify - it only deletes the round trip and the
# dependency. tests/test_patches.py asserts that equality, so a spotapi bump
# that changes its fallback fails loudly instead of drifting.
#
# WHEN SPOTIFY ROTATES: logins stop working and _get_auth_vars raises
# BaseClientError("Could not get session auth tokens"). The wrapper below turns
# that into a log line naming these constants. Two ways to recover:
#   - an operator can set SPOTIFY_TOTP_SECRET="<version>:<b1,b2,...>" and
#     restart, without waiting for a new image;
#   - the fix proper is to refresh the two constants here and cut a release.
# Rotation becomes a loud, infrequent, deliberate maintenance event instead of a
# silent runtime dependency on someone else's uptime.
SPOTIFY_TOTP_SECRET_VERSION = 61
SPOTIFY_TOTP_SECRET_BYTES = (
    44, 55, 47, 42, 70, 40, 34, 114, 76, 74, 50, 111, 120,
    97, 75, 76, 94, 102, 43, 69, 49, 120, 118, 80, 64, 78,
)

TOTP_SECRET_ENV_VAR = "SPOTIFY_TOTP_SECRET"
TOTP_SECRET_BYTE_MAX = 255   #< a secret byte is a byte; anything else is a typo, not a rotation

# How many token requests must fail in a row before the admin panel calls it a
# rotation. A rotation is total and instance-wide - confirmed empirically on
# 2026-07-30 by running the smoke test against a deliberately wrong secret:
# every public lookup failed with "Could not get session auth tokens", whereas
# a bad-cookies run left them all passing and failed only current_user(). But a
# 429, a Spotify outage or a network blip raise the same exception, so a single
# failure proves nothing. Same "don't believe it until it repeats" shape as
# SCOPE_ERROR_CONFIRM_THRESHOLD in the listener.
TOTP_ROTATION_CONFIRM_THRESHOLD = 3

# Once a rotation is CONFIRMED, the replacement is read from Spotify's own
# web-player bundle (Database/Spotify/totpSecret.py) rather than from any
# third-party mirror. Two unauthenticated requests, and only ever after the
# threshold above - the pinned secret remains the normal path, so if Spotify
# restructures that bundle the result is "recovery unavailable" (exactly
# today's behaviour) rather than broken logins.
#
# On by default because the source is Spotify itself and it only runs when the
# instance is already failing; the switch exists for operators who would rather
# nothing touched authentication on its own.
TOTP_AUTO_RECOVER_ENV_VAR = "SPOTIFY_TOTP_AUTO_RECOVER"

# An instance in this state retries on every request, so without a cooldown a
# rotation would turn into a request flood against Spotify's CDN.
TOTP_RECOVERY_COOLDOWN_SECONDS = 15 * 60

_totpAuthLock = threading.Lock()
_totpConsecutiveFailures = 0
_totpFirstFailureAt = None   #< monotonic; "how long has this been going on"
_totpAdoptedSecret = None    #< (version, bytearray) once recovery has found a newer one
_totpLastRecoveryAt = None   #< monotonic; gates the cooldown above


def recordTotpAuthFailure() -> int:
    """Count a failed session-token request. Returns the new streak length."""
    global _totpConsecutiveFailures, _totpFirstFailureAt
    with _totpAuthLock:
        if _totpConsecutiveFailures == 0:
            _totpFirstFailureAt = time.monotonic()
        _totpConsecutiveFailures += 1
        return _totpConsecutiveFailures


def recordTotpAuthSuccess() -> None:
    """Clear the streak. Whatever it was, it is over - and a stale alarm on the
    admin panel is worse than no alarm, because it trains people to ignore it."""
    global _totpConsecutiveFailures, _totpFirstFailureAt
    with _totpAuthLock:
        _totpConsecutiveFailures = 0
        _totpFirstFailureAt = None


def resetTotpAuthState() -> None:
    """Test seam - this state is process-global, so tests must not inherit each
    other's streaks, adopted secrets or cooldowns."""
    global _totpAdoptedSecret, _totpLastRecoveryAt
    recordTotpAuthSuccess()
    with _totpAuthLock:
        _totpAdoptedSecret = None
        _totpLastRecoveryAt = None


def _autoRecoverEnabled() -> bool:
    raw = os.environ.get(TOTP_AUTO_RECOVER_ENV_VAR)
    if raw is None:
        return True   #< default on
    return raw.strip().lower() in TRUTHY_DEBUG_VALUES


def attemptTotpRecovery() -> bool:
    """Read the current secret from Spotify's web player and adopt it if it is
    newer than the one in force. True when something was adopted.

    Strictly-newer only: re-reading the version we already have proves nothing
    changed, and an older one (a stale edge, a rollback) must never walk us
    backwards. Never raises - the caller is already handling a failure, and an
    exception here would replace a diagnosable auth error with a network one."""
    global _totpAdoptedSecret, _totpLastRecoveryAt

    if not _autoRecoverEnabled():
        return False

    now = time.monotonic()
    with _totpAuthLock:
        if _totpLastRecoveryAt is not None and now - _totpLastRecoveryAt < TOTP_RECOVERY_COOLDOWN_SECONDS:
            return False
        _totpLastRecoveryAt = now

    currentVersion, _ = _resolveTotpSecret()
    candidates = [(version, secret) for version, secret in fetchWebPlayerSecrets()
                  if version > currentVersion]
    if not candidates:
        logger.warning(
            "Read Spotify's web player for a newer TOTP secret and found none newer "
            "than version %s. If logins are still failing, the cause is something "
            'else, or set %s="<version>:<comma-separated bytes>" manually.',
            currentVersion, TOTP_SECRET_ENV_VAR)
        return False

    version, secret = max(candidates, key=lambda pair: pair[0])
    with _totpAuthLock:
        _totpAdoptedSecret = (version, bytearray(secret))
    logger.warning(
        "Adopted TOTP secret version %s, read from Spotify's own web player (was %s). "
        "Logins should recover on the next attempt. This lives in memory only - pin it "
        "in Database/patches.py (SPOTIFY_TOTP_SECRET_VERSION/_BYTES) to make it "
        "permanent, or it will be re-read after every restart.",
        version, currentVersion)
    return True


def totpAuthSnapshot() -> dict:
    """What /admin shows for the pinned TOTP secret.

    Deliberately answers "what now?" as well as "what is wrong": which version
    is actually in force (which is NOT the pinned one when an override is set),
    and the name of the variable to set."""
    activeVersion, _ = _resolveTotpSecret()
    with _totpAuthLock:
        failures = _totpConsecutiveFailures
        firstAt = _totpFirstFailureAt
        adopted = _totpAdoptedSecret
    envOverride = bool(os.environ.get(TOTP_SECRET_ENV_VAR, "").strip())
    return {
        "pinnedVersion": SPOTIFY_TOTP_SECRET_VERSION,
        "activeVersion": activeVersion,
        # Distinguished on purpose: "someone set a variable" and "we adopted
        # one off Spotify" call for different follow-up, and both differ from
        # the pinned default.
        "overrideActive": envOverride,
        "autoRecovered": not envOverride and adopted is not None,
        "overrideEnvVar": TOTP_SECRET_ENV_VAR,
        "consecutiveFailures": failures,
        "suspectedRotation": failures >= TOTP_ROTATION_CONFIRM_THRESHOLD,
        "secondsSinceFirstFailure": None if firstAt is None else time.monotonic() - firstAt,
    }


def _parseTotpSecretOverride(raw: str):
    """A "<version>:<b1,b2,...>" override string as (version, bytearray), or a
    ValueError when it isn't one. Strict on purpose - a half-understood value
    would fail later, at Spotify, as an unexplained login failure."""
    version, separator, byteText = raw.partition(":")
    if not separator:
        raise ValueError('expected "<version>:<comma-separated bytes>"')
    values = [chunk.strip() for chunk in byteText.split(",") if chunk.strip()]
    if not values:
        raise ValueError("no secret bytes given")
    secret = bytearray()
    for value in values:
        number = int(value)   #< ValueError for anything non-numeric
        if not 0 <= number <= TOTP_SECRET_BYTE_MAX:
            raise ValueError(f"byte {number} out of range 0-{TOTP_SECRET_BYTE_MAX}")
        secret.append(number)
    return int(version.strip()), secret


def _resolveTotpSecret():
    """The (version, secret) to authenticate with, in precedence order:

      1. the environment override, when it parses - a human setting it is a
         decision, and it must beat anything derived automatically;
      2. a secret adopted by recovery from Spotify's own web player;
      3. the pinned constants - the normal path.

    A malformed override is reported and then IGNORED rather than raised. The
    override exists to rescue an instance during a rotation; letting a typo in
    it take login offline would invert the point of having it."""
    raw = os.environ.get(TOTP_SECRET_ENV_VAR, "").strip()
    if raw:
        try:
            return _parseTotpSecretOverride(raw)
        except ValueError as e:
            logger.error(
                "Ignoring malformed %s (%s); falling back to the secret pinned in "
                "Database/patches.py (version %s).",
                TOTP_SECRET_ENV_VAR, e, SPOTIFY_TOTP_SECRET_VERSION)

    with _totpAuthLock:
        adopted = _totpAdoptedSecret
    if adopted is not None:
        return adopted[0], bytearray(adopted[1])

    # A fresh bytearray per call: generate_totp() transforms these bytes, and a
    # shared mutable would let one caller's mutation corrupt every later login.
    return SPOTIFY_TOTP_SECRET_VERSION, bytearray(SPOTIFY_TOTP_SECRET_BYTES)


def patch_totp_secret() -> bool:
    """Replace spotapi's mirror fetch with the pinned secret, and make a token
    failure name it (see the block comment above)."""
    try:
        import spotapi.client
    except (ModuleNotFoundError, ImportError):
        return False

    spotapi.client.get_latest_totp_secret = _resolveTotpSecret

    # Idempotent on purpose: this runs at import AND is re-applied deliberately
    # by tests whose import order may have missed it (see setUpModule in
    # tests/test_patches.py). Without the guard the second application wraps
    # the first, so one failed request would count twice and trip the rotation
    # threshold early. The other patches in this module get this for free by
    # capturing their originals at module scope.
    if getattr(spotapi.client.BaseClient._get_auth_vars, "_totpTracked", False):
        return True

    original_get_auth_vars = spotapi.client.BaseClient._get_auth_vars

    def patched_get_auth_vars(self, *args, **kwargs):
        try:
            result = original_get_auth_vars(self, *args, **kwargs)
        except spotapi.exceptions.BaseClientError:
            failures = recordTotpAuthFailure()
            # The message spotapi raises names neither TOTP nor these
            # constants, so the pin would be undiscoverable from the symptom it
            # produces. Logged at the confirmation threshold and then every
            # further multiple of it: an instance in this state retries
            # constantly, and one line per attempt would bury the incident it
            # is reporting.
            if failures % TOTP_ROTATION_CONFIRM_THRESHOLD == 0:
                logger.error(
                    "Spotify has refused the session token request %d times in a row. "
                    "That is instance-wide, so the most likely cause is a rotated TOTP "
                    "secret: this build pins version %s (SPOTIFY_TOTP_SECRET_VERSION in "
                    'Database/patches.py). Set %s="<version>:<comma-separated bytes>" and '
                    "restart to apply a new one without waiting for a release. See the "
                    "Worker Health panel on /admin.",
                    failures, SPOTIFY_TOTP_SECRET_VERSION, TOTP_SECRET_ENV_VAR)
                # Confirmed streak: go read the current secret off Spotify's own
                # web player. Rate-limited and never raising (see
                # attemptTotpRecovery); an adopted secret takes effect on the
                # next attempt rather than retrying this one, which keeps the
                # error semantics of this call unchanged.
                attemptTotpRecovery()
            raise
        recordTotpAuthSuccess()
        return result

    patched_get_auth_vars._totpTracked = True
    spotapi.client.BaseClient._get_auth_vars = patched_get_auth_vars
    return True


patch_spotapi_user()
patch_totp_secret()
