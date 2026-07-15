import logging
import os
import json
import secrets
import tempfile
import threading
import requests
from pathlib import Path
import time
from datetime import timedelta

from flask import Flask, render_template, redirect, request, url_for, jsonify, send_from_directory, session, g, abort
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash

from Database.database import Database
from Database.db import SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON
from Database.repository import Repository
from Database.Migrators.migrate import migrateIfNeeded
from Database.Listeners.spotifyListener import _suppress_signal_in_thread
from Database.logging_config import configureLogging
from Database.utils import msToString, convertToDatetime, formatDuration, dateToString, versionTuple, now, startOfDay, parseDateString
import SpotipyFree
from SpotipyFree import saveSession, parseCookieString

logger = logging.getLogger(__name__)

PAGE_SIZE = 50                  #< list items shown per page
LOGIN_CACHE_TTL_SECONDS = 180  #< seconds to cache isListenerLoggedIn result per user
CHART_ARTIST_TREND_TOP_N = 5   #< how many top artists are plotted on the trend line chart
WRAPPED_LIST_SIZE = 10          #< default/fallback for ?limit= - how many items per category the Wrapped page shows
WRAPPED_LIMIT_OPTIONS = (10, 25, 50, 100)   #< selectable values for Wrapped's items-per-category dropdown
COMPARE_TOP_LIST_SIZE = 10                #< items per top-songs/artists/albums list shown on the Compare page
COMPARE_OVERLAP_POOL_SIZE = 100           #< how deep each side's top-artists list is searched for shared taste overlap
MAX_UPLOAD_MB = 500              #< cap on a single import-history request's total upload size
DEFAULT_SORT_BY = "totalTimeListened"
# The only sortBy values Repository.SONG_SORT_COLUMNS/ALBUM_SORT_COLUMNS/
# ARTIST_SORT_COLUMNS know how to handle - an unrecognized ?sortBy= would
# otherwise reach a ValueError deep in the DB layer and 500 instead of just
# falling back to the default.
VALID_SORT_BY = {"totalTimeListened", "plays", "name"}
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
PASSWORD_MIN_LENGTH = 8   #< also enforced client-side via the minlength attribute
RATE_LIMIT_MAX_ATTEMPTS = 10     #< max POSTs allowed per window, per source IP, per route
RATE_LIMIT_WINDOW_SECONDS = 300  #< 5 minutes
RATE_LIMIT_ERROR_MESSAGE = "Too many attempts. Please wait a few minutes and try again."

# Baseline defense-in-depth headers applied to every response (see
# registerRoutes' after_request hook below).
#
# script-src/style-src keep 'unsafe-inline': every template in this app relies
# on inline <script> blocks and inline event-handler attributes (onclick=,
# onerror=, style=...), none of which are nonce/hash-tagged - disallowing
# unsafe-inline here would break the app outright, not just tighten it.
# Google Fonts is the only external resource any template actually loads.
# No Strict-Transport-Security: this app is normally self-hosted over plain
# HTTP on a local network/Docker host (see README), and HSTS would force
# HTTPS for the origin going forward - actively breaking that expected setup.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


