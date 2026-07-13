import logging
import re
import signal
import threading
import time
from contextlib import contextmanager
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
        thread = threading.Thread(target=self.startListener, args=(callback,), kwargs={"onStale": onStale}, daemon=True)
        thread.start()
    
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
