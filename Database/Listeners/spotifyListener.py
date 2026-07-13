import logging
import re
import signal
import threading
import time
from contextlib import contextmanager
from typing import Optional
from SpotipyFree import Spotify
from Database.utils import parseError

logger = logging.getLogger(__name__)

LISTENER_STOP_JOIN_TIMEOUT_SECONDS = 5  #< bound how long shutdown waits for spotapi's background LastPlayed thread to exit

# current_user_recently_played() doesn't actually poll - it just returns spotapi's
# websocket-fed local cache (see SpotipyFree.Spotify.current_user_recently_played).
# That websocket can silently die (its own reconnect() call targets a method that
# doesn't exist on PlayerStatus - a bug in spotapi, not this code), after which the
# cache is frozen forever: no exception, no new items, nothing recorded, ever again,
# with the polling loop below none the wiser. If nothing has changed for this long,
# assume the feed is dead and ask the caller to rebuild the session rather than
# staying wedged silently until the process is restarted.
LISTENER_STALE_TIMEOUT_SECONDS = 30 * 60

AUTH_ERROR_TIMEOUT_SECONDS = 30  #< trigger reconnection immediately for auth errors, not 30 min

REST_API_POLL_INTERVAL_SECONDS = 10 * 60  #< poll /v1/me/player/recently-played every 10 minutes for verification
REST_API_LIMIT = 50  #< fetch last 50 tracks per API call
REST_API_BACKOFF_MAX_SECONDS = 3600  #< max backoff time for 429 errors (1 hour)


def _is_auth_error(exc: Exception) -> bool:
    """Check if an exception is an authentication-related error (expired/invalid
    credentials, 401/403) rather than transient network issues."""
    exc_str = str(exc).lower()
    exc_type = type(exc).__name__.lower()
    return (
        "loginerror" in exc_type
        or "loginerror" in exc_str
        or "401" in exc_str
        or "403" in exc_str
        or "unauthorized" in exc_str
        or re.search(r"invalid\s+.*token", exc_str)
        or re.search(r"session\s+.*expired", exc_str)
    )


def _is_rate_limit_error(exc: Exception) -> bool:
    """Check if exception is a 429 rate-limit error."""
    return "429" in str(exc)


