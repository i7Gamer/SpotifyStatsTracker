import logging
import re
import signal
import threading
import time
from contextlib import contextmanager
from SpotipyFree import Spotify
from Database.utils import parseError, timeToInt

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

# Bounds memory for the missed-track dedup set in _checkConnectStateForMissedTracks -
# prev_tracks itself is always much shorter than this (it's a rolling local queue
# history), so this is just a defensive cap, not a tuning knob.
CONNECT_STATE_MISSED_TRACK_CACHE_SIZE = 50


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


def _refresh_spotify_access_token(client_id: str, client_secret: str, refresh_token: str) -> str | None:
    import base64
    import requests
    url = "https://accounts.spotify.com/api/token"
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
    headers = {
        "Authorization": f"Basic {auth_header}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    try:
        resp = requests.post(url, data=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("access_token")
        else:
            logger.error("Failed to refresh Spotify access token: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logger.error("Error refreshing Spotify access token: %s", str(e))
    return None


def _fetch_recently_played_from_web_api(access_token: str) -> list[dict]:
    import requests
    url = "https://api.spotify.com/v1/me/player/recently-played?limit=50"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("items", [])
        else:
            logger.error("Failed to fetch recently played tracks from Web API: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logger.error("Error fetching recently played tracks from Web API: %s", str(e))
    return []


@contextmanager
def _suppress_signal_in_thread():
    """Temporarily patch signal.signal to skip SIGINT registration when called
    from a non-main thread (e.g. Flask worker threads). The spotapi library
    unconditionally registers a SIGINT handler in its __init__, which raises
    ValueError on non-main threads."""
    original = signal.signal
    try:
        original(signal.SIGINT, signal.getsignal(signal.SIGINT))
        is_allowed = True
    except ValueError:
        is_allowed = False

    if not is_allowed:
        def _patched(signalnum, handler):
            logger.debug("Suppressing signal registration for signal %s in context where signals are not allowed", signalnum)
            return signal.getsignal(signalnum)
        signal.signal = _patched
    try:
        yield
    finally:
        signal.signal = original


class Listener:
    def __init__(self, cookiesFile, refreshInterval=6, email=None, get_credentials=None):
        self.run = False
        self.email = email  #< store expected email for validation
        self._authenticated_user_id = None  #< cache spotify user id for validation
        self.get_credentials = get_credentials
        self._lastWebApiPollTime = 0
        with _suppress_signal_in_thread():
            self.sp = Spotify(cookiesFile=cookiesFile, email=email)
            self.sp.startRecentlyPlayedListener(refreshInterval=refreshInterval)

        # Validate that this Spotify client is properly authenticated for the expected user
        try:
            current_user = self.sp.current_user()
            self._authenticated_user_id = current_user.get("id")
            logger.info("Listener initialized for user %s (Spotify ID: %s)", email, self._authenticated_user_id)
        except Exception as e:
            logger.warning("Could not verify authenticated user during listener init: %s", parseError(e))

        self.recentlyPlayed_Z1 = self.sp.current_user_recently_played()
        self._lastChangeTime = time.monotonic()
        self._warnedMissingTrackUris = set()  #< dedupes _checkConnectStateForMissedTracks warnings

    def isLoggedIn(self):
        if self.sp.isLoggedIn() == False:
            return False
        try:
            self.sp.current_user()
            return True
        except:
            return False

    def _validateCurrentUser(self) -> bool:
        """Verify that the authenticated Spotify session still belongs to the expected user.
        Returns True if valid, False if session has changed. Logs warnings if mismatches detected."""
        try:
            current_user = self.sp.current_user()
            current_user_id = current_user.get("id")
            if self._authenticated_user_id and current_user_id != self._authenticated_user_id:
                logger.error(
                    "Session user mismatch! Expected %s, got %s - this could indicate cross-user contamination",
                    self._authenticated_user_id, current_user_id
                )
                return False
            return True
        except Exception as e:
            logger.warning("Could not validate current user: %s", parseError(e))
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

    def _getRecentTrackUrisFromConnectState(self):
        """Read previously-played track URIs off the same PlayerStatus object
        SpotipyFree's LastPlayedManger already keeps refreshed every
        refreshInterval tick (see SpotipyFree/LastPlayed.py) - no extra
        network call needed.

        Deliberately reads the raw cached `_state` dict rather than calling
        PlayerStatus.state/.saved_state/.last_songs_played: `.state` makes a
        fresh connect_device() HTTP request on every access (spotapi's own
        LastPlayed.py comments that this "often gets rate limited"), and
        `.saved_state`/`.last_songs_played` are functools.cached_property, so
        they'd freeze at whatever state existed on first access and never
        pick up later refreshes.

        Returns None if no connect-state has been captured yet (e.g. the
        websocket listener hasn't ticked once, or was never started)."""
        lastPlayedManager = getattr(self.sp, "lastPlayedManager", None)
        manager = getattr(lastPlayedManager, "manager", None) if lastPlayedManager is not None else None
        state = getattr(manager, "_state", None) if manager is not None else None
        if not state:
            return None
        return [uri for uri in (track.get("uri") for track in state.get("prev_tracks", [])) if uri]

    def _checkConnectStateForMissedTracks(self) -> None:
        """Diagnostic cross-check: warn if Spotify's Connect-state queue
        history (prev_tracks) contains a track we never recorded via
        current_user_recently_played(). This is a side-channel, not a source
        of truth - it comes from spotapi's already-running websocket tick, so
        it costs no extra network calls, but prev_tracks is only the local
        queue's rolling history (no per-item timestamp), not an account-wide
        play log - so it can only flag a possible miss, not backfill one.

        Must never raise: a bug here is not allowed to disrupt the primary
        polling loop."""
        try:
            recentUris = self._getRecentTrackUrisFromConnectState()
            if not recentUris:
                return

            recordedTrackIds = {
                item.get("track", {}).get("track_id")
                for item in self.recentlyPlayed_Z1
                if item.get("track")
            }

            missingUris = []
            for uri in recentUris:
                trackId = uri.removeprefix("spotify:track:")
                if trackId in recordedTrackIds or uri in self._warnedMissingTrackUris:
                    continue
                missingUris.append(uri)

            if missingUris:
                logger.warning(
                    "Connect-state queue history shows %d track(s) that were never recorded via "
                    "current_user_recently_played() - the websocket cache may have missed a play. "
                    "Missing tracks: %s",
                    len(missingUris),
                    ", ".join(missingUris),
                )
                for uri in missingUris:
                    if len(self._warnedMissingTrackUris) >= CONNECT_STATE_MISSED_TRACK_CACHE_SIZE:
                        self._warnedMissingTrackUris.pop()
                    self._warnedMissingTrackUris.add(uri)
        except Exception as e:
            logger.debug("Connect-state cross-check failed (non-fatal): %s", parseError(e))

    def _checkOnce(self, callback, onStale) -> bool:
        """One iteration of the poll loop. Returns False if the feed was found
        stale and handed off to `onStale` for reconnection - the caller should
        stop this listener, since a new one now owns tracking. Raises an exception
        if an auth error is detected so startListener can handle it immediately."""
        # Validate session identity to detect cross-user contamination
        if not self._validateCurrentUser():
            logger.error("Listener session validation failed - triggering reconnection")
            if onStale is not None:
                try:
                    onStale()
                except Exception as e:
                    logger.error("Reconnect attempt failed: %s", parseError(e))
            return False

        recentlyPlayed = self.sp.current_user_recently_played()
        if recentlyPlayed != self.recentlyPlayed_Z1:
            newItems = self.getNewItems(recentlyPlayed)
            if newItems:
                logger.info("Listener callback: %d new items for user %s", len(newItems), self.email)
            callback(newItems)
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

    def startListener(self, callback, onStale=None, onWebApiSnapshot=None):
        self.run = True
        while self.run:
            try:
                if not self._checkOnce(callback, onStale):
                    self.run = False
                    return
                self._checkConnectStateForMissedTracks()
                self._checkWebApiBackfill(callback, onWebApiSnapshot=onWebApiSnapshot)
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

    def _checkWebApiBackfill(self, callback, onWebApiSnapshot=None) -> None:
        if not self.get_credentials:
            return

        now = time.monotonic()
        # Query every 5 minutes (300 seconds)
        if now - getattr(self, "_lastWebApiPollTime", 0) < 300:
            return

        self._lastWebApiPollTime = now

        try:
            creds = self.get_credentials()
            if not creds or not creds.get("client_id") or not creds.get("client_secret") or not creds.get("refresh_token"):
                return

            logger.info("Running Spotify Web API recently-played backfill check...")
            access_token = _refresh_spotify_access_token(creds["client_id"], creds["client_secret"], creds["refresh_token"])
            if not access_token:
                logger.warning("Could not obtain access token for Web API backfill.")
                return

            items = _fetch_recently_played_from_web_api(access_token)
            if not items:
                return

            # Compare Web API items against recentlyPlayed_Z1 to see if we missed any
            recorded_timestamps = {
                timeToInt(item.get("played_at"))
                for item in self.recentlyPlayed_Z1
                if item.get("played_at")
            }

            missed_items = []
            for item in items:
                played_at_str = item.get("played_at")
                if not played_at_str:
                    continue
                
                timestamp = timeToInt(played_at_str)
                # Check if we already recorded this play (matching within a 2-second window to handle minor precision diffs)
                is_recorded = any(abs(timestamp - recorded_t) <= 2 for recorded_t in recorded_timestamps)
                if not is_recorded:
                    track = item.get("track")
                    if not track:
                        continue
                    
                    # Estimate ms_played as the full track duration
                    duration_ms = track.get("duration_ms", 0)
                    
                    context = item.get("context") or {}
                    
                    # Convert to the format expected by callback:
                    # {"track": track, "played_at": played_at_str, "ms_played": duration_ms, "context": context}
                    missed_items.append({
                        "track": track,
                        "played_at": played_at_str,
                        "ms_played": duration_ms,
                        "context": context
                    })

            if missed_items:
                logger.info("Backfilling %d plays from Web API recently-played history", len(missed_items))
                # Pass them to callback (it expects a list, newest plays last)
                # Web API returns newest plays first, so reverse to maintain cron order
                missed_items.reverse()
                callback(missed_items)
                
                # Update recentlyPlayed_Z1 to include these newly recorded plays
                for item in missed_items:
                    self.recentlyPlayed_Z1.append(item)

            if onWebApiSnapshot is not None:
                onWebApiSnapshot(items)

        except Exception as e:
            logger.error("Error during Web API backfill: %s", parseError(e))

    def startListener_thread(self, callback, onStale=None, onWebApiSnapshot=None):
        thread = threading.Thread(
            target=self.startListener,
            args=(callback,),
            kwargs={"onStale": onStale, "onWebApiSnapshot": onWebApiSnapshot},
            daemon=True,
        )
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
