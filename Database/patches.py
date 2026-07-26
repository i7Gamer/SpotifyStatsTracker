import copy
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

logger = logging.getLogger(__name__)

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


import json
import sys


try:
    from Database.db import RESTRICTED_FALLBACK_REASON, UNKNOWN_TRACK_NAME
except ModuleNotFoundError:
    from db import RESTRICTED_FALLBACK_REASON, UNKNOWN_TRACK_NAME

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

    The album is keyed per track (album_<trackId>, the same convention the
    importer's fallbacks use) rather than one shared "Unknown album": a single
    fabricated album id would collect every undescribable track from every user
    into one page of unrelated songs."""
    return {
        "name": UNKNOWN_TRACK_NAME,
        "track_id": trackId,
        "id": trackId,
        "disc_number": 0,
        "track_number": 0,
        "duration_ms": 0,
        "artists": [],
        "album": {"id": f"album_{trackId}", "name": "", "images": [],
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
            is_rate_limit = "429" in error_str or ("rate" in error_str and "limit" in error_str)
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
            if not (is_rate_limit or is_session_error or is_failed_request):
                raise

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
            resp = self.login.client.get(url)

            if resp.fail:
                logger.warning(
                    "spotapi.User.get_user_info HTTP request failed: status=%s, error=%s, response=%s, headers=%s",
                    resp.status_code,
                    resp.error.string if hasattr(resp.error, "string") else None,
                    _describeResponseBody(resp.response, RESPONSE_SNIPPET_MAX_LEN),
                    _safeResponseHeaders(getattr(getattr(resp, "raw", None), "headers", None))
                )
                raise UserError("Could not get user info", error=resp.error.string)

            if not isinstance(resp.response, Mapping):
                logger.warning(
                    "spotapi.User.get_user_info returned non-Mapping response: status=%s, type=%s, response=%s, headers=%s",
                    resp.status_code,
                    type(resp.response).__name__,
                    _describeResponseBody(resp.response, RESPONSE_SNIPPET_MAX_LEN),
                    _safeResponseHeaders(getattr(getattr(resp, "raw", None), "headers", None))
                )
                raise UserError(
                    f"Invalid JSON (Status: {resp.status_code}, Type: {type(resp.response).__name__}, "
                    f"Response: {_describeResponseBody(resp.response, RESPONSE_ERROR_SNIPPET_MAX_LEN)})"
                )

            self.csrf_token = resp.raw.headers.get("X-Csrf-Token")
            return resp.response

        def patched_get_plan_info(self) -> Mapping[str, Any]:
            url = "https://www.spotify.com/ca-en/api/account/v2/plan/"
            resp = self.login.client.get(url)

            if resp.fail:
                logger.warning(
                    "spotapi.User.get_plan_info HTTP request failed: status=%s, error=%s, response=%s, headers=%s",
                    resp.status_code,
                    resp.error.string if hasattr(resp.error, "string") else None,
                    _describeResponseBody(resp.response, RESPONSE_SNIPPET_MAX_LEN),
                    _safeResponseHeaders(getattr(getattr(resp, "raw", None), "headers", None))
                )
                raise UserError("Could not get user plan info", error=resp.error.string)

            if not isinstance(resp.response, Mapping):
                logger.warning(
                    "spotapi.User.get_plan_info returned non-Mapping response: status=%s, type=%s, response=%s, headers=%s",
                    resp.status_code,
                    type(resp.response).__name__,
                    _describeResponseBody(resp.response, RESPONSE_SNIPPET_MAX_LEN),
                    _safeResponseHeaders(getattr(getattr(resp, "raw", None), "headers", None))
                )
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


def patch_last_played() -> bool:
    """Patch SpotipyFree.LastPlayed.LastPlayedManger.updateLoop to handle
    situations where state or state.timestamp is None (e.g. inactive device)
    without raising TypeError, spamming tracebacks, or forcing constant reconnects.
    Transient "Could not get player state" failures are retried in place and only
    escalate to a websocket reconnect after a persistent streak (see
    STATE_FAILURE_RECONNECT_THRESHOLD above).
    """
    try:
        from SpotipyFree.LastPlayed import LastPlayedManger
        import datetime

        def patched_update_loop(self, callback, refreshInterval=3):
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
                    if (state is None or
                        getattr(state, "timestamp", None) is None or
                        getattr(state, "track", None) is None or
                        getattr(state.track, "uid", None) is None):
                        time.sleep(refreshInterval)
                        continue

                    timestamp = int(state.timestamp) / 1000
                    if self.lastPLayed != state.track.uid:
                        if self.lastTrackUri is not None:
                            timePlayed = max(0, int((time.time() - self.lastPlayedAt.timestamp()) * 1000))
                            callback(self.lastTrackUri, self.lastPlayedAtText, self.lastContextUri, timePlayed)
                        self.lastTrackUri = state.track.uri
                        self.lastPlayedAt = datetime.datetime.fromtimestamp(
                            timestamp, tz=datetime.timezone.utc
                        )
                        self.lastPlayedAtText = self.lastPlayedAt.isoformat().replace("+00:00", "Z")
                        self.lastContextUri = state.context_uri
                        self.lastPLayed = state.track.uid
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