def _get_retry_after_header(exc: Exception) -> Optional[int]:
    """Extract Retry-After value from error response, in seconds.
    Returns None if not found or invalid."""
    exc_str = str(exc)
    # Try to extract numeric Retry-After value from error message
    match = re.search(r"Retry-After[:\s]+(\d+)", exc_str, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except (ValueError, AttributeError):
            pass
    return None


@contextmanager
def _suppress_signal_in_thread():
    """Temporarily patch signal.signal to skip SIGINT registration when called
    from a non-main thread (e.g. Flask worker threads). The spotapi library
    unconditionally registers a SIGINT handler in its __init__, which raises
    ValueError on non-main threads."""
    original = signal.signal
    if threading.current_thread() is not threading.main_thread():
        def _patched(signalnum, handler):
            if signalnum == signal.SIGINT:
                return signal.getsignal(signalnum)
            return original(signalnum, handler)
        signal.signal = _patched
    try:
        yield
    finally:
        signal.signal = original


class Listener:
    def __init__(self, cookiesFile, refreshInterval=6, email=None):
        self.run = False
        with _suppress_signal_in_thread():
            self.sp = Spotify(cookiesFile=cookiesFile, email=email)
            self.sp.startRecentlyPlayedListener(refreshInterval=refreshInterval)
        self.recentlyPlayed_Z1 = self.sp.current_user_recently_played()
        self._lastChangeTime = time.monotonic()
        self._rest_api_thread = None
        self._rest_api_backoff_seconds = 0  #< current backoff delay for 429 errors

    def isLoggedIn(self):
        if self.sp.isLoggedIn() == False:
            return False
        try:
            self.sp.current_user()
            return True
        except:
            return False

    def getNewItems(self, new: list):
        oldTimes = [item["played_at"] for item in self.recentlyPlayed_Z1]

        for i, item in enumerate(new):
            # print("Comparing item played at:", item["played_at"], "with old times:", oldTimes)
            if item["played_at"] not in oldTimes:
                return new[i:]

        return None

    def track(self, id):
        return self.sp.track(id)
    
    def playlistName(self, playlistId):
        return self.sp.playlist(playlistId).get("name", "Unknown Playlist")
    def albumName(self, albumId):
        return self.sp.album(albumId).get("name", "Unknown Album")

    def _pollRestApiHistory(self):
        """Fetch recently-played tracks via REST API (/v1/me/player/recently-played).
        Returns list of track objects from authoritative REST API, not websocket cache.
        Raises exception if API call fails."""
        try:
            # Use spotapi's authenticated client for REST API access to recently-played endpoint
            client = self.sp.user_auth.client
            url = "https://api.spotify.com/v1/me/player/recently-played"
            resp = client.get(url, authenticate=True, params={"limit": REST_API_LIMIT})
            if resp.fail:
                raise Exception(f"Failed to fetch recently played: {resp.error.string}")
            
            result = resp.response
            if result and "items" in result:
                return result["items"]
            return []
        except Exception as e:
            logger.error("REST API poll failed: %s", parseError(e))
            raise

    def _filterNewTracks(self, api_tracks, recorded_played_at_times):
        """Filter out tracks that are already recorded based on played_at timestamp.
        Returns only tracks with played_at not in recorded_played_at_times."""
        new_tracks = []
        for track in api_tracks:
            played_at = track.get("played_at")
            if played_at and played_at not in recorded_played_at_times:
                new_tracks.append(track)
        return new_tracks

    def _pollRestApiHistoryLoop(self):
        """Background thread that periodically polls REST API for verification/cleanup.
        Implements exponential backoff for 429 rate-limit errors and respects Retry-After headers."""
        while self.run:
            try:
                # Use backoff delay if rate-limited, otherwise use normal interval
                sleep_duration = self._rest_api_backoff_seconds or REST_API_POLL_INTERVAL_SECONDS
                time.sleep(sleep_duration)
                if not self.run:
                    break
                # Fetch REST API data - this will be used for verification and cleanup
                api_tracks = self._pollRestApiHistory()
                logger.debug("REST API poll returned %d tracks", len(api_tracks))
                # Success: reset backoff
                self._rest_api_backoff_seconds = 0
            except Exception as e:
                if _is_rate_limit_error(e):
                    # Handle 429 rate-limit errors with backoff
                    retry_after = _get_retry_after_header(e)
                    if retry_after:
                        # Spotify told us how long to wait
                        self._rest_api_backoff_seconds = retry_after
                        logger.warning(
                            "REST API rate limited (429). Respecting Retry-After: waiting %ds before retry",
                            retry_after,
                        )
                    else:
                        # Exponential backoff: 2s, 4s, 8s, 16s... up to max (1 hour)
                        if self._rest_api_backoff_seconds == 0:
                            self._rest_api_backoff_seconds = 2
                        else:
                            self._rest_api_backoff_seconds = min(
                                self._rest_api_backoff_seconds * 2, REST_API_BACKOFF_MAX_SECONDS
                            )
                        logger.warning(
                            "REST API rate limited (429). Exponential backoff: waiting %ds before retry",
                            self._rest_api_backoff_seconds,
                        )
                else:
                    # Non-rate-limit errors: log and reset backoff to try again on normal schedule
                    logger.warning("REST API polling error: %s", parseError(e))
                    self._rest_api_backoff_seconds = 0

    def _checkOnce(self, callback, onStale) -> bool:
        """One iteration of the poll loop. Returns False if the feed was found
        stale and handed off to `onStale` for reconnection - the caller should
        stop this listener, since a new one now owns tracking. Raises an exception
        if an auth error is detected so startListener can handle it immediately."""
        recentlyPlayed = self.sp.current_user_recently_played()
        if recentlyPlayed != self.recentlyPlayed_Z1:
            callback(self.getNewItems(recentlyPlayed))
            self.recentlyPlayed_Z1 = recentlyPlayed
            self._lastChangeTime = time.monotonic()
            return True

        if onStale is None:
            return True

        elapsed = time.monotonic() - self._lastChangeTime

        if elapsed <= LISTENER_STALE_TIMEOUT_SECONDS:
            return True

        logger.warning(
            "Recently-played feed unchanged for over %ss, assuming the underlying "
            "session/websocket died silently - reconnecting", LISTENER_STALE_TIMEOUT_SECONDS,
        )
        try:
            onStale()
        except Exception as e:
            logger.error("Reconnect attempt failed: %s", parseError(e))
        return False

    def startListener(self, callback, onStale=None):
        self.run = True
        while self.run:
            try:
                if not self._checkOnce(callback, onStale):
                    self.run = False
                    return
                time.sleep(1)
            except Exception as e:
                if _is_auth_error(e):
                    logger.warning("Auth error detected, triggering immediate reconnection: %s", parseError(e))
                    if onStale is not None:
                        try:
                            onStale()
                        except Exception as reconnect_err:
                            logger.error("Reconnect attempt failed: %s", parseError(reconnect_err))
                    self.run = False
                    return
                else:
                    logger.error("Error in listener: %s", parseError(e))
                    time.sleep(30)

    def startListener_thread(self, callback, onStale=None):
        # Start websocket-based listener thread
        thread = threading.Thread(target=self.startListener, args=(callback,), kwargs={"onStale": onStale}, daemon=True)
        thread.start()
        # Start REST API polling thread for verification/cleanup
        self._rest_api_thread = threading.Thread(target=self._pollRestApiHistoryLoop, daemon=True)
        self._rest_api_thread.start()
    
    def stop(self):
        self.run = False
        # Also stop spotapi's own background LastPlayed thread (started via
        # startRecentlyPlayedListener). Left running, it can hit a rate-limited or
        # malformed response mid-request while the interpreter is shutting down,
        # producing spurious errors. Bounded join instead of calling its own
        # stop() (which joins with no timeout) so app shutdown can't hang.
        lastPlayedManager = getattr(self.sp, "lastPlayedManager", None)
        if lastPlayedManager is not None:
            lastPlayedManager.run = False
            thread = getattr(lastPlayedManager, "thread", None)
            if thread is not None and thread.is_alive():
                thread.join(timeout=LISTENER_STOP_JOIN_TIMEOUT_SECONDS)
