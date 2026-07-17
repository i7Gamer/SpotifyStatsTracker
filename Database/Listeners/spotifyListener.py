import collections
import os
import logging
import re
import signal
import threading
import time
from contextlib import contextmanager
from SpotipyFree import Spotify
from Database.utils import parseError, timeToInt

# A background thread's websocket ping (e.g. spotapi's keep_alive) can raise
# websockets.exceptions.ConnectionClosed/ConnectionAbortedError for many reasons -
# a graceful shutdown close, or the connection simply dropping. Either way it's
# already handled: patched_keep_alive (Database/patches.py) reconnects on a drop,
# and the stale-feed watchdog below rebuilds the whole listener if pings stay
# dead. So log one clean line instead of letting threading's default excepthook
# dump a raw traceback. Anything else (a real bug) still gets the default
# handler so it stays loud and visible.
import websockets.exceptions
import sys

logger = logging.getLogger(__name__)

def _shutdown_exception_hook(args):
    """Log expected websocket-close exceptions from background threads instead of
    letting them print a raw traceback; forward anything else to the default handler."""
    exc = args.exc_value

    if isinstance(exc, (websockets.exceptions.ConnectionClosed, ConnectionAbortedError)):
        threadName = args.thread.name if args.thread is not None else "unknown"
        logger.warning(
            "Background thread '%s' exited after websocket close (%s); reconnect/stale-feed recovery will handle it.",
            threadName, exc,
        )
        return

    # Otherwise, use the default exception handler
    sys.__excepthook__(args.exc_type, args.exc_value, args.exc_traceback)

threading.excepthook = _shutdown_exception_hook

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

RATE_LIMIT_ERROR_BACKOFF_SECONDS = 60  #< backoff for 429 rate limit errors from Spotify API

# Bounds memory for the missed-track dedup set in _checkConnectStateForMissedTracks -
# prev_tracks itself is always much shorter than this (it's a rolling local queue
# history), so this is just a defensive cap, not a tuning knob.
CONNECT_STATE_MISSED_TRACK_CACHE_SIZE = 50

WEB_API_POLL_INTERVAL_SECONDS = 15 * 60  #< Query Web API recently-played backfill every 15 minutes

USER_VALIDATION_CACHE_SECONDS = 5 * 60  #< Cache user validation results to reduce bot detection triggers

TRUTHY_DEBUG_VALUES = {"1", "true"}  #< FLASK_DEBUG values that enable verbose diagnostics (mirrors Database.database)


def _flaskDebugEnabled() -> bool:
    return os.environ.get("FLASK_DEBUG", "").lower() in TRUTHY_DEBUG_VALUES


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
    """Check if an exception is a rate limit error (429 Too Many Requests)
    or other transient Spotify API error (malformed JSON, etc.)."""
    exc_str = str(exc).lower()
    return ("429" in exc_str or ("rate" in exc_str and "limit" in exc_str) or
            "json" in exc_str)  # Invalid JSON usually indicates Spotify API issue


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