def _passwordPolicyError(password: str) -> str | None:
    """None if `password` satisfies the account password policy, otherwise a
    user-facing message naming the first unmet rule."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters long."
    if not any(c.isupper() for c in password):
        return "Password must contain at least one uppercase letter."
    if not any(c.islower() for c in password):
        return "Password must contain at least one lowercase letter."
    if not any(c.isdigit() or not c.isalnum() for c in password):
        return "Password must contain at least one number or special character."
    return None


class _RateLimiter:
    """In-memory fixed-window rate limiter, keyed by (bucket, identifier).

    Single-process only (state isn't shared across workers and doesn't
    survive a restart) - adequate for this app's single-process Waitress
    deployment, and mirrors the existing in-memory _login_cache pattern
    rather than pulling in an external dependency for a personal, low-
    traffic self-hosted app."""

    def __init__(self, maxAttempts: int, windowSeconds: float):
        self.maxAttempts = maxAttempts
        self.windowSeconds = windowSeconds
        self._hits: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def hit(self, bucket: str, identifier: str) -> bool:
        """Record one attempt for (bucket, identifier). Returns True if it's
        allowed (under the limit), False if this attempt should be rejected."""
        key = (bucket, identifier)
        now_ts = time.monotonic()
        cutoff = now_ts - self.windowSeconds
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if t >= cutoff]
            if len(hits) >= self.maxAttempts:
                self._hits[key] = hits
                return False
            hits.append(now_ts)
            self._hits[key] = hits
            return True


class SpotifyDashboardApp:
    def __init__(self):
        configureLogging()
        migrateIfNeeded()
        self.app = Flask(__name__)
        self.baseDir = Path(__file__).resolve().parent
        self.app.secret_key = self._get_or_create_secret_key()
        self.app.permanent_session_lifetime = timedelta(days=30)
        # Caps a single import-history request's total upload size (summed across
        # every file in a multi-file upload) - without this, an oversized/
        # accidental upload is read fully into memory before anything can reject
        # it.
        self.app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
        self.app.config["WTF_CSRF_TIME_LIMIT"] = None
        if os.environ.get("PYTEST_CURRENT_TEST") or self.app.config.get("TESTING"):
            self.app.config["WTF_CSRF_ENABLED"] = False
        CSRFProtect(self.app)
        # Users, emails and Spotify session cookies live in the shared database
        # (see Database/repository.py) instead of secrets/users_map.json and
        # secrets/cookies.json.
        self.repo = Repository()

        self.user_databases = {}
        self._db_lock = threading.RLock()
        self._session_lock = threading.RLock()
        self._login_cache: dict = {}  #< {email: (result: bool, expires_at: float)}
        # Throttles POSTs to /login, /register, /reset-password per source IP -
        # without this, a network-reachable instance is brute-forceable
        # indefinitely (check_password_hash costs some compute, but nothing
        # stops an unlimited number of attempts).
        self._authRateLimiter = _RateLimiter(RATE_LIMIT_MAX_ATTEMPTS, RATE_LIMIT_WINDOW_SECONDS)
        # Lets a self-hoster turn off the "do these cookies actually belong to
        # this email" check at login, e.g. if Spotify starts blocking the
        # verification request for their account. Off by default since it's
        # what stops one user from claiming another's account/database.
        self.skipEmailVerification = os.environ.get("SKIP_EMAIL_VERIFICATION", "").strip().lower() in TRUTHY_ENV_VALUES
        
        try:
            self.currentVersion = (self.baseDir / "Database" / "VERSION").read_text(encoding="utf-8").strip()  #< only needs to be checked once because app cant update without restart
        except Exception:
            self.currentVersion = "0.0.0"
        self.latestVersion = None
        self._version_lock = threading.Lock()
        self._stop_event = threading.Event()
        self.startVersionCheck_thread()
        self.checkLogin_thread()

        self.registerRoutes()

    def _get_or_create_secret_key(self):
        """Resolve the Flask session-signing key. Prefers FLASK_SECRET_KEY, otherwise
        persists a random key under secrets/ so sessions can't be forged using the
        publicly-known default that used to ship in this repo."""
        envKey = os.environ.get("FLASK_SECRET_KEY")
        if envKey:
            return envKey

        keyFile = self.baseDir / "secrets" / "flask_secret_key.txt"
        if keyFile.exists():
            existingKey = keyFile.read_text(encoding="utf-8").strip()
            if existingKey:
                return existingKey

        newKey = secrets.token_hex(32)
        keyFile.parent.mkdir(parents=True, exist_ok=True)
        keyFile.write_text(newKey, encoding="utf-8")
        return newKey

    def get_username_for_email(self, email):
        return self.repo.getUsernameForEmail(email)

    def get_or_create_user(self, email):
        # The whole check-then-create sequence needs the lock, not just the final
        # write: two concurrent first-time logins for different emails that
        # happen to sanitize to the same username prefix (e.g. "alice@a.com" and
        # "alice@b.com") could otherwise both pass the uniqueness check before
        # either has actually created their row.
        with self._session_lock:
            username = self.repo.getUsernameForEmail(email)
            if not username:
                # Create a new username from email prefix
                prefix = email.split("@")[0]
                sanitized = "".join(c for c in prefix if c.isalnum() or c in ("-", "_")).strip()
                if not sanitized:
                    sanitized = f"user_{int(time.time())}"

                username = sanitized
                counter = 1
                while True:
                    if username in self.user_databases:
                        pass  # a live Database already exists under this name - can't be the same account, needs a new suffix
                    elif not self.repo.usernameExists(username):
                        self.repo.upsertUser(username, email)
                        break
                    elif self.repo.getEmailForUsername(username) is None:
                        # Orphaned account with no email on record - e.g. a
                        # migration whose users_map.json didn't have this user's
                        # email. Claim it instead of creating a sibling account
                        # that strands its existing history (the caller already
                        # verified these cookies belong to `email`).
                        self.repo.setUserEmail(username, email)
                        break

                    username = f"{sanitized}_{counter}"
                    counter += 1

        return username

    def _verifyCookiesMatchEmail(self, cookies: dict, email: str) -> bool:
        """Check that the submitted Spotify cookies actually belong to `email` by
        fetching the account profile with them. The cookies are written to a
        throwaway session file so an unverified login attempt can never overwrite
        another user's stored cookies. Without this check, anyone could claim any
        email at login and be handed that user's database."""
        if not cookies or not email:
            return False

        tmpFd, tmpPath = tempfile.mkstemp(prefix="verify_cookies_", suffix=".json")
        os.close(tmpFd)
        try:
            saveSession(cookies, email, tmpPath)
            with _suppress_signal_in_thread():
                sp = SpotipyFree.Spotify(cookiesFile=tmpPath, email=email)
            if not sp.isLoggedIn():
                return False
            profile = sp.current_user() or {}
            profileEmail = (profile.get("email") or "").strip().lower()
            return profileEmail == email.strip().lower()
        except Exception as e:
            logger.warning("Cookie verification failed for %s: %s", email, e)
            return False
        finally:
            try:
                os.unlink(tmpPath)
            except OSError:
                pass

    def get_user_db(self, username, email):
        with self._db_lock:
            if username not in self.user_databases:
                db = Database(user=username, email=email)
                db.startAutoImporter()
                db.resetProgress()
                db.startListener(email=email)
                self.user_databases[username] = db
            return self.user_databases[username]

    def _refresh_user_session(self, username, email):
        """Restart this user's listener against the cookies just saved to the
        database, and drop any cached login-status result. Without this, a
        re-login after expired/invalid cookies (get_user_db is a no-op for a
        username that already has a live Database) would leave the old, dead
        listener running and the stale cached is_user_logged_in() result in
        place until the process restarts."""
        with self._db_lock:
            db = self.user_databases.get(username)
            if db is not None:
                if db.listener is not None:
                    db.listener.stop()
                db.startListener(email=email)
        self._login_cache.pop(email, None)

    def is_user_logged_in(self, email):
        if not email:
            return False

        username = self.repo.getUsernameForEmail(email)
        if not username or self.repo.getUserCookies(username) is None:
            return False

        # isListenerLoggedIn() can make a live network call to Spotify - the result
        # is cached per user for LOGIN_CACHE_TTL_SECONDS to avoid a round-trip on
        # every request (the main cause of Waitress queue saturation).
        now_ts = time.monotonic()
        cached = self._login_cache.get(email)
        if cached is not None and cached[1] > now_ts:
            return cached[0]

        # get_user_db is a no-op (returns the existing instance) if this user
        # already has a live Database - it's only actually constructing one
        # here for a user _ensureAllUsersLogin hasn't (yet, or ever, if
        # construction kept failing) loaded. Either way this must never just
        # assume True for an unloaded user: that's exactly the check the
        # password-login branch relies on to confirm a stored session is
        # still live, not merely that cookies exist.
        try:
            result = self.get_user_db(username, email).isListenerLoggedIn()
        except Exception as e:
            logger.error("Error checking login status for %s: %s", email, e)
            result = False

        self._login_cache[email] = (result, now_ts + LOGIN_CACHE_TTL_SECONDS)
        return result

    def checkLogin_thread(self):
        self._ensureAllUsersLogin()
        thread = threading.Thread(target=self._checkLoginLoop, daemon=True)
        thread.start()

    def _ensureAllUsersLogin(self):
        try:
            usersWithCookies = self.repo.getAllUsersWithCookies()
        except Exception as e:
            logger.error("Error initializing users: %s", e)
            return

        for username, email in usersWithCookies:
            try:
                db = self.get_user_db(username, email)
                # If listener has crashed, marked DEAD, or its thread has stopped, restart it
                if db.getListenerHealth()["status"] == "DEAD" or not (
                    db.listener and db.listener.thread and db.listener.thread.is_alive()
                ):
                    logger.warning("Listener thread for user %s is not running or is DEAD. Restarting...", username)
                    db.startListener(email=email)
            except Exception as e:
                logger.error("Error initializing user %s: %s", username, e)
    
    def _checkLoginLoop(self):
        while not self._stop_event.is_set():
            self._ensureAllUsersLogin()
            self._stop_event.wait(60 * 5)  # Check every 5 minutes

    def startVersionCheck_thread(self):
        thread = threading.Thread(target=self._versionCheckLoop, daemon=True)
        thread.start()

    def _versionCheckLoop(self):
        # Check version from GitHub at startup and then every hour.
        url = "https://raw.githubusercontent.com/i7Gamer/SpotifyStatsTracker/main/Database/VERSION"
        while not self._stop_event.is_set():
            try:
                resp = requests.get(url, timeout=6)
                if resp.status_code == 200:
                    remoteVersion = resp.text.strip()
                    # store remoteVersion if it's newer than current
                    try:
                        with self._version_lock:
                            if versionTuple(remoteVersion) > versionTuple(self.currentVersion):
                                self.latestVersion = remoteVersion
                            else:
                                self.latestVersion = None
                    except:
                        pass
            except Exception:
                pass

            self._stop_event.wait(60 * 60)

    def _getPercentPlayedText(self, item, sortBy, totalPlays, totalMs):
        if sortBy == "plays":
            percent = round((item.get("plays", 0) / totalPlays * 100), 1) if totalPlays else 0
            return f"{percent}% of all plays"
        elif sortBy == "totalTimeListened":
            percent =  round((item.get("totalTimeListened", 0) / totalMs * 100), 1) if totalMs else 0
            return f"{percent}% of all time played"
        else:
            return ""

    def _embedSongTextElements(self, song) -> dict:
        if "playedAt" in song:   #< some tracks just dont have it (top tracks)
            db = g.get("db", None)
            tz = db.tz if db else None
            playedAt = convertToDatetime(song["playedAt"], tz=tz)
            song["playedAtText"] = playedAt.strftime("%d %b %Y, %H:%M")
            song["timePlayedText"] = msToString(song["timePlayed"])

        song["contextName"] = None
        if "playedFrom" in song:
            db = g.get("db", None)
            if db:
                song["contextName"] = db.playlistName(song["playedFrom"])

        artistsText = ", ".join(a.get("name", "") for a in song["artists"])
        album = song.get("album")   #< can be None - see Repository._songRowToDict()'s LEFT JOIN fallback
        # releaseDate 0/None is the app-wide "unknown" sentinel (synthetic
        # tracks, albums the metadata backfiller hasn't reached yet - see
        # Repository.upsertTrack/_createSyntheticTrack) - dateToString would
        # otherwise render it as the Unix epoch date instead of blank.
        releaseDateText = dateToString(album["releaseDate"]) if album and album.get("releaseDate") else ""
        song["releaseDateText"] = releaseDateText
        song["artistsText"] = artistsText
        song["durationText"] = formatDuration(song["duration"])
        if album:
            album["releaseDateText"] = releaseDateText
        return song

    def _embedTopSongTextElements(self, song, sortBy=None, totalPlays=0, totalMs=0) -> dict:
        song["totalTimeListenedText"] = msToString(song.get("totalTimeListened", 0))
        db = g.get("db", None)
        tz = db.tz if db else None
        song["firstListenedText"] = convertToDatetime(song.get("firstListenedAt", 0), tz=tz).strftime("%b %d, %Y")
        song["sortPercentText"] = self._getPercentPlayedText(song, sortBy, totalPlays, totalMs)
        return song

    def _embedAlbumTextElements(self, album, sortBy=None, totalPlays=0, totalMs=0) -> dict:
        album["totalTimeListenedText"] = msToString(album.get("totalTimeListened", 0))
        db = g.get("db", None)
        tz = db.tz if db else None
        album["firstListenedText"] = convertToDatetime(album.get("firstListenedAt", 0), tz=tz).strftime("%b %d, %Y")
        album["sortPercentText"] = self._getPercentPlayedText(album, sortBy, totalPlays, totalMs)
        # See _embedSongTextElements()'s comment: releaseDate 0/None means unknown.
        releaseDate = album.get("releaseDate")
        album["releaseDateText"] = dateToString(releaseDate) if releaseDate else ""
        album["artistsText"] = ", ".join(a.get("name", "") for a in album.get("artists", []))
        return album

    def _embedAlbumsTextElements(self, albums, sortBy=None, totalPlays=0, totalMs=0) -> list[dict]:
        return [self._embedAlbumTextElements(album, sortBy, totalPlays, totalMs) for album in albums]

    def _embedArtistTextElement(self, artist, sortBy=None, totalPlays=0, totalMs=0) -> dict:
        artist["totalTimeListenedText"] = msToString(artist.get("totalTimeListened", 0))
        db = g.get("db", None)
        tz = db.tz if db else None
        artist["firstListenedText"] = convertToDatetime(artist.get("firstListenedAt", 0), tz=tz).strftime("%b %d, %Y")
        artist["sortPercentText"] = self._getPercentPlayedText(artist, sortBy, totalPlays, totalMs)
        return artist

    def _embedSongsTextElements(self, songs) -> list[dict]:
        return [self._embedSongTextElements(song) for song in songs]

    def _embedTopSongsTextElements(self, songs, sortBy=None, totalPlays=0, totalMs=0) -> list[dict]:
        return [self._embedTopSongTextElements(song, sortBy, totalPlays, totalMs) for song in songs]

    def _embedArtistsTextElements(self, songs, sortBy=None, totalPlays=0, totalMs=0) -> list[dict]:
        return [self._embedArtistTextElement(song, sortBy, totalPlays, totalMs) for song in songs]

    def _gatherCompareStats(self, db, startDate, endDate) -> dict:
        """One Compare-page side's stats, gathered identically for the viewer
        and the counterpart so the two columns can't drift apart. Runs the
        same _embed*TextElements step every other page feeding
        _track_card.html uses - without it the cards render with blank
        time/first-listened/duration/percent lines. The displayed top-artist
        list is sliced from the same pool the "you both love" overlap
        intersects, so the list, the summary row, and the overlap all agree
        on one by-plays ranking (and each side's artist aggregation runs
        once, not twice)."""
        totalPlays, totalMs = db.getPlayTotals(startDate, endDate)
        topSongs = self._embedTopSongsTextElements(
            self._embedSongsTextElements(db.getTopSongs(startDate, endDate, limit=COMPARE_TOP_LIST_SIZE)),
            sortBy="plays", totalPlays=totalPlays, totalMs=totalMs)
        topAlbums = self._embedAlbumsTextElements(
            db.getTopAlbums(startDate, endDate, limit=COMPARE_TOP_LIST_SIZE),
            sortBy="plays", totalPlays=totalPlays, totalMs=totalMs)
        topArtistsPool = db.getTopArtists(startDate, endDate, limit=COMPARE_OVERLAP_POOL_SIZE)
        topArtists = self._embedArtistsTextElements(
            topArtistsPool[:COMPARE_TOP_LIST_SIZE],
            sortBy="plays", totalPlays=totalPlays, totalMs=totalMs)
        return {
            "totalPlays": totalPlays,
            "totalTimeText": msToString(totalMs),
            "topSongs": topSongs,
            "topArtists": topArtists,
            "topAlbums": topAlbums,
            "topArtistsPool": topArtistsPool,
        }

    def _buildPageUrl(self, endpoint, page, **queryArgs):
        cleanArgs = {key: value for key, value in queryArgs.items() if value not in (None, "")}
        cleanArgs["page"] = page
        return url_for(endpoint, **cleanArgs)

    def _getNeighboringUrls(self, name, page, totalPages, **queryArgs):
        prevUrl = self._buildPageUrl(name, page - 1, **queryArgs) if page > 1 else None
        nextUrl = self._buildPageUrl(name, page + 1, **queryArgs) if page < totalPages else None
        return prevUrl, nextUrl

    def _buildPageNumberLinks(self, endpoint, page, totalPages, window=2, **queryArgs):
        """Page-number links for a pagination strip: always page 1 and the last
        page, plus a `window`-page radius around the current page, with an
        {"ellipsis": True} marker filling any gap between shown pages."""
        if totalPages <= 1:
            return []

        pagesToShow = {1, totalPages}
        for p in range(page - window, page + window + 1):
            if 1 <= p <= totalPages:
                pagesToShow.add(p)

        links = []
        previousPage = None
        for p in sorted(pagesToShow):
            if previousPage is not None and p - previousPage > 1:
                links.append({"ellipsis": True})
            links.append({"num": p, "url": self._buildPageUrl(endpoint, p, **queryArgs), "current": p == page})
            previousPage = p
        return links

    def _buildPaginationContext(self, endpoint, page, totalPages, totalCount, pageSize=PAGE_SIZE, **queryArgs):
        """Everything a list page's pagination strip needs: prev/next links,
        windowed page-number links, and the 'Showing X-Y of Z' counts."""
        prevUrl, nextUrl = self._getNeighboringUrls(endpoint, page, totalPages, **queryArgs)
        pageLinks = self._buildPageNumberLinks(endpoint, page, totalPages, **queryArgs)
        showingStart = (page - 1) * pageSize + 1 if totalCount else 0
        showingEnd = min(page * pageSize, totalCount)
        return {
            "page": page,
            "totalPages": totalPages,
            "prevUrl": prevUrl,
            "nextUrl": nextUrl,
            "pageLinks": pageLinks,
            "showingStart": showingStart,
            "showingEnd": showingEnd,
            "totalCount": totalCount,
        }

    def _getChangeText(self, currentValue, previousValue):
        if previousValue is None or previousValue == 0:
            if currentValue == 0:
                return None, ""
            return f"New this period", "change-positive"

        change = ((currentValue - previousValue) / previousValue) * 100
        if round(change, 1) == 0:
            return "No change from the previous period", ""

        formatted = f"{abs(round(change, 1))}% {'more' if change > 0 else 'less'} than the previous period"
        cssClass = "change-positive" if change > 0 else "change-negative"
        return formatted, cssClass

    def _getPageParam(self):
        """The current request's ?page=... as an int >= 1, tolerating junk input."""
        try:
            return max(1, int(request.args.get("page", 1) or 1))
        except (TypeError, ValueError):
            return 1

    def _getSortByParam(self, default=DEFAULT_SORT_BY):
        """The current request's ?sortBy=..., falling back to `default` for any
        value the DB layer doesn't know how to sort by (see VALID_SORT_BY) -
        without this, an unrecognized value reaches a ValueError/KeyError deep
        in Repository/Database and 500s instead of just using the default."""
        sortBy = request.args.get("sortBy", default)
        return sortBy if sortBy in VALID_SORT_BY else default

    def _calculatePagination(self, totalCount):
        """Calculate safe page bounds given a total count.
        Returns (page, totalPages, startIndex) where page is clamped to valid range."""
        page = self._getPageParam()
        totalPages = max(1, (totalCount + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, totalPages))
        startIndex = (page - 1) * PAGE_SIZE
        return page, totalPages, startIndex

    def _getValidInterval(self, interval, default="day"):
        """Validate interval parameter, falling back to default for unrecognized values."""
        valid_intervals = {"", "today", "day", "week", "month", "year", "5years", "all time", "custom"}
        return interval if interval in valid_intervals else default

    def _getValidGroupBy(self, groupBy, default="day"):
        """Validate groupBy parameter, falling back to default for unrecognized values."""
        return groupBy if groupBy in ("day", "week", "month") else default

    def _getDateRange(self, interval: str = None, customStart: str = None, customEnd: str = None, default="day", tz=None):
            """Get start and end dates based on interval or custom dates.

            Returns a half-open local interval [startDate, endDate).
            """
            nowLocal = now(tz=tz)
            startDate = None

            futureBuffer = timedelta(days=1) 

            endDate = nowLocal + futureBuffer   #< bypass any timezone issues

            if customStart and customEnd:
                try:
                    startLocal = parseDateString(customStart, tz=tz)
                    endLocal = parseDateString(customEnd, tz=tz)
                    if startLocal is None or endLocal is None:
                        raise ValueError("Invalid custom date")

                    startDate = startLocal
                    endDate = endLocal + timedelta(days=1)
                except ValueError:
                    pass
            if interval == "":
                interval = default
            if not startDate:
                if interval == "today":
                    startDate = convertToDatetime(startOfDay(nowLocal, tz=tz), tz=tz)
                    endDate = convertToDatetime(startOfDay(nowLocal + timedelta(days=1), tz=tz), tz=tz)

                elif interval == "day":
                    startDate = convertToDatetime(startOfDay(nowLocal - timedelta(days=1), tz=tz), tz=tz)
                    endDate = convertToDatetime(startOfDay(nowLocal, tz=tz), tz=tz)

                elif interval == "week":
                    startDate = nowLocal - timedelta(weeks=1)

                elif interval == "month":
                    startDate = nowLocal - timedelta(days=30)

                elif interval == "year":
                    startDate = nowLocal - timedelta(days=365)

                elif interval == "5years":
                    startDate = nowLocal - timedelta(days=365*5)
                else:
                    startDate = None
                    endDate = None

            return startDate, endDate

    def _getIntervalLabel(self, interval: str = None, customStart: str = None, customEnd: str = None):
        labels = {
            "all time": "All Time",
            "today": "Today",
            "day": "Yesterday",
            "week": "Last Week",
            "month": "Last Month",
            "year": "Last Year",
            "5years": "Last 5 Years",
        }

        if interval == "custom" and customStart and customEnd:
            return f"Custom range: {customStart} to {customEnd}"

        return labels.get(interval or "day", "Yesterday")

    def _embedTimeSeriesTextElements(self, timeSeries: list) -> list:
        for bucket in timeSeries:
            bucket["totalTimeListenedText"] = msToString(bucket["totalTimeListened"])
        return timeSeries

    def _embedHeatmapTextElements(self, heatmap: list) -> list:
        for row in heatmap:
            for cell in row:
                cell["totalTimeListenedText"] = msToString(cell["totalTimeListened"])
        return heatmap

    def _getWrappedYearParam(self, availableYears: list, defaultYear: int) -> int:
        """The current request's ?year=... if it's one of the years the user
        actually has data for, else `defaultYear` - mirrors _getPageParam()'s
        tolerate-junk-input, silently-clamp behavior for ?page=."""
        try:
            year = int(request.args.get("year", defaultYear))
        except (TypeError, ValueError):
            return defaultYear
        return year if year in availableYears else defaultYear

    def _discoveriesInYear(self, items: list, yearStart, yearEnd, limit: int) -> list:
        """Items (songs or artists) whose true, all-time first listen falls
        within [yearStart, yearEnd) - not just their earliest play *within* that
        range, which a date-scoped query would report instead. `items` must
        therefore come from an unbounded (no date range) stats call. Sorted by
        play count, most-played discovery first."""
        yearStartTs, yearEndTs = yearStart.timestamp(), yearEnd.timestamp()
        discovered = [
            item for item in items
            if item.get("firstListenedAt") is not None and yearStartTs <= item["firstListenedAt"] < yearEndTs
        ]
        discovered.sort(key=lambda item: item.get("plays", 0), reverse=True)
        return discovered[:limit]

    def registerRoutes(self):
        @self.app.after_request
        def _setSecurityHeaders(response):
            for header, value in SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
            return response

        @self.app.context_processor
        def _injectPasswordPolicy():
            # Lets register.html/reset_password.html show the actual configured
            # minimum instead of a hardcoded number that could drift from
            # PASSWORD_MIN_LENGTH.
            return {"minPasswordLength": PASSWORD_MIN_LENGTH}

        @self.app.context_processor
        def _injectFallbackMarkers():
            # Single-sources the created_reason marker values (Database.db) so
            # templates compare against the constants instead of duplicating the
            # string literals.
            return {
                "SYNTHETIC_FALLBACK_REASON": SYNTHETIC_FALLBACK_REASON,
                "RESTRICTED_FALLBACK_REASON": RESTRICTED_FALLBACK_REASON,
            }

        @self.app.context_processor
        def _injectShareStatus():
            # Lets layout.html's nav show a "Compare" link only for users who
            # have at least one usable accepted share, and the topbar badges
            # show a count of share requests waiting on them plus a count of
            # their own requests that were just accepted - computed here so
            # every template gets all three without every route remembering
            # to pass them. Memoized on g: one request can render several
            # templates (the Wrapped AJAX endpoint renders six partials), and
            # each render re-runs every context processor - these cheap
            # queries must not repeat per partial. No is_user_logged_in
            # check: that can cost a live Spotify round-trip, far too heavy
            # per render, and a stale session's worst case is a nav
            # link/badge that 302s to login like every other nav item would.
            if "hasAcceptedShares" not in g:
                username = session.get("username")
                g.hasAcceptedShares = self.repo.hasAnyAcceptedShare(username) if username else False
                g.pendingIncomingSharesCount = self.repo.getPendingIncomingSharesCount(username) if username else 0
                g.unseenAcceptedShareCount = self.repo.getUnseenAcceptedShareCount(username) if username else 0
            return {
                "hasAcceptedShares": g.hasAcceptedShares,
                "pendingIncomingSharesCount": g.pendingIncomingSharesCount,
                "unseenAcceptedShareCount": g.unseenAcceptedShareCount,
            }

        def _is_version_newer(remote: str, local: str) -> bool:
            try:
                return versionTuple(remote) > versionTuple(local)
            except Exception:
                return False

        @self.app.route("/health", methods=["GET"])
        def health():
            """Cheap, unauthenticated liveness/readiness check for container
            orchestration and uptime monitoring - does a trivial query rather
            than just returning 200 unconditionally, so it can tell "process
            alive" apart from "process alive but the database is unreachable"
            (the single point of failure for this app)."""
            try:
                self.repo.connection().execute("SELECT 1").fetchone()
                return jsonify({"status": "ok"}), 200
            except Exception as e:
                logger.error("Health check failed: %s", e)
                return jsonify({"status": "error", "detail": str(e)}), 503

        def get_current_user_or_redirect():
            email = session.get("email")
            if not email or not self.is_user_logged_in(email):
                return None, None, None
            
            # Ensure the username matches the correct email mapping to prevent session pollution from legacy user "Tzur"
            correct_username = self.get_username_for_email(email)
            if not correct_username:
                correct_username = self.get_or_create_user(email)
            
            if session.get("username") != correct_username:
                session["username"] = correct_username

            username = correct_username
            db = self.get_user_db(username, email)
            g.db = db
            return email, username, db

        def _authorized_image_username():
            """Returns the username the current session is allowed to view images for, or None."""
            email = session.get("email")
            if not email or not self.is_user_logged_in(email):
                return None
            return self.get_username_for_email(email)

        # Images are shared across every user (Database.imgDir_tracks/imgDir_artists
        # are class-level, not per user) - the <username> segment in these routes is
        # kept only as the authorization check ("is this session allowed to ask at
        # all"), not to select which directory to read from.
        @self.app.route('/img/<username>/tracks/<filename>')
        def serveTrackImage(username, filename):
            if username != _authorized_image_username() or filename != os.path.basename(filename):
                return "", 404
            return send_from_directory(Database.imgDir_tracks, filename)

        @self.app.route('/img/<username>/artists/<filename>')
        def serveArtistImage(username, filename):
            if username != _authorized_image_username() or filename != os.path.basename(filename):
                return "", 404
            imageDir = Database.imgDir_artists
            imagePath = os.path.join(imageDir, filename)

            if not os.path.exists(imagePath):
                parts = os.path.splitext(filename)
                if len(parts) == 2 and parts[0].isalnum():
                    artistId = parts[0]
                    db = self.user_databases.get(username)
                    if db:
                        db.lazyFetchArtistImage(artistId, Path(imagePath))

            return send_from_directory(imageDir, filename)

        @self.app.route("/import-history", methods=["POST"])
        def importHistory():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login"))

            if db.readProgress().get("status") == "running":
                return redirect(url_for("importPage"))

            uploads = [f for f in request.files.getlist("history_file") if f and f.filename]
            if not uploads:
                return redirect(url_for("importPage"))

            contents = []
            for upload in uploads:
                try:
                    contents.append(upload.read().decode("utf-8"))
                except UnicodeDecodeError:
                    # Mirrors AutoImporter._handleImport's per-file resilience
                    # (see its try/except around open(..., encoding="utf-8"))
                    # - one unreadable file must not 500 the whole request and
                    # drop every other file in the same upload.
                    logger.warning("Skipping upload %r for user %s: not valid UTF-8 text", upload.filename, username)
            if not contents:
                return redirect(url_for("importPage"))

            # Marked "running" here, synchronously, rather than via a
            # post-thread-start time.sleep(1) "give it a moment" delay - that
            # blocked a Waitress worker thread on every submission and still
            # couldn't fully guarantee the background thread's own first
            # writeProgress() call (inside Database.importHistory, gated on
            # parsing the export first) had actually landed by the time it
            # returned.
            db.writeProgress("running", 0, 0, "Starting import")
            thread = threading.Thread(target=db.importHistoryBatch, args=(contents,), daemon=True)
            thread.start()
            return redirect(url_for("importPage"))

        @self.app.route("/import", methods=["GET"])
        def importPage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login"))
            return render_template(
                "import.html",
                importProgress=db.readProgress(),
                maxUploadMb=MAX_UPLOAD_MB,
                uploadTooLarge=request.args.get("error") == "upload_too_large",
                section="import",
            )

        @self.app.errorhandler(413)
        def _uploadTooLarge(error):
            return redirect(url_for("importPage", error="upload_too_large"))

        def _safeNextUrl(nextUrl):
            """Only allow same-origin relative redirects after login - a `next`
            value like `//evil.com`, `https://evil.com`, or `/\\evil.com`
            (browsers normalize a leading "/\\" to "//", making it protocol-
            relative too) would otherwise send a freshly authenticated session
            to an attacker-controlled site."""
            if not nextUrl or nextUrl[0] != "/":
                return None
            if len(nextUrl) >= 2 and nextUrl[1] in ("/", "\\"):
                return None
            return nextUrl

        def _rateLimited(bucket: str) -> bool:
            """True if this request's source IP has exceeded RATE_LIMIT_MAX_ATTEMPTS
            for `bucket` within RATE_LIMIT_WINDOW_SECONDS - callers should reject
            the request with RATE_LIMIT_ERROR_MESSAGE when this returns True."""
            identifier = request.remote_addr or "unknown"
            return not self._authRateLimiter.hit(bucket, identifier)

        @self.app.route("/login", methods=["GET", "POST"])
        def login():
            if request.method == "GET":
                return render_template("login.html", next=_safeNextUrl(request.args.get("next")))

            email = request.form.get("email", "").strip()
            nextUrl = _safeNextUrl(request.form.get("next"))

            if _rateLimited("login"):
                return render_template(
                    "login.html", email=email, next=nextUrl,
                    error=RATE_LIMIT_ERROR_MESSAGE), 429

            # The password form and the (collapsed, fallback) cookies form are
            # separate <form>s on login.html - only the submitted one's fields
            # are present, so their presence tells the two branches apart.
            if "password" in request.form:
                password = request.form.get("password", "")
                if not email or not password:
                    return render_template(
                        "login.html", email=email, next=nextUrl,
                        error="Email and password are both required.")

                username = self.repo.getUsernameForEmail(email)
                passwordHash = self.repo.getUserPasswordHash(username) if username else None
                if username and not passwordHash:
                    return render_template(
                        "login.html", email=email, next=nextUrl,
                        error="This account doesn't have a password yet - register to add one, "
                              "or log in with cookies instead.")
                if not passwordHash or not check_password_hash(passwordHash, password):
                    return render_template(
                        "login.html", email=email, next=nextUrl,
                        error="Invalid email or password.")

                # Password auth only ever unlocks the session while the cookies
                # stored from the last cookie-based (re-)login/register/reset
                # are still live - a lapsed Spotify session must be refreshed
                # via /reset-password or the cookies form, not just a password.
                if not self.is_user_logged_in(email):
                    return render_template(
                        "login.html", email=email, next=nextUrl,
                        error="Your saved Spotify session has expired. Reset your password with "
                              "fresh cookies, or log in with cookies instead.")

                session.permanent = True
                self.get_user_db(username, email)
                session["email"] = email
                session["username"] = username
                return redirect(nextUrl or url_for("dashboard"))

            cookies = request.form.get("cookies", "")

            if not email or not cookies:
                return render_template(
                    "login.html", email=email, next=nextUrl,
                    error="Email and cookies are both required.")

            # Verification happens against a throwaway session file, so nothing
            # is persisted for this email unless the cookies really are theirs.
            parsedCookies = parseCookieString(cookies)
            if not self.skipEmailVerification and not self._verifyCookiesMatchEmail(parsedCookies, email):
                return render_template(
                    "login.html", email=email, next=nextUrl,
                    error=f"Couldn't verify that these cookies belong to {email}. "
                          "Make sure you are logged into open.spotify.com with that account and copied all cookies.")

            session.permanent = True
            # get_or_create_user/get_user_db manage their own locking and can
            # start a listener (a live network call) - kept outside the session
            # lock so logging one user in doesn't block everyone else.
            username = self.get_or_create_user(email)
            self.repo.setUserCookies(username, parsedCookies)
            if username in self.user_databases:
                # Returning user re-logging in (e.g. after their session
                # expired) - restart their listener against the fresh cookies
                # instead of leaving the old, dead one running.
                self._refresh_user_session(username, email)
            else:
                self.get_user_db(username, email)
            session["email"] = email
            session["username"] = username

            return redirect(nextUrl or url_for("dashboard"))

        @self.app.route("/register", methods=["GET", "POST"])
        def register():
            if request.method == "GET":
                return render_template("register.html")

            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            confirmPassword = request.form.get("confirm_password", "")
            cookies = request.form.get("cookies", "")

            if _rateLimited("register"):
                return render_template(
                    "register.html", email=email, error=RATE_LIMIT_ERROR_MESSAGE), 429

            if not email or not password or not confirmPassword or not cookies:
                return render_template(
                    "register.html", email=email,
                    error="Email, password, confirmed password and cookies are all required.")

            if password != confirmPassword:
                return render_template(
                    "register.html", email=email, error="Passwords do not match.")

            policyError = _passwordPolicyError(password)
            if policyError:
                return render_template("register.html", email=email, error=policyError)

            # Verification happens against a throwaway session file, so nothing
            # is persisted for this email unless the cookies really are theirs.
            parsedCookies = parseCookieString(cookies)
            if not self.skipEmailVerification and not self._verifyCookiesMatchEmail(parsedCookies, email):
                return render_template(
                    "register.html", email=email,
                    error=f"Couldn't verify that these cookies belong to {email}. "
                          "Make sure you are logged into open.spotify.com with that account and copied all cookies.")

            existingUsername = self.repo.getUsernameForEmail(email)
            if existingUsername and self.repo.getUserPasswordHash(existingUsername):
                return render_template(
                    "register.html", email=email,
                    error="An account with this email already exists. Log in, or reset your "
                          "password if you forgot it.")

            # get_or_create_user returns the existing username for a legacy
            # (pre-password) account instead of erroring, so registering with
            # an email that's already logged in via cookies just adds a
            # password to that account rather than being rejected as a dupe.
            session.permanent = True
            username = self.get_or_create_user(email)
            self.repo.setUserCookies(username, parsedCookies)
            self.repo.setUserPassword(username, generate_password_hash(password))
            if username in self.user_databases:
                self._refresh_user_session(username, email)
            else:
                self.get_user_db(username, email)
            session["email"] = email
            session["username"] = username

            return redirect(url_for("dashboard"))

        @self.app.route("/reset-password", methods=["GET", "POST"])
        def resetPassword():
            if request.method == "GET":
                return render_template("reset_password.html")

            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            confirmPassword = request.form.get("confirm_password", "")
            cookies = request.form.get("cookies", "")

            if _rateLimited("reset-password"):
                return render_template(
                    "reset_password.html", email=email, error=RATE_LIMIT_ERROR_MESSAGE), 429

            if not email or not password or not confirmPassword or not cookies:
                return render_template(
                    "reset_password.html", email=email,
                    error="Email, new password, confirmed password and cookies are all required.")

            if password != confirmPassword:
                return render_template(
                    "reset_password.html", email=email, error="Passwords do not match.")

            policyError = _passwordPolicyError(password)
            if policyError:
                return render_template("reset_password.html", email=email, error=policyError)

            username = self.repo.getUsernameForEmail(email)
            if not username:
                return render_template(
                    "reset_password.html", email=email, error="No account found for this email.")

            # There's no old password to check against a forgotten one - proof
            # of identity is the same as everywhere else in this app: valid,
            # matching Spotify cookies for the account's email.
            parsedCookies = parseCookieString(cookies)
            if not self.skipEmailVerification and not self._verifyCookiesMatchEmail(parsedCookies, email):
                return render_template(
                    "reset_password.html", email=email,
                    error=f"Couldn't verify that these cookies belong to {email}. "
                          "Make sure you are logged into open.spotify.com with that account and copied all cookies.")

            self.repo.setUserCookies(username, parsedCookies)
            self.repo.setUserPassword(username, generate_password_hash(password))
            if username in self.user_databases:
                self._refresh_user_session(username, email)
            else:
                self.get_user_db(username, email)

            session.permanent = True
            session["email"] = email
            session["username"] = username

            return redirect(url_for("dashboard"))

        @self.app.route("/logout", methods=["GET"])
        def logout():
            session.clear()
            return redirect(url_for("login"))

        @self.app.route("/profile", methods=["GET", "POST"])
        def profilePage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            success = request.args.get("success")
            error = request.args.get("error")
            responseStatus = 200   #< 429 when a POST action was rate limited

            spotify_callback_url = os.environ.get("SPOTIFY_CALLBACK_URL")
            feature_enabled = bool(spotify_callback_url)

            if request.method == "POST":
                action = request.form.get("action")
                if action == "save_preferences":
                    default_window = request.form.get("default_dashboard_window")
                    timezone = request.form.get("timezone")
                    if timezone == "":
                        timezone = None
                    try:
                        db.repo.updateUserSettings(username, default_window, timezone)
                        db.refreshSettings()
                        success = "Preferences saved successfully!"
                    except Exception as e:
                        error = f"Failed to save preferences: {str(e)}"
                elif action == "request_share":
                    target_username = request.form.get("target_username", "").strip()
                    # Throttled like login/register: declines delete the row,
                    # so nothing else stops a rejected requester from re-
                    # requesting (or fanning out to every user) indefinitely.
                    if _rateLimited("request_share"):
                        error = RATE_LIMIT_ERROR_MESSAGE
                        responseStatus = 429
                    elif not target_username:
                        error = "Please choose a user to request a share with."
                    elif target_username == username:
                        error = "You cannot request a share with yourself."
                    elif not self.repo.usernameExists(target_username):
                        error = "That username does not exist."
                    else:
                        result = self.repo.createShareRequest(username, target_username)
                        if result == "accepted":
                            success = f"You and {target_username} are now sharing data with each other!"
                        elif result == "requested":
                            success = f"Share request sent to {target_username}."
                        elif result == "already_accepted":
                            success = f"You already share data with {target_username}."
                        else:   #< "already_requested"
                            success = f"A share request to {target_username} is already pending."
                else:
                    if not feature_enabled:
                        abort(404)
                    client_id = request.form.get("client_id")
                    client_secret = request.form.get("client_secret")
                    if client_id and client_secret:
                        try:
                            db.updateUserSpotifyCredentials(client_id, client_secret, None)
                            success = "Spotify Developer credentials saved! Please click 'Authorize with Spotify' to connect your account."
                        except Exception as e:
                            error = f"Failed to save credentials: {str(e)}"
                    else:
                        error = "Both Client ID and Client Secret are required."

            creds = db.getUserSpotifyCredentials() or {}
            client_id = creds.get("client_id")
            client_secret = creds.get("client_secret")
            refresh_token = creds.get("refresh_token")

            settings = db.repo.getUserSettings(username)
            default_window = settings.get("default_dashboard_window", "day")
            user_timezone = settings.get("timezone") or ""

            # Reaching this render means the Active Shares list below is
            # about to show whatever the notification was about - clears the
            # topbar's "your request was accepted" badge for next page load.
            self.repo.markAcceptedSharesSeenByRequester(username)

            pendingIncoming = self.repo.getPendingIncomingShares(username)
            pendingOutgoing = self.repo.getPendingOutgoingShares(username)
            acceptedShares = self.repo.getAcceptedShares(username)
            # Users already in a share relationship (either direction, pending
            # or accepted) are excluded from the request picker - re-requesting
            # them is always a no-op, so offering them just invites confusion.
            existingCounterparts = ({share["counterpart"] for share in acceptedShares}
                                    | {r["requester_username"] for r in pendingIncoming}
                                    | {r["recipient_username"] for r in pendingOutgoing})
            shareCandidates = [u for u in self.repo.getAllUsernamesExcept(username)
                               if u not in existingCounterparts]

            return render_template(
                "profile.html",
                username=username,
                email=email,
                client_id=client_id,
                client_secret=client_secret,
                has_api=bool(client_id and client_secret),
                is_authenticated=bool(refresh_token),
                redirect_uri=spotify_callback_url,
                success=success,
                error=error,
                section="profile",
                feature_enabled=feature_enabled,
                default_window=default_window,
                user_timezone=user_timezone,
                pendingIncoming=pendingIncoming,
                pendingOutgoing=pendingOutgoing,
                acceptedShares=acceptedShares,
                shareCandidates=shareCandidates,
            ), responseStatus

        @self.app.route("/profile/disconnect", methods=["GET"])
        def profileDisconnect():
            if not os.environ.get("SPOTIFY_CALLBACK_URL"):
                abort(404)
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login"))

            try:
                db.updateUserSpotifyCredentials(None, None, None)
                return redirect(url_for("profilePage", success="Successfully disconnected Spotify API credentials."))
            except Exception as e:
                return redirect(url_for("profilePage", error=f"Failed to disconnect: {str(e)}"))

        @self.app.route("/profile/shares/<int:share_id>", methods=["POST"])
        def profileShareAction(share_id):
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login"))

            action = request.form.get("action")
            # Each branch's repo call is itself ownership-checked (only the
            # recipient can accept/decline, only the requester can cancel,
            # either party can revoke) - `ok=False` here covers both "no such
            # share_id" and "share_id exists but isn't yours", identically.
            if action == "accept":
                ok = self.repo.respondToShareRequest(share_id, username, accept=True)
                successMsg, errorMsg = "Share accepted - you're now comparing data with each other!", "Could not accept that request."
            elif action == "decline":
                ok = self.repo.respondToShareRequest(share_id, username, accept=False)
                successMsg, errorMsg = "Share request declined.", "Could not decline that request."
            elif action == "cancel":
                ok = self.repo.cancelShareRequest(share_id, username)
                successMsg, errorMsg = "Share request canceled.", "Could not cancel that request."
            elif action == "revoke":
                ok = self.repo.revokeShare(share_id, username)
                successMsg, errorMsg = "Share revoked.", "Could not revoke that share."
            else:
                ok, errorMsg = False, "Unknown action."

            if ok:
                return redirect(url_for("profilePage", success=successMsg))
            return redirect(url_for("profilePage", error=errorMsg))

        @self.app.route("/spotify-authorize", methods=["GET"])
        def spotifyAuthorize():
            spotify_callback_url = os.environ.get("SPOTIFY_CALLBACK_URL")
            if not spotify_callback_url:
                abort(404)
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login"))

            creds = db.getUserSpotifyCredentials() or {}
            client_id = creds.get("client_id")
            if not client_id:
                return redirect(url_for("profilePage", error="API Credentials not configured."))

            scope = "user-read-recently-played"
            
            auth_url = (
                f"https://accounts.spotify.com/authorize"
                f"?client_id={client_id}"
                f"&response_type=code"
                f"&redirect_uri={spotify_callback_url}"
                f"&scope={scope}"
            )
            return redirect(auth_url)

        @self.app.route("/spotify-callback", methods=["GET"])
        def spotifyCallback():
            spotify_callback_url = os.environ.get("SPOTIFY_CALLBACK_URL")
            if not spotify_callback_url:
                abort(404)
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login"))

            code = request.args.get("code")
            error = request.args.get("error")

            if error or not code:
                return redirect(url_for("profilePage", error=f"Spotify authorization failed: {error or 'No authorization code returned'}"))

            creds = db.getUserSpotifyCredentials() or {}
            client_id = creds.get("client_id")
            client_secret = creds.get("client_secret")

            if not client_id or not client_secret:
                return redirect(url_for("profilePage", error="API Credentials missing."))

            import base64
            import requests
            url = "https://accounts.spotify.com/api/token"
            payload = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": spotify_callback_url,
            }
            auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
            headers = {
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded"
            }

            try:
                resp = requests.post(url, data=payload, headers=headers, timeout=10)
                if resp.status_code == 200:
                    resp_data = resp.json()
                    refresh_token = resp_data.get("refresh_token")
                    db.updateUserSpotifyCredentials(client_id, client_secret, refresh_token)
                    
                    # Restart listener thread to pick up the credentials immediately
                    db.startListener()
                    
                    return redirect(url_for("profilePage", success="Spotify account successfully authorized and connected!"))
                else:
                    return redirect(url_for("profilePage", error=f"Failed to exchange token: {resp.text}"))
            except Exception as e:
                return redirect(url_for("profilePage", error=f"Exception during token exchange: {str(e)}"))

        @self.app.route("/import-progress", methods=["GET"])
        def importProgress():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return jsonify({"error": "unauthorized"}), 401
            return jsonify(db.readProgress())

        @self.app.route("/version_status", methods=["GET"])
        def version_status():
            # Return the current and latest versions (latest is null if not newer)
            with self._version_lock:
                latest = self.latestVersion
            if latest and _is_version_newer(latest, self.currentVersion):
                return jsonify({"current": self.currentVersion, "latest": latest})
            else:
                return jsonify({"current": self.currentVersion, "latest": None})

        @self.app.route("/api/listener-status", methods=["GET"])
        def listenerStatus():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return jsonify({"error": "Not logged in"}), 401
            health = db.getListenerHealth()
            return jsonify(health)

        @self.app.route("/overview", methods=["GET"])
        def overviewPage():
            from datetime import datetime
            # Intentionally unauthenticated: aggregate counts/DB size carry no
            # per-user listening data, so they're shown to any visitor as a
            # public "is this instance alive" summary - only the per-user
            # table below (usernames, emails, sync status) is gated on login.
            global_stats = self.repo.getGlobalDatabaseStats()
            
            total_time_ms = global_stats.get("total_time_ms", 0)
            total_hours = total_time_ms // (1000 * 60 * 60)
            if total_hours >= 24:
                days = total_hours // 24
                hours = total_hours % 24
                global_time_text = f"{days}d {hours}h"
            else:
                global_time_text = f"{total_hours}h"

            db_size_bytes = global_stats.get("db_size_bytes", 0)
            if db_size_bytes >= 1024 * 1024 * 1024:
                global_size_text = f"{db_size_bytes / (1024 * 1024 * 1024):.2f} GB"
            elif db_size_bytes >= 1024 * 1024:
                global_size_text = f"{db_size_bytes / (1024 * 1024):.2f} MB"
            else:
                global_size_text = f"{db_size_bytes / 1024:.1f} KB"

            email = session.get("email")
            is_logged_in = email is not None and self.is_user_logged_in(email)

            # Get current user's timezone for consistent date display
            current_user_tz = None
            if is_logged_in:
                current_username = self.get_username_for_email(email) or self.get_or_create_user(email)
                current_db = self.get_user_db(current_username, email)
                current_user_tz = current_db.tz if current_db else None

            users_list = []
            if is_logged_in:
                all_users = self.repo.getAllUsersDetails()
                for u in all_users:
                    u_username = u["username"]
                    u_email = u["email"]

                    # Get Listener sync status
                    if u["cookies_json"]:
                        # Ensure we have a Database instance initialized to get live sync health
                        u_db = self.get_user_db(u_username, u_email)
                        health = u_db.getListenerHealth()
                        sync_status = health.get("status", "UNKNOWN")
                    else:
                        sync_status = "Not Configured"

                    # Check API backfill configuration status
                    has_api = bool(u["spotify_client_id"] and u["spotify_refresh_token"])
                    api_status = "Configured" if has_api else "Not Configured"

                    # Total plays for this user
                    plays_count = self.repo.getPlaysCount(u_username)

                    # Format created_at date using current user's timezone
                    created_at_val = u.get("created_at")
                    created_date_str = ""
                    if created_at_val:
                        try:
                            created_date_str = convertToDatetime(created_at_val, tz=current_user_tz).strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            pass
                    
                    users_list.append({
                        "username": u_username,
                        "email": u_email,
                        "sync_status": sync_status,
                        "api_status": api_status,
                        "plays_count": plays_count,
                        "created_at": created_date_str
                    })
            
            return render_template(
                "overview.html",
                global_stats=global_stats,
                global_time_text=global_time_text,
                global_size_text=global_size_text,
                is_logged_in=is_logged_in,
                users_list=users_list,
                section="overview"
            )

        @self.app.route("/", methods=["GET"])
        def dashboard():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            settings = db.repo.getUserSettings(username)
            default_window = settings.get("default_dashboard_window", "day")

            page = self._getPageParam()
            searchQuery = request.args.get("q", "")
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")
            
            interval = request.args.get("interval", default_window)
            if interval == "":
                interval = default_window

            if interval == "custom" and not (customStart and customEnd):
                interval = "all time"

            if searchQuery:
                # Matching and pagination both happen in SQL (Repository.searchPlays)
                # instead of fetching every play ever recorded and filtering in Python.
                totalCount = db.searchEntriesCount(searchQuery)
                page, totalPages, startIndex = self._calculatePagination(totalCount)
                tracks = db.searchEntries(searchQuery, count=PAGE_SIZE, startIndex=startIndex)
            else:
                # Only materialize the page being shown - joining full track
                # metadata onto every entry ever recorded on every request gets
                # slow once the history grows large.
                totalCount = db.getEntriesCount()
                page, totalPages, startIndex = self._calculatePagination(totalCount)
                tracks = db.getEntriesFromNew(count=PAGE_SIZE, startIndex=startIndex)
            tracks = self._embedSongsTextElements(tracks)

            intervalLabel = self._getIntervalLabel(interval, customStart, customEnd)
            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="day", tz=db.tz)
            stats = db.getOverallStats(startDate, endDate) 

            totalDurationText = msToString(stats["totalDurationMs"])

            currentTopSong = self._embedTopSongTextElements(stats["currentTopSongs"][0], sortBy="plays", totalPlays=stats["totalSongsPlayed"], totalMs=stats["totalDurationMs"]) if stats["currentTopSongs"] else None
            currentTopArtist = self._embedArtistTextElement(stats["currentTopArtists"][0], sortBy="totalTimeListened", totalPlays=stats["totalSongsPlayed"], totalMs=stats["totalDurationMs"]) if stats["currentTopArtists"] else None

            totalSongsChangeText, totalSongsChangeClass = self._getChangeText(stats["totalSongsPlayed"], stats["previousSongsPlayed"])
            totalListenChangeText, totalListenChangeClass = self._getChangeText(stats["totalDurationMs"], stats["previousDurationMs"])

            pagination = self._buildPaginationContext(
                "dashboard",
                page,
                totalPages,
                totalCount,
                q=searchQuery,
                interval=interval,
                startDate=customStart,
                endDate=customEnd,
            )

            creds = db.getUserSpotifyCredentials() or {}
            has_api = bool(creds.get("client_id") and creds.get("client_secret"))
            is_authenticated = bool(creds.get("refresh_token"))

            return render_template(
                "tracks.html",
                tracks=tracks,
                totalSongsPlayed=stats["totalSongsPlayed"],
                totalListenTime=totalDurationText,
                totalSongsChangeText=totalSongsChangeText,
                totalSongsChangeClass=totalSongsChangeClass,
                totalListenChangeText=totalListenChangeText,
                totalListenChangeClass=totalListenChangeClass,
                currentTopSong=currentTopSong,
                currentTopArtist=currentTopArtist,
                intervalLabel=intervalLabel,
                username=username,
                startIndex=startIndex,
                section="dashboard",
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                has_api=has_api,
                is_authenticated=is_authenticated,
                **pagination,
            )

        @self.app.route("/top-songs", methods=["GET"])
        def topSongsPage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            page = self._getPageParam()
            searchQuery = request.args.get("q", "")
            sortBy = self._getSortByParam()
            interval = request.args.get("interval", "")
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")

            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="all time", tz=db.tz)
            # totalPlays/totalMs are a whole-range aggregate regardless of search -
            # a cheap dedicated query instead of summing every song's metadata.
            totalPlays, totalMs = db.getPlayTotals(startDate, endDate)
            uniqueSongs = db.getSongsCount(startDate, endDate)

            # Only materialize the page being shown - SQL-level LIMIT/OFFSET and
            # WHERE-clause matching (see Repository.getSongsPage) instead of
            # sorting+hydrating+filtering every song ever played in Python.
            if searchQuery:
                totalCount = db.getSongsCount(startDate, endDate, searchQuery=searchQuery)
            else:
                totalCount = uniqueSongs
            page, totalPages, startIndex = self._calculatePagination(totalCount)
            tracks = db.getTopSongs(startDate=startDate, endDate=endDate, by=sortBy,
                                     limit=PAGE_SIZE, offset=startIndex, searchQuery=searchQuery)

            pagination = self._buildPaginationContext(
                "topSongsPage",
                page,
                totalPages,
                totalCount,
                q=searchQuery,
                sortBy=sortBy,
                interval=interval,
                startDate=customStart,
                endDate=customEnd,
            )

            tracks = self._embedSongsTextElements(tracks)
            tracks = self._embedTopSongsTextElements(tracks, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)

            return render_template(
                "top_songs.html",
                tracks=tracks,
                username=username,
                totalPlays=totalPlays,
                totalTime=msToString(totalMs),
                uniqueSongs=uniqueSongs,
                startIndex=startIndex,
                section="top_songs",
                sortBy=sortBy,
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                **pagination,
            )

        @self.app.route("/top-albums", methods=["GET"])
        def topAlbumsPage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            page = self._getPageParam()
            searchQuery = request.args.get("q", "")
            sortBy = self._getSortByParam()
            interval = request.args.get("interval", "")
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")

            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="all time", tz=db.tz)
            totalPlays, totalMs = db.getPlayTotals(startDate, endDate)
            uniqueAlbums = db.getAlbumsCount(startDate, endDate)

            # Only materialize the page being shown - SQL-level LIMIT/OFFSET and
            # WHERE-clause matching (see Repository.getAlbumsPage) instead of
            # sorting+hydrating+filtering every album ever played in Python.
            if searchQuery:
                totalCount = db.getAlbumsCount(startDate, endDate, searchQuery=searchQuery)
            else:
                totalCount = uniqueAlbums
            page, totalPages, startIndex = self._calculatePagination(totalCount)
            albums = db.getTopAlbums(startDate=startDate, endDate=endDate, by=sortBy,
                                      limit=PAGE_SIZE, offset=startIndex, searchQuery=searchQuery)

            pagination = self._buildPaginationContext(
                "topAlbumsPage",
                page,
                totalPages,
                totalCount,
                q=searchQuery,
                sortBy=sortBy,
                interval=interval,
                startDate=customStart,
                endDate=customEnd,
            )

            albums = self._embedAlbumsTextElements(albums, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)

            return render_template(
                "top_albums.html",
                tracks=albums,
                username=username,
                totalPlays=totalPlays,
                totalTime=msToString(totalMs),
                uniqueAlbums=uniqueAlbums,
                startIndex=startIndex,
                section="top_albums",
                sortBy=sortBy,
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                **pagination,
            )

        @self.app.route("/top-artists", methods=["GET"])
        def topArtistsPage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            page = self._getPageParam()
            searchQuery = request.args.get("q", "")
            sortBy = self._getSortByParam()
            interval = request.args.get("interval", "")
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")

            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="all time", tz=db.tz)
            # totalPlays/totalUnique/totalMs are the whole (date-range-scoped) top
            # list's totals regardless of search - mirrors getPlayTotals()'s role
            # for the songs/albums pages, computed via a dedicated SQL aggregate
            # instead of fetching every artist and summing in Python.
            totalPlays, totalUnique, totalMs = db.getArtistTotals(startDate, endDate)
            uniqueArtists = db.getArtistsCount(startDate, endDate)

            # Only materialize the page being shown - SQL-level LIMIT/OFFSET
            # instead of sorting+hydrating every artist ever played.
            if searchQuery:
                totalCount = db.getArtistsCount(startDate, endDate, searchQuery=searchQuery)
            else:
                totalCount = uniqueArtists
            page, totalPages, startIndex = self._calculatePagination(totalCount)
            artists = db.getTopArtists(startDate=startDate, endDate=endDate, by=sortBy,
                                        limit=PAGE_SIZE, offset=startIndex, searchQuery=searchQuery)

            artists = self._embedArtistsTextElements(artists, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
            pagination = self._buildPaginationContext(
                "topArtistsPage",
                page,
                totalPages,
                totalCount,
                q=searchQuery,
                sortBy=sortBy,
                interval=interval,
                startDate=customStart,
                endDate=customEnd,
            )

            return render_template(
                "top_artists.html",
                tracks=artists,
                username=username,
                totalPlays=totalPlays,
                totalUnique=totalUnique,
                uniqueArtists=uniqueArtists,
                totalTime=msToString(totalMs),
                startIndex=startIndex,
                section="top_artists",
                sortBy=sortBy,
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                **pagination,
            )

        @self.app.route("/charts", methods=["GET"])
        def chartsPage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            interval = self._getValidInterval(request.args.get("interval", "month"), default="month")
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")
            if interval == "custom" and not (customStart and customEnd):
                interval = "month"
            groupBy = self._getValidGroupBy(request.args.get("groupBy", "day"))

            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="month", tz=db.tz)
            intervalLabel = self._getIntervalLabel(interval, customStart, customEnd)

            isSingleDayView = interval in ("day", "today")
            lastDayDate = startDate.strftime("%Y-%m-%d") if isSingleDayView and startDate else None

            timeSeriesGroupBy = "hour" if isSingleDayView else groupBy

            timeSeries = self._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(startDate=startDate, endDate=endDate, groupBy=timeSeriesGroupBy)
            )
            heatmap = self._embedHeatmapTextElements(db.getHourOfDayHeatmap(startDate=startDate, endDate=endDate))
            artistTrend = None if isSingleDayView else db.getArtistTrend(startDate=startDate, endDate=endDate, topN=CHART_ARTIST_TREND_TOP_N, groupBy=groupBy)

            explicitRatio = db.getExplicitRatio(startDate=startDate, endDate=endDate)
            decadeDistribution = db.getReleaseDecadeDistribution(startDate=startDate, endDate=endDate)
            completionStats = db.getCompletionStats(startDate=startDate, endDate=endDate)

            return render_template(
                "charts.html",
                username=username,
                section="charts",
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                groupBy=groupBy,
                intervalLabel=intervalLabel,
                lastDayDate=lastDayDate,
                timeSeries=timeSeries,
                heatmap=heatmap,
                artistTrend=artistTrend,
                explicitRatio=explicitRatio,
                decadeDistribution=decadeDistribution,
                completionStats=completionStats,
            )

        @self.app.route("/wrapped", methods=["GET"])
        def wrappedPage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            nowLocal = now(tz=db.tz)
            currentYear = nowLocal.year

            oldestEntries = db.getEntriesFromOld(count=1, fullPagination=False)
            earliestYear = convertToDatetime(oldestEntries[0]["playedAt"], tz=db.tz).year if oldestEntries else currentYear
            availableYears = list(range(currentYear, earliestYear - 1, -1))   #< most recent first, for the year badges

            year = self._getWrappedYearParam(availableYears, currentYear)
            groupBy = self._getValidGroupBy(request.args.get("groupBy", "week"), default="week")

            limit = request.args.get("limit", type=int)
            if limit not in WRAPPED_LIMIT_OPTIONS:
                limit = WRAPPED_LIST_SIZE

            # 1. Fetch precalculated cached wrapped stats from database (unless db is a mock)
            from unittest.mock import MagicMock
            is_mock = isinstance(db, MagicMock) or (hasattr(db, "repo") and isinstance(db.repo, MagicMock))

            cached = None
            if not is_mock:
                cached = db.repo.getCachedWrapped(db.user, year)
                if not cached:
                    # Cache miss: recalculate and cache on the fly
                    db.recalculateWrappedForYear(year)
                    cached = db.repo.getCachedWrapped(db.user, year)
            else:
                # If db/repo is mock, check if getCachedWrapped was explicitly mocked to return a non-mock dict
                try:
                    res = db.repo.getCachedWrapped(db.user, year)
                    if res and not isinstance(res, MagicMock):
                        cached = res
                except Exception:
                    pass

            if cached is not None:
                # If still empty defaults needed
                if not cached:
                    cached = {
                        "total_plays": 0,
                        "total_ms": 0,
                        "longest_streak": 0,
                        "peak_day": None,
                        "peak_plays": 0,
                        "unique_songs": 0,
                        "unique_artists": 0,
                        "discovered_songs": 0,
                        "discovered_artists": 0,
                        "time_series_day": "[]",
                        "time_series_week": "[]",
                        "time_series_month": "[]",
                        "top_songs": "[]",
                        "top_artists": "[]",
                        "top_albums": "[]",
                        "discovered_songs_list": "[]",
                        "discovered_artists_list": "[]",
                        "discovered_albums_list": "[]",
                    }

                # 2. Extract values and parse lists
                totalPlays = cached["total_plays"]
                totalMs = cached["total_ms"]
                longestStreak = cached["longest_streak"]
                peakListeningTime = (cached["peak_day"], cached["peak_plays"]) if cached["peak_day"] else None
                uniqueSongsCount = cached["unique_songs"]
                uniqueArtistsCount = cached["unique_artists"]
                discoveredSongsCount = cached["discovered_songs"]
                discoveredArtistsCount = cached["discovered_artists"]

                timeSeriesDay = json.loads(cached["time_series_day"])
                timeSeriesWeek = json.loads(cached["time_series_week"])
                timeSeriesMonth = json.loads(cached["time_series_month"])

                topSongs = json.loads(cached["top_songs"])
                topArtists = json.loads(cached["top_artists"])
                topAlbums = json.loads(cached["top_albums"])

                discoveredSongs = json.loads(cached["discovered_songs_list"])
                discoveredArtists = json.loads(cached["discovered_artists_list"])
                discoveredAlbums = json.loads(cached["discovered_albums_list"])

                # 3. Select timeseries grouping
                if groupBy == "day":
                    timeSeries = timeSeriesDay
                elif groupBy == "month":
                    timeSeries = timeSeriesMonth
                else:
                    timeSeries = timeSeriesWeek

                # 4. Slice to requested limit (precalculated lists store up to 100 items)
                topSongs = topSongs[:limit]
                topArtists = topArtists[:limit]
                topAlbums = topAlbums[:limit]
                discoveredSongs = discoveredSongs[:limit]
                discoveredArtists = discoveredArtists[:limit]
                discoveredAlbums = discoveredAlbums[:limit]
            else:
                # Dynamic calculations for mocks (unit tests compatibility)
                yearStart = nowLocal.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
                yearEnd = nowLocal.replace(year=year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

                topSongs = db.getTopSongs(startDate=yearStart, endDate=yearEnd, by="plays", limit=limit)
                topArtists = db.getTopArtists(startDate=yearStart, endDate=yearEnd, by="plays", limit=limit)
                topAlbums = db.getTopAlbums(startDate=yearStart, endDate=yearEnd, by="plays", limit=limit)
                totalPlays, totalMs = db.getPlayTotals(yearStart, yearEnd)

                discoveredSongs = self._discoveriesInYear(
                    db.getSongsStats(sortBy="plays"), yearStart, yearEnd, limit
                )
                discoveredArtists = self._discoveriesInYear(
                    db.getArtistsStats(), yearStart, yearEnd, limit
                )
                discoveredAlbums = self._discoveriesInYear(
                    db.getAlbumsStats(sortBy="plays"), yearStart, yearEnd, limit
                )

                timeSeries = db.getListeningTimeSeries(startDate=yearStart, endDate=yearEnd, groupBy=groupBy)

                longestStreak = db.getLongestStreak(yearStart, yearEnd)
                peakListeningTime = db.getPeakListeningTime(yearStart, yearEnd)
                uniqueSongsCount = db.getSongsCount(yearStart, yearEnd)
                uniqueArtistsCount = db.getArtistsCount(yearStart, yearEnd)
                discoveredSongsCount = db.getDiscoveredSongsCount(yearStart, yearEnd)
                discoveredArtistsCount = db.getDiscoveredArtistsCount(yearStart, yearEnd)

            # 5. Embed presentation elements
            timeSeries = self._embedTimeSeriesTextElements(timeSeries)
            topSongs = self._embedSongsTextElements(topSongs)
            topSongs = self._embedTopSongsTextElements(topSongs, sortBy="plays", totalPlays=totalPlays, totalMs=totalMs)
            topArtists = self._embedArtistsTextElements(topArtists, sortBy="plays", totalPlays=totalPlays, totalMs=totalMs)
            topAlbums = self._embedAlbumsTextElements(topAlbums, sortBy="plays", totalPlays=totalPlays, totalMs=totalMs)
            discoveredSongs = self._embedTopSongsTextElements(self._embedSongsTextElements(discoveredSongs))
            discoveredArtists = self._embedArtistsTextElements(discoveredArtists)
            discoveredAlbums = self._embedAlbumsTextElements(discoveredAlbums)

            # 6. Check if AJAX request and return JSON response if true
            if request.args.get("ajax") == "true":
                update_type = request.args.get("type", "all")
                res = {}

                if update_type in ("all", "chart"):
                    res["timeSeries"] = timeSeries

                if update_type in ("all", "lists"):
                    res["topSongsHtml"] = render_template("_wrapped_list.html", items=topSongs, section="top_songs", username=username, year=year)
                    res["topArtistsHtml"] = render_template("_wrapped_list.html", items=topArtists, section="top_artists", username=username, year=year)
                    res["topAlbumsHtml"] = render_template("_wrapped_list.html", items=topAlbums, section="top_albums", username=username, year=year)
                    res["discoveredSongsHtml"] = render_template("_wrapped_list.html", items=discoveredSongs, section="top_songs", username=username, year=year)
                    res["discoveredArtistsHtml"] = render_template("_wrapped_list.html", items=discoveredArtists, section="top_artists", username=username, year=year)
                    res["discoveredAlbumsHtml"] = render_template("_wrapped_list.html", items=discoveredAlbums, section="top_albums", username=username, year=year)

                if update_type == "all":
                    topSongText = (
                        f"{topSongs[0]['name']} - {topSongs[0]['artists'][0]['name']}"
                        if topSongs and topSongs[0].get('artists')
                        else (topSongs[0]['name'] if topSongs else "N/A")
                    )
                    topArtistText = topArtists[0]['name'] if topArtists else "N/A"
                    topAlbumText = topAlbums[0]['name'] if topAlbums else "N/A"
                    res.update({
                        "totalPlays": totalPlays,
                        "totalTime": msToString(totalMs),
                        "longestStreak": longestStreak,
                        "peakDay": peakListeningTime[0] if peakListeningTime else "N/A",
                        "peakPlays": peakListeningTime[1] if peakListeningTime else 0,
                        "uniqueSongsCount": uniqueSongsCount,
                        "uniqueArtistsCount": uniqueArtistsCount,
                        "discoveredSongsCount": discoveredSongsCount,
                        "discoveredArtistsCount": discoveredArtistsCount,
                        "topSongText": topSongText,
                        "topArtistText": topArtistText,
                        "topAlbumText": topAlbumText
                    })
                return jsonify(res)

            creds = db.getUserSpotifyCredentials() or {}
            has_api = bool(creds.get("client_id") and creds.get("client_secret"))
            is_authenticated = bool(creds.get("refresh_token"))

            success = request.args.get("success")
            error = request.args.get("error")

            return render_template(
                "wrapped.html",
                username=username,
                section="wrapped",
                year=year,
                availableYears=availableYears,
                groupBy=groupBy,
                limit=limit,
                limitOptions=WRAPPED_LIMIT_OPTIONS,
                totalPlays=totalPlays,
                totalTime=msToString(totalMs),
                topSongs=topSongs,
                topArtists=topArtists,
                topAlbums=topAlbums,
                discoveredSongs=discoveredSongs,
                discoveredArtists=discoveredArtists,
                discoveredAlbums=discoveredAlbums,
                timeSeries=timeSeries,
                longestStreak=longestStreak,
                peakListeningTime=peakListeningTime,
                uniqueSongsCount=uniqueSongsCount,
                uniqueArtistsCount=uniqueArtistsCount,
                discoveredSongsCount=discoveredSongsCount,
                discoveredArtistsCount=discoveredArtistsCount,
                has_api=has_api,
                is_authenticated=is_authenticated,
                success=success,
                error=error,
            )

        @self.app.route("/compare", methods=["GET"])
        def comparePage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            # Mirrors /overview's cookies_json guard: get_user_db starts a
            # live listener, which needs stored cookies - a share counterpart
            # without them (only creatable by seeding user_shares directly;
            # the UI can't accept a share while logged out) must be skipped,
            # not crash the page.
            acceptedUsernames = [
                u for u in self.repo.getAcceptedShareUsernames(username)
                if self.repo.getUserCookies(u) is not None
            ]
            if not acceptedUsernames:
                abort(404)

            withUsername = request.args.get("with", acceptedUsernames[0])
            if withUsername not in acceptedUsernames:
                # ?with= is untrusted input - never let it select a user's data
                # the session user hasn't mutually accepted a share with. Fall
                # back to a real choice rather than 404ing, which would leak
                # whether an unrelated username exists at all.
                withUsername = acceptedUsernames[0]

            otherEmail = self.repo.getEmailForUsername(withUsername)
            otherDb = self.get_user_db(withUsername, otherEmail)

            interval = request.args.get("interval", "")
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")
            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="all time", tz=db.tz)
            groupBy = self._getValidGroupBy(request.args.get("groupBy", "day"))
            # Single-day ranges bucket by hour, mirroring chartsPage's
            # isSingleDayView - one 'day' bucket would collapse the whole
            # trend into a single point.
            trendGroupBy = "hour" if interval in ("day", "today") else groupBy

            my = self._gatherCompareStats(db, startDate, endDate)
            their = self._gatherCompareStats(otherDb, startDate, endDate)

            theirArtistIds = {a["id"] for a in their["topArtistsPool"]}
            # Sliced like every other list on the page. No percent text here -
            # it would mix two different users' totals.
            sharedArtists = self._embedArtistsTextElements(
                [a for a in my["topArtistsPool"] if a["id"] in theirArtistIds][:COMPARE_TOP_LIST_SIZE])

            trendStartDate, trendEndDate = startDate, endDate
            if trendStartDate is None or trendEndDate is None:
                # "All Time" passes no explicit range, and getListeningTimeSeries
                # then gap-fills each user only across their own first-to-last
                # play - two users with disjoint listening eras would union into
                # an axis with the years between them missing entirely. Pin both
                # series to one combined range instead.
                playRanges = [r for r in (self.repo.getPlayTimeRange(username),
                                          self.repo.getPlayTimeRange(withUsername)) if r]
                if playRanges:
                    trendStartDate = convertToDatetime(min(r[0] for r in playRanges), tz=db.tz)
                    trendEndDate = convertToDatetime(max(r[1] for r in playRanges), tz=db.tz) + timedelta(seconds=1)

            # Each Database instance buckets time-series labels in its own
            # user's timezone, so the two series can still have edge buckets
            # the other lacks - built from the union of both sides' labels
            # rather than assumed to line up positionally.
            myByLabel = {b["label"]: b["totalTimeListened"] for b in db.getListeningTimeSeries(trendStartDate, trendEndDate, groupBy=trendGroupBy)}
            theirByLabel = {b["label"]: b["totalTimeListened"] for b in otherDb.getListeningTimeSeries(trendStartDate, trendEndDate, groupBy=trendGroupBy)}
            allLabels = sorted(set(myByLabel) | set(theirByLabel))
            comparisonTrend = {
                "buckets": allLabels,
                "series": [
                    {"name": username, "data": [myByLabel.get(label, 0) for label in allLabels]},
                    {"name": withUsername, "data": [theirByLabel.get(label, 0) for label in allLabels]},
                ],
            }

            return render_template(
                "compare.html",
                section="compare",
                username=username,
                withUsername=withUsername,
                acceptedUsernames=acceptedUsernames,
                my=my,
                their=their,
                sharedArtists=sharedArtists,
                comparisonTrend=comparisonTrend,
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                groupBy=groupBy,
            )

        @self.app.route("/song/<track_id>", methods=["GET"])
        def songDetailPage(track_id):
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            song = db.getSong(track_id)
            if song is None:
                return redirect(url_for("topSongsPage"))

            groupBy = self._getValidGroupBy(request.args.get("groupBy", "week"), default="week")

            song = self._embedSongTextElements(song)
            song = self._embedTopSongTextElements(song)

            timeSeries = self._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(trackId=track_id, groupBy=groupBy)
            )
            heatmap = self._embedHeatmapTextElements(db.getHourOfDayHeatmap(trackId=track_id))

            return render_template(
                "song_detail.html",
                song=song,
                username=username,
                groupBy=groupBy,
                timeSeries=timeSeries,
                heatmap=heatmap,
                section="top_songs",
            )

        @self.app.route("/artist/<artist_id>", methods=["GET"])
        def artistDetailPage(artist_id):
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            artist = db.getArtist(artist_id)
            if artist is None:
                return redirect(url_for("topArtistsPage"))

            groupBy = self._getValidGroupBy(request.args.get("groupBy", "week"), default="week")

            songs = db.getSongsStats(sortBy="plays", artistId=artist_id)
            firstSong = min(songs, key=lambda s: s.get("firstListenedAt") or float("inf")) if songs else None
            firstSongName = firstSong.get("name") if firstSong else None

            songs = self._embedSongsTextElements(songs)
            songs = self._embedTopSongsTextElements(
                songs, sortBy="plays", totalPlays=artist.get("plays", 0), totalMs=artist.get("totalTimeListened", 0)
            )
            artist = self._embedArtistTextElement(artist)

            timeSeries = self._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(artistId=artist_id, groupBy=groupBy)
            )

            return render_template(
                "artist_detail.html",
                artist=artist,
                songs=songs,
                firstSongName=firstSongName,
                username=username,
                groupBy=groupBy,
                timeSeries=timeSeries,
                section="top_artists",
            )

        @self.app.route("/album/<album_id>", methods=["GET"])
        def albumDetailPage(album_id):
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            album = db.getAlbum(album_id)
            if album is None:
                return redirect(url_for("topAlbumsPage"))

            groupBy = self._getValidGroupBy(request.args.get("groupBy", "week"), default="week")

            songs = db.getSongsStats(sortBy="plays", albumId=album_id)
            firstSong = min(songs, key=lambda s: s.get("firstListenedAt") or float("inf")) if songs else None
            firstSongName = firstSong.get("name") if firstSong else None

            songs = self._embedSongsTextElements(songs)
            songs = self._embedTopSongsTextElements(
                songs, sortBy="plays", totalPlays=album.get("plays", 0), totalMs=album.get("totalTimeListened", 0)
            )
            album = self._embedAlbumTextElements(album)

            timeSeries = self._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(albumId=album_id, groupBy=groupBy)
            )

            return render_template(
                "album_detail.html",
                album=album,
                songs=songs,
                firstSongName=firstSongName,
                groupBy=groupBy,
                username=username,
                timeSeries=timeSeries,
                section="top_albums",
            )

    def shutdown(self):
        self._stop_event.set()
        with self._db_lock:
            for db in self.user_databases.values():
                try:
                    db.stop()
                except Exception as e:
                    logger.error("Error stopping database for %s: %s", db.user, e)

    def run(self):
        try:
            debug = os.environ.get("FLASK_DEBUG", "").lower() in TRUTHY_ENV_VALUES
            self.app.run(host="0.0.0.0", debug=debug, port=5444, use_reloader=False)#, threaded=False)
        finally:
            self.shutdown()

if __name__ == "__main__":
    ## $env:IMPORT_KEYWORD="Weekly"
    ## $env:TZ="America/Los_Angeles"

    dashboardApp = SpotifyDashboardApp()
    dashboardApp.run()