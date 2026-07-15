import logging
import signal
import threading
import time
import websockets.sync.client
import websockets.exceptions
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
    except Exception:
        pass

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


def _get_track_info_with_retry(trackId: str, max_retries: int = 3):
    """Fetch track info from spotapi with retry logic for transient failures.

    Args:
        trackId: Spotify track ID
        max_retries: Maximum number of retry attempts

    Returns:
        Track info dict from spotapi.Public.song_info()["data"]["trackUnion"]

    Raises:
        Exception: If all retries fail
    """
    for attempt in range(max_retries):
        try:
            return spotapi.Public.song_info(trackId)["data"]["trackUnion"]
        except Exception as e:
            error_str = str(e).lower()
            is_rate_limit = "429" in error_str or ("rate" in error_str and "limit" in error_str)
            is_session_error = "could not get session" in error_str or "session" in error_str

            # Only retry on transient errors (rate limit, session issues), not on real 404s
            if not (is_rate_limit or is_session_error):
                raise

            if attempt < max_retries - 1:
                backoff_secs = 2 ** attempt  # 1, 2, 4 seconds
                logger.warning("Track fetch failed (attempt %d/%d), backing off %ds: %s", attempt + 1, max_retries, backoff_secs, e)
                time.sleep(backoff_secs)
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

            raw = _get_track_info_with_retry(trackId)
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

        original_get_user_info = spotapi.user.User.get_user_info
        original_get_plan_info = spotapi.user.User.get_plan_info

        def patched_get_user_info(self) -> Mapping[str, Any]:
            url = "https://www.spotify.com/api/account-settings/v1/profile"
            resp = self.login.client.get(url)

            if resp.fail:
                logger.warning(
                    "spotapi.User.get_user_info HTTP request failed: status=%s, error=%s, response=%s, headers=%s",
                    resp.status_code,
                    resp.error.string if hasattr(resp.error, "string") else None,
                    str(resp.response)[:RESPONSE_SNIPPET_MAX_LEN] if resp.response is not None else None,
                    dict(resp.raw.headers) if hasattr(resp.raw, "headers") else {}
                )
                raise UserError("Could not get user info", error=resp.error.string)

            if not isinstance(resp.response, Mapping):
                logger.warning(
                    "spotapi.User.get_user_info returned non-Mapping response: status=%s, type=%s, response=%s, headers=%s",
                    resp.status_code,
                    type(resp.response).__name__,
                    str(resp.response)[:RESPONSE_SNIPPET_MAX_LEN] if resp.response is not None else None,
                    dict(resp.raw.headers) if hasattr(resp.raw, "headers") else {}
                )
                raise UserError(
                    f"Invalid JSON (Status: {resp.status_code}, Type: {type(resp.response).__name__}, "
                    f"Response: {str(resp.response)[:RESPONSE_ERROR_SNIPPET_MAX_LEN]})"
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
                    str(resp.response)[:RESPONSE_SNIPPET_MAX_LEN] if resp.response is not None else None,
                    dict(resp.raw.headers) if hasattr(resp.raw, "headers") else {}
                )
                raise UserError("Could not get user plan info", error=resp.error.string)

            if not isinstance(resp.response, Mapping):
                logger.warning(
                    "spotapi.User.get_plan_info returned non-Mapping response: status=%s, type=%s, response=%s, headers=%s",
                    resp.status_code,
                    type(resp.response).__name__,
                    str(resp.response)[:RESPONSE_SNIPPET_MAX_LEN] if resp.response is not None else None,
                    dict(resp.raw.headers) if hasattr(resp.raw, "headers") else {}
                )
                raise UserError(
                    f"Invalid JSON (Status: {resp.status_code}, Type: {type(resp.response).__name__}, "
                    f"Response: {str(resp.response)[:RESPONSE_ERROR_SNIPPET_MAX_LEN]})"
                )

            return resp.response

        spotapi.user.User.get_user_info = patched_get_user_info
        spotapi.user.User.get_plan_info = patched_get_plan_info
        return True
    except (ModuleNotFoundError, ImportError):
        return False


patch_spotipy_free()
patch_spotapi_user()