def _get_current_user_from_web_api(access_token: str) -> dict | None:
    """Fetch current user info from Web API to validate the access token belongs to the expected user."""
    import requests
    url = "https://api.spotify.com/v1/me"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        else:
            logger.error("Failed to fetch current user from Web API: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logger.error("Error fetching current user from Web API: %s", str(e))
    return None


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
    def __init__(self, cookiesFile, refreshInterval=6, email=None, get_credentials=None,
                 get_backfill_enabled=None):
        self.run = False
        self._stop_event = threading.Event()
        self.email = email  #< store expected email for validation
        self._authenticated_user_id = None  #< cache spotify user id for validation
        self.contaminationDetected = False  #< True when the cookies authenticate as a DIFFERENT account than self.email
        self.get_credentials = get_credentials
        # Optional admin kill switch, re-read each poll (like get_credentials)
        # so a restart is never needed to see a flip - None (the default for
        # any caller that doesn't pass it, e.g. existing tests) means always
        # enabled.
        self.get_backfill_enabled = get_backfill_enabled
        self._lastWebApiPollTime = None  #< None means "never polled yet" - forces an immediate first poll
        with _suppress_signal_in_thread():
            self.sp = Spotify(cookiesFile=cookiesFile, email=email)
            self.sp.startRecentlyPlayedListener(refreshInterval=refreshInterval)

        # Validate that this Spotify client is properly authenticated for the expected user
        try:
            current_user = self.sp.current_user()
            self._authenticated_user_id = current_user.get("id")
            authenticated_email = current_user.get("email")

            # CRITICAL: Verify cookies actually belong to the expected user, not a
            # different account. Detection alone isn't enough - the flag set here
            # makes this listener refuse to record anything (see startListener/
            # isLoggedIn): without it, plays from the wrong account kept being
            # recorded under this user, and _validateCurrentUser was no help
            # because it baselines on _authenticated_user_id, i.e. the WRONG
            # account's own id, so the ongoing check always passed. Only a real,
            # non-empty string email is proof of a mismatch - Spotify can return
            # "email": null, which must not read as contamination.
            if isinstance(authenticated_email, str) and authenticated_email and email:
                if authenticated_email.lower() != email.lower():
                    self.contaminationDetected = True
                    logger.error(
                        "CRITICAL: Cookie contamination detected! Cookies for %s are actually authenticated as %s. "
                        "Recording is disabled for this listener so plays from %s are NOT recorded under %s's account. "
                        "The stored cookies must be re-authorized.",
                        email, authenticated_email, authenticated_email, email
                    )

            logger.info("Listener initialized for user %s (Spotify ID: %s)", email, self._authenticated_user_id)
        except Exception as e:
            logger.warning("Could not verify authenticated user during listener init: %s", parseError(e))

        self.recentlyPlayed_Z1 = self.sp.current_user_recently_played()
        self.webApiRecentlyPlayed_Z1 = []  #< _checkWebApiBackfill's own dedup bookkeeping, kept
                                            #  separate from recentlyPlayed_Z1 (live-listener-owned,
                                            #  different dict shape) so the two polling loops never
                                            #  stomp on each other's cache
        self._lastChangeTime = time.monotonic()
        self._warnedMissingTrackUris = collections.OrderedDict()  #< dedupes _checkConnectStateForMissedTracks
                                                                   #  warnings; OrderedDict (not set) so
                                                                   #  eviction can target the oldest entry
        self._last_user_validation_time = None  #< None means "never validated yet" - forces an immediate first check
        self._last_user_validation_result = True  #< cache validation result

    def isLoggedIn(self):
        # A contaminated session is technically logged in - as the WRONG
        # account. Reporting False routes the user back through the login
        # flow, whose cookie verification requires the matching account.
        if self.contaminationDetected:
            return False
        if self.sp.isLoggedIn() == False:
            return False
        try:
            self.sp.current_user()
            return True
        except:
            return False

    def _validateCurrentUser(self) -> bool:
        """Verify that the authenticated Spotify session still belongs to the expected user.
        Returns True if valid, False if session has changed. Logs warnings if mismatches detected.

        Results are cached for USER_VALIDATION_CACHE_SECONDS to reduce bot detection triggers
        from excessive polling. Cache is bypassed on errors to detect auth failures quickly."""
        now = time.monotonic()
        # Return cached result if still fresh (never cached yet on first call)
        if self._last_user_validation_time is not None and (now - self._last_user_validation_time) < USER_VALIDATION_CACHE_SECONDS:
            return self._last_user_validation_result

        try:
            current_user = self.sp.current_user()
            current_user_id = current_user.get("id")
            if self._authenticated_user_id and current_user_id != self._authenticated_user_id:
                logger.error(
                    "Session user mismatch! Expected %s, got %s - this could indicate cross-user contamination",
                    self._authenticated_user_id, current_user_id
                )
                result = False
            else:
                result = True

            self._last_user_validation_time = now
            self._last_user_validation_result = result
            return result
        except Exception as e:
            error_str = str(e).lower()
            # Invalid JSON errors usually indicate rate limiting or bad response from Spotify
            if "json" in error_str or _is_rate_limit_error(e):
                logger.warning("Transient error validating current user (rate limit or malformed response): %s", parseError(e))
                raise  # Trigger rate limit backoff in startListener
            if not _is_auth_error(e):
                raise
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

    def getConnectPlayerState(self) -> dict | None:
        """The raw connect player_state dict off the same PlayerStatus object
        SpotipyFree's LastPlayedManger already keeps refreshed every
        refreshInterval tick (see SpotipyFree/LastPlayed.py) - no extra
        network call needed. Feeds both the missed-track cross-check and the
        dashboard's Now Playing.

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
        return state or None

    def _getRecentTrackUrisFromConnectState(self):
        """Previously-played track URIs from the connect state, or None if no
        state has been captured yet - see getConnectPlayerState()."""
        state = self.getConnectPlayerState()
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
                        self._warnedMissingTrackUris.popitem(last=False)  #< evict oldest (FIFO) -
                                                                           #  set.pop() would remove
                                                                           #  an arbitrary element
                    self._warnedMissingTrackUris[uri] = None
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

        try:
            recentlyPlayed = self.sp.current_user_recently_played()
        except Exception as e:
            error_str = str(e).lower()
            # Invalid JSON errors usually indicate rate limiting or bad response from Spotify
            if "json" in error_str or _is_rate_limit_error(e):
                logger.warning("Transient error fetching recently played (rate limit or malformed response): %s", parseError(e))
                raise  # Trigger rate limit backoff in startListener
            raise  # Let startListener handle other errors

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
        if self.contaminationDetected:
            # Never record from a session that belongs to a different account -
            # see the contamination check in __init__.
            logger.error(
                "Listener for %s not started: the stored cookies authenticate as a "
                "different Spotify account. Re-login with matching cookies to resume tracking.",
                self.email,
            )
            self.run = False
            return
        self.run = True
        while self.run and not self._stop_event.is_set():
            try:
                if not self._checkOnce(callback, onStale):
                    self.run = False
                    return
                self._checkConnectStateForMissedTracks()
                self._checkWebApiBackfill(callback, onWebApiSnapshot=onWebApiSnapshot)
                self._stop_event.wait(1)
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
                elif _is_rate_limit_error(e):
                    logger.warning("Rate limit error detected, backing off for %d seconds: %s", RATE_LIMIT_ERROR_BACKOFF_SECONDS, parseError(e))
                    self._stop_event.wait(RATE_LIMIT_ERROR_BACKOFF_SECONDS)
                else:
                    logger.error("Error in listener: %s", parseError(e))
                    self._stop_event.wait(30)

    def _checkWebApiBackfill(self, callback, onWebApiSnapshot=None) -> None:
        if not self.get_credentials:
            return
        if self.get_backfill_enabled is not None and not self.get_backfill_enabled():
            return

        now = time.monotonic()
        # Query every 15 minutes (never polled yet on first call)
        lastPollTime = getattr(self, "_lastWebApiPollTime", None)
        if lastPollTime is not None and now - lastPollTime < WEB_API_POLL_INTERVAL_SECONDS:
            return

        self._lastWebApiPollTime = now

        try:
            creds = self.get_credentials()
            if not creds or not creds.get("client_id") or not creds.get("client_secret") or not creds.get("refresh_token"):
                return

            if _flaskDebugEnabled():
                logger.info("Running Spotify Web API recently-played backfill check...")
            access_token = _refresh_spotify_access_token(creds["client_id"], creds["client_secret"], creds["refresh_token"])
            if not access_token:
                logger.warning("Could not obtain access token for Web API backfill.")
                return

            # Validate that the access token belongs to the authenticated user,
            # not a different Spotify account (prevents cross-user contamination)
            web_api_user = _get_current_user_from_web_api(access_token)
            if not web_api_user:
                logger.warning("Could not validate Web API user, skipping backfill.")
                return

            web_api_user_id = web_api_user.get("id")
            web_api_user_display = web_api_user.get("display_name", web_api_user_id)
            web_api_user_email = web_api_user.get("email", "")
            if _flaskDebugEnabled():
                logger.info("Web API user: %s (ID: %s, email: %s), Listener email: %s",
                           web_api_user_display, web_api_user_id, web_api_user_email, self.email)

            # Validate that the access token belongs to the authenticated user. Since SpotipyFree
            # may store user IDs differently than the Spotify Web API, check email first (most reliable),
            # fall back to display name if email unavailable.
            mismatch = False
            if self.email and web_api_user_email and self.email.lower() != web_api_user_email.lower():
                mismatch = True
                mismatch_reason = f"email mismatch: API has {web_api_user_email}, listener is {self.email}"
            elif self.email and not web_api_user_email and web_api_user_display:
                # Email validation failed (API response missing email), fall back to display name
                # Only flag if listener email username doesn't roughly match display name
                logger.debug("Web API response missing email, using display name as backup validation")
                mismatch = False  # Can't prove mismatch without email, be lenient

            if mismatch:
                logger.error(
                    "CONTAMINATION CHECK FAILED: Web API user mismatch (%s). Skipping backfill to prevent cross-user data import.",
                    mismatch_reason
                )
                return

            items = _fetch_recently_played_from_web_api(access_token)
            if _flaskDebugEnabled():
                logger.info("Web API returned %d items for backfill check", len(items) if items else 0)
            if not items:
                return

            # Compare Web API items against everything recorded so far - both
            # the live listener's own cache AND this function's own cache from
            # a previous poll (kept separate from recentlyPlayed_Z1, which is
            # exclusively owned by the live-listener path - see webApiRecentlyPlayed_Z1's
            # own comment in __init__).
            recorded_timestamps = {
                timeToInt(item.get("played_at"))
                for item in self.recentlyPlayed_Z1 + self.webApiRecentlyPlayed_Z1
                if item.get("played_at")
            }

            # Built directly from `items` in one pass so each missed item stays
            # tied to its OWN source API item's played_at - no post-hoc
            # re-matching by track ID, which breaks when the same track
            # appears more than once in `items` (all copies would resolve to
            # whichever occurrence next() finds first).
            missed_items = []

            for item in items:
                played_at_str = item.get("played_at")
                track = item.get("track")
                track_id = track.get("id") if track else None
                if not played_at_str or not track_id:
                    continue

                timestamp = timeToInt(played_at_str)
                duration_ms = track.get("duration_ms", 0)
                duration_s = duration_ms // 1000

                # Spotify's Web API documents played_at only as "the date and
                # time the track was played" - it does NOT specify start vs
                # end, and Spotify's own developer community has confirmed the
                # same endpoint can report either for different entries (see
                # spotify/web-api#1083). So this can't assume one direction:
                # check both interpretations - timestamp itself already being
                # a start time, or timestamp being an end time duration_s
                # seconds after the true start - before deciding this play is
                # genuinely missing.
                is_recorded = any(
                    abs(timestamp - recorded_t) <= 2 or abs(timestamp - duration_s - recorded_t) <= 2
                    for recorded_t in recorded_timestamps
                )
                if not is_recorded:
                    context = item.get("context") or {}

                    # Store played_at as given, untouched - see comment above
                    # on why we no longer subtract duration_s here.
                    missed_items.append({
                        "track": track,
                        "played_at": played_at_str,
                        "ms_played": duration_ms,
                        "context": context
                    })

            if missed_items:
                logger.info("Backfilling %d plays from Web API recently-played history", len(missed_items))
                # Mark these as backfilled so the database can record the source
                for missed_item in missed_items:
                    missed_item["_source"] = "web_api_backfill"
                # Pass them to callback (it expects a list, newest plays last)
                # Web API returns newest plays first, so reverse to maintain cron order
                missed_items.reverse()
                callback(missed_items)

            # Replace webApiRecentlyPlayed_Z1 (NOT recentlyPlayed_Z1 - that
            # cache belongs to the live listener) with this batch so it holds
            # exactly the last batch checked - only plays not in this batch
            # get treated as new/missed on the next run. Entries missing a
            # track ID or played_at are skipped entirely.
            self.webApiRecentlyPlayed_Z1 = [
                {
                    "track": item.get("track"),
                    "played_at": item.get("played_at"),
                    "ms_played": item.get("track", {}).get("duration_ms", 0),
                    "context": item.get("context") or {}
                }
                for item in items
                if item.get("played_at") and item.get("track", {}).get("id")
            ]

            if onWebApiSnapshot is not None:
                onWebApiSnapshot(items)

        except Exception as e:
            logger.error("Error during Web API backfill: %s", parseError(e))

    def startListener_thread(self, callback, onStale=None, onWebApiSnapshot=None):
        self._stop_event.clear()
        self.thread = threading.Thread(
            target=self.startListener,
            args=(callback,),
            kwargs={"onStale": onStale, "onWebApiSnapshot": onWebApiSnapshot},
            daemon=True,
        )
        self.thread.start()

    def signalStop(self):
        """Signal-only half of stop(): flip every stop flag (the poll loop,
        spotapi's LastPlayed thread, the patched keep_alive/updateLoop) without
        joining threads or closing sockets. Shutdown's phase 1 calls this for
        every user before any join blocks, so no user's listener is still
        running unsignaled while another user's threads are being joined."""
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        self.run = False

        lastPlayedManager = getattr(self.sp, "lastPlayedManager", None)
        if lastPlayedManager is not None:
            manager = getattr(lastPlayedManager, "manager", None)
            if manager is not None:
                # Tell the patched keep_alive/updateLoop (Database.patches) the
                # coming close is intentional so they exit instead of reconnecting.
                try:
                    manager._deliberate_close = True
                except AttributeError:
                    pass  # __slots__-only instance; the patches treat a missing flag as False
            lastPlayedManager.run = False

    def stop(self):
        self.signalStop()

        # Also stop spotapi's own background LastPlayed thread (started via
        # startRecentlyPlayedListener). Left running, it can hit a rate-limited or
        # malformed response mid-request while the interpreter is shutting down,
        # producing spurious errors. Close the websocket connection so the
        # keep_alive thread doesn't try to send pings on a closed connection.
        lastPlayedManager = getattr(self.sp, "lastPlayedManager", None)
        if lastPlayedManager is not None:
            manager = getattr(lastPlayedManager, "manager", None)
            if manager is not None:
                ws = getattr(manager, "ws", None)
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass  # Connection may already be closed

            # Give the keep_alive thread a moment to detect the closed connection
            # and exit gracefully before we join it
            time.sleep(0.1)
            thread = getattr(lastPlayedManager, "thread", None)
            if thread is not None and thread.is_alive():
                thread.join(timeout=LISTENER_STOP_JOIN_TIMEOUT_SECONDS)

        # Bounded join on the listener thread itself to wait for clean exit.
        # Skip the join if called from within the listener thread itself to avoid
        # "cannot join current thread" error (happens when onStale() callback
        # invokes reconnection from within the listener thread).
        if hasattr(self, "thread") and self.thread.is_alive() and threading.current_thread() != self.thread:
            self.thread.join(timeout=LISTENER_STOP_JOIN_TIMEOUT_SECONDS)
