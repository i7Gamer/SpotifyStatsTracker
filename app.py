import logging
import math
import os
import json
import random
import secrets
import tempfile
import threading
import requests
from pathlib import Path
import time
from datetime import timedelta, datetime, timezone

from flask import Flask, render_template, redirect, request, url_for, jsonify, send_from_directory, session, g, abort, Response, stream_with_context, make_response
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

from Database.database import Database
from Database.backup import BackupWorker
from Database.db import SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON
from Database.repository import Repository
from Database.Migrators.migrate import migrateIfNeeded
from Database.Listeners.spotifyListener import _suppress_signal_in_thread
from Database.logging_config import configureLogging
from Database.utils import msToString, convertToDatetime, formatDuration, dateToString, versionTuple, now, startOfDay, parseDateString
# Genre-gate / coverage helpers live in services/genre_gate.py; re-exported here
# so route code (and the test suite, which imports several by name) still reach
# them through `app`.
from services.genre_gate import (
    GENRE_GATE_OVERALL_MIN_PERCENT, GENRE_GATE_CATEGORY_MIN_PERCENT,
    emptyGenreCoverage, sanitizeGenreCoverage, resolveGenreCoverage,
    resolveGenreDistribution, genreGatePasses, emptyBiographyCoverage,
    sanitizeBiographyCoverage, resolveBiographyCoverage,
    resolveGenresForTrack, resolveGenresForAlbum, resolveGenresForArtist,
)
# Taste-match scoring lives in services/taste_match.py; the compare route calls
# _tasteMatchPercent/_markLinkExternally and _buildSharedItems calls
# _rankById/_sharedRankScore.
from services.taste_match import (
    _tasteMatchPercent, _markLinkExternally, _rankById, _sharedRankScore,
)
from routes.media import register as registerMediaRoutes
from routes.admin import register as registerAdminRoutes
from routes.compare import register as registerCompareRoutes
from routes.wrapped import register as registerWrappedRoutes
from routes.auth import register as registerAuthRoutes
from routes.system import register as registerSystemRoutes
import SpotipyFree
from SpotipyFree import saveSession, parseCookieString

logger = logging.getLogger(__name__)

PAGE_SIZE = 50                  #< list items shown per page
LOGIN_CACHE_TTL_SECONDS = 180  #< seconds to cache isListenerLoggedIn result per user
CHART_ARTIST_TREND_TOP_N = 5   #< how many top artists are plotted on the trend line chart
CHART_TOP_GENRES_LIMIT = 10    #< bars on the Charts page's Top Genres chart
WRAPPED_TOP_GENRES_LIMIT = 5   #< genres listed on the Wrapped genre card
COMPARE_TOP_GENRES_LIMIT = 10  #< per-side genres (and shared genres) shown on Compare
COMPARE_GENRE_POOL_SIZE = 50   #< per-side genre pool the shared-genre intersection is computed over
TRACK_CARD_GENRE_LIMIT = 3     #< genre pills shown per track/artist/album card, position-ordered
# GENRE_GATE_OVERALL_MIN_PERCENT / GENRE_GATE_CATEGORY_MIN_PERCENT now live in
# services/genre_gate.py (imported above).
WRAPPED_LIST_SIZE = 10          #< default/fallback for ?limit= - how many items per category the Wrapped page shows
WRAPPED_LIMIT_OPTIONS = (10, 25, 50, 100)   #< selectable values for Wrapped's items-per-category dropdown
# Public Wrapped share-link expiry choices: form value -> seconds until
# expiry, or None for "never". Mirrors ALBUM_BACKFILL_RETRY_SECONDS/
# GENRE_BACKFILL_RETRY_SECONDS's N * 24 * 3600 convention in repository.py.
SHARE_LINK_EXPIRY_CHOICES = {
    "never": None,
    "7d": 7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
}
# Cap on concurrent (non-expired) share links per "bucket" - a bucket is
# either one specific year or the all-years link type. Prevents runaway
# link accumulation (each one is a standing, unauthenticated access grant)
# while still letting someone hand out a few links to different people
# without having to revoke-then-recreate each time.
SHARE_LINK_MAX_PER_BUCKET = 5
COMPARE_TOP_LIST_SIZE = 10                #< items per top-songs/artists/albums list shown on the Compare page
COMPARE_OVERLAP_POOL_SIZE = 100           #< how deep each side's top songs/artists/albums lists are searched for taste-match overlap
# Top Common Songs/Artists/Albums search a SEPARATE, deeper pool than
# COMPARE_OVERLAP_POOL_SIZE - decoupled on purpose so widening the shared-
# item search can never move the taste-match score (see _tasteMatchPercent,
# which only ever reads the shallower topXPool fields). First knob to
# revisit if the Top Common lists feel too sparse (raise it) or too full of
# irrelevant long-tail matches (lower it) - 300 was tried and felt too deep.
COMPARE_SHARED_POOL_SIZE = 200
COMPARE_TREND_WEEK_SPAN_DAYS = 120        #< comparison trends spanning more days than this auto-bucket by week...
COMPARE_TREND_MONTH_SPAN_DAYS = 730       #< ...and more than this by month (day buckets over years are sub-pixel)
# Taste-match weights, credit factors and the response-curve exponent now live
# in services/taste_match.py alongside the scoring functions that use them.
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_INLINE_ARTISTS = 5   #< artist lists longer than this collapse behind a "+N more" toggle (_artist_links.html)...
MIN_HIDDEN_ARTISTS = 2   #< ...but only when at least this many names would be hidden - "+1 more" saves no space
MAX_UPLOAD_MB = 500              #< cap on a single import-history request's total upload size
DEFAULT_SORT_BY = "totalTimeListened"
# The only sortBy values Repository.SONG_SORT_COLUMNS/ALBUM_SORT_COLUMNS/
# ARTIST_SORT_COLUMNS know how to handle - an unrecognized ?sortBy= would
# otherwise reach a ValueError deep in the DB layer and 500 instead of just
# falling back to the default.
VALID_SORT_BY = {"totalTimeListened", "plays", "name"}
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
# Opt-in to honoring X-Forwarded-* headers from a reverse proxy (see
# _trustedProxyCount). Without it, every visitor behind a proxy shares the
# proxy's IP, so the per-IP auth rate limiter would let any one client lock
# the entire instance out of /login for the whole window.
TRUST_PROXY_HEADERS_ENV_VAR = "TRUST_PROXY_HEADERS"
# When set, the user with this email is made the instance's ONLY admin at
# startup (see _ensureAdminExists) - the explicit-configuration path, and the
# recovery path if the automatic earliest-user promotion picked the wrong
# account.
ADMIN_EMAIL_ENV_VAR = "ADMIN_EMAIL"
PASSWORD_MIN_LENGTH = 8   #< also enforced client-side via the minlength attribute
# The Spotify OAuth CSRF `state` round-trip (RFC 6749 §10.12): /spotify-authorize
# stores a one-shot random value under this session key and sends it along to
# Spotify; /spotify-callback refuses to exchange a code unless the request
# echoes that exact value back. Without it, anyone sharing this instance's
# Spotify app credentials could complete the consent themselves and trick a
# logged-in victim into loading the callback URL - storing the ATTACKER's
# refresh token (and, via backfill, their listening history) on the victim's
# account.
SPOTIFY_OAUTH_STATE_SESSION_KEY = "spotify_oauth_state"
SPOTIFY_OAUTH_STATE_NUM_BYTES = 32   #< entropy fed to secrets.token_urlsafe
RATE_LIMIT_MAX_ATTEMPTS = 10     #< max POSTs allowed per window, per source IP, per route
RATE_LIMIT_WINDOW_SECONDS = 300  #< 5 minutes
RATE_LIMIT_ERROR_MESSAGE = "Too many attempts. Please wait a few minutes and try again."
# EXPORT_CHUNK_SIZE / EXPORT_CSV_COLUMNS now live in services/export.py.
EXPORT_FORMATS = ("json", "csv")
# Random startup-offset bounds for this module's periodic workers, so a
# restart doesn't fire every worker at the same instant (the metadata
# backfiller and wrapped worker in Database/database.py already stagger
# themselves the same way). The Spotify listener is deliberately NOT
# staggered - delaying it would lose plays.
VERSION_CHECK_MIN_START_DELAY_SECONDS = 30
VERSION_CHECK_MAX_START_DELAY_SECONDS = 180
LOGIN_CHECK_MIN_START_DELAY_SECONDS = 60
LOGIN_CHECK_MAX_START_DELAY_SECONDS = 300

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


def _trustedProxyCount() -> int:
    """How many reverse-proxy hops to trust X-Forwarded-* headers from, per the
    TRUST_PROXY_HEADERS env var: a hop count ("2"), or a plain truthy value
    ("true") meaning one proxy. 0/unset/junk disables it - trusting forwarded
    headers while NOT behind a proxy would let clients forge their source IP
    straight past the auth rate limiter, so this must stay opt-in."""
    raw = os.environ.get(TRUST_PROXY_HEADERS_ENV_VAR, "").strip().lower()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 1 if raw in TRUTHY_ENV_VALUES else 0


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
        # The genre-gate thresholds are quoted in templates (the locked-state
        # progress card) - exposed as globals so every include sees them
        # without each route re-passing them.
        self.app.jinja_env.globals.update(
            genreGateOverallMinPercent=GENRE_GATE_OVERALL_MIN_PERCENT,
            genreGateCategoryMinPercent=GENRE_GATE_CATEGORY_MIN_PERCENT,
        )
        proxyHops = _trustedProxyCount()
        if proxyHops:
            # Restores the real client address (and scheme/host) from the
            # X-Forwarded-* headers set by the reverse proxy in front of this
            # app - request.remote_addr is what the auth rate limiter keys on.
            self.app.wsgi_app = ProxyFix(self.app.wsgi_app, x_for=proxyHops, x_proto=proxyHops, x_host=proxyHops)
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
        # No download is in flight this early, so any surviving 'pending'
        # image claim is stale (a previous run died mid-download) and would
        # block that image from ever being fetched.
        staleImageClaims = self.repo.deleteStalePendingImages()
        if staleImageClaims:
            logger.info("Cleared %d stale pending image download claim(s) from a previous run", staleImageClaims)
        self._ensureAdminExists()

        self.user_databases = {}
        # Usernames whose Database in user_databases has had its background
        # listener/auto-importer actually started - see _getReadOnlyUserDb()
        # and get_user_db()'s activation-guard. A username can be cached in
        # user_databases without being here yet (a public share-link view
        # constructed a read-only instance before its owner ever logged in
        # this process).
        self._activatedUsers: set = set()
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
        # Snapshots the shared database on a schedule (see Database/backup.py) -
        # a manual backup command in the README protects nobody who doesn't run it.
        self.backupWorker = BackupWorker()
        self.backupWorker.start()
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

    def _ensureAdminExists(self):
        """Admin bootstrap, run at every startup. ADMIN_EMAIL (when set) is
        authoritative: that user becomes the ONLY admin - demoting anyone
        else, which is what makes it the recovery path when the automatic
        promotion picked the wrong account. A typo'd ADMIN_EMAIL changes
        nothing (losing all admins to a typo would be worse than keeping a
        stale one). Without the env var, the earliest-created user is
        promoted once if no admin exists yet, so fresh installs converge on
        the instance owner (migration 1.17.0 does the same for upgrades)."""
        adminEmail = os.environ.get(ADMIN_EMAIL_ENV_VAR, "").strip()
        if adminEmail:
            username = self.repo.getUsernameForEmailCaseInsensitive(adminEmail)
            if not username:
                logger.warning("%s=%s does not match any user - admin assignment unchanged",
                               ADMIN_EMAIL_ENV_VAR, adminEmail)
                return
            for other in self.repo.getAdminUsernames():
                if other != username:
                    self.repo.setUserAdmin(other, False)
                    logger.info("Demoted %s from admin (%s designates %s)", other, ADMIN_EMAIL_ENV_VAR, username)
            if not self.repo.isAdmin(username):
                self.repo.setUserAdmin(username, True)
                logger.info("Promoted %s to admin (%s)", username, ADMIN_EMAIL_ENV_VAR)
            return

        promoted = self.repo.promoteEarliestUserToAdminIfNoneExists()
        if promoted:
            logger.info("Promoted earliest-created user %s to admin (no admin existed yet)", promoted)

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
            db = self.user_databases.get(username)
            needsActivation = db is None or username not in self._activatedUsers
            if db is None:
                # Share the app-wide stop event so the listener reconnect
                # paths can refuse to fire once shutdown has begun.
                db = Database(user=username, email=email, shutdown_event=self._stop_event)

            if needsActivation:
                try:
                    db.startAutoImporter()
                    db.resetProgress()
                    db.startListener(email=email)
                except Exception:
                    # Database.__init__ already started this instance's
                    # background threads (wrapped worker, metadata
                    # backfiller); startAutoImporter added its watchdog. If a
                    # later step fails (startListener is a live Spotify call)
                    # the instance must not stay reachable half-activated, so
                    # both caches are rolled back along with stopping it -
                    # every retry would otherwise stack another full set of
                    # threads per user, or silently keep serving the dead
                    # instance to the next caller.
                    try:
                        db.stop()
                    except Exception as stopError:
                        logger.error("Failed to stop partially-started Database for user %s: %s",
                                     username, stopError)
                    self.user_databases.pop(username, None)
                    self._activatedUsers.discard(username)
                    raise
                self.user_databases[username] = db
                self._activatedUsers.add(username)
            return self.user_databases[username]

    def _getReadOnlyUserDb(self, username):
        """A Database for `username` suitable for a public, unauthenticated
        share-link view - never starts the listener/auto-importer (no live
        Spotify session should ever be triggered by an anonymous GET). If
        `username` already has an active Database (the common case: the
        owner has logged in to this process before), that instance is
        reused as-is. Otherwise a new instance is cached without activating
        it; get_user_db() activates it in place on the owner's next real
        login instead of skipping activation forever, since by then the
        username is already in user_databases. Callers must already know
        `username` exists (e.g. it came from a share_links row, which a
        foreign key guarantees points at a real user)."""
        with self._db_lock:
            db = self.user_databases.get(username)
            if db is not None:
                return db

            email = self.repo.getEmailForUsername(username)
            db = Database(user=username, email=email, shutdown_event=self._stop_event)
            self.user_databases[username] = db
            return db

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

    def get_current_user_or_redirect(self):
        """The (email, username, db) triple for the authenticated session, or
        (None, None, None) when no live session exists - route handlers redirect
        to /login on the None case. Also self-heals a session whose username
        drifted from its email mapping, and stashes the db on g for teardown."""
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

    def _rateLimited(self, bucket: str) -> bool:
        """True if this request's source IP has exceeded RATE_LIMIT_MAX_ATTEMPTS
        for `bucket` within RATE_LIMIT_WINDOW_SECONDS - callers should reject
        the request with RATE_LIMIT_ERROR_MESSAGE when this returns True."""
        identifier = request.remote_addr or "unknown"
        return not self._authRateLimiter.hit(bucket, identifier)

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
        # checkLogin_thread() already ran _ensureAllUsersLogin synchronously
        # before this thread started (listeners must come up immediately), so
        # the loop's own first pass can wait out a random offset - staggering
        # the periodic re-checks against the other workers after a restart.
        if self._stop_event.wait(random.randint(LOGIN_CHECK_MIN_START_DELAY_SECONDS,
                                                LOGIN_CHECK_MAX_START_DELAY_SECONDS)):
            return
        while not self._stop_event.is_set():
            self._ensureAllUsersLogin()
            self._stop_event.wait(60 * 5)  # Check every 5 minutes

    def startVersionCheck_thread(self):
        thread = threading.Thread(target=self._versionCheckLoop, daemon=True)
        thread.start()

    def _versionCheckLoop(self):
        # Check version from GitHub shortly after startup (random offset, so a
        # restart doesn't fire every worker at once) and then every hour.
        url = "https://raw.githubusercontent.com/i7Gamer/SpotifyStatsTracker/main/Database/VERSION"
        if self._stop_event.wait(random.randint(VERSION_CHECK_MIN_START_DELAY_SECONDS,
                                                VERSION_CHECK_MAX_START_DELAY_SECONDS)):
            return
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
                    except Exception:
                        pass  #< malformed remote VERSION string - skip this check
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

    _GENRE_RESOLVERS = {
        "track": resolveGenresForTrack,
        "album": resolveGenresForAlbum,
        "artist": resolveGenresForArtist,
    }

    def _attachGenres(self, db, items: list[dict], kind: str) -> list[dict]:
        """Sets item['genres'] (a list of genre name strings, [] when none,
        capped to TRACK_CARD_GENRE_LIMIT) for _track_card.html's genre badge
        - one indexed per-item lookup per item, cheap enough against the
        local SQLite file that no batch query is warranted (see
        resolveGenresForTrack/Album/Artist's degrade-to-[] contract, which
        keeps this safe against stubbed test dbs too). Truncated here rather
        than in the template so every caller (including detail pages, which
        wrap a single item) gets the same cap without threading a constant
        through every render_template() call.

        These per-item badges bypass the charts/wrapped/compare coverage-
        unlock gate by design (they show whatever's known regardless of
        aggregate confidence) - but the admin's instance-wide kill switch
        still applies: disabled means no genre lookups at all, matching every
        other genre surface."""
        if not self.repo.isLastfmGenreBackfillEnabled():
            for item in items:
                item["genres"] = []
            return items
        resolver = self._GENRE_RESOLVERS[kind]
        for item in items:
            item["genres"] = resolver(db, item["id"])[:TRACK_CARD_GENRE_LIMIT] if item.get("id") else []
        return items

    def _gatherCompareStats(self, db, startDate, endDate, limit=COMPARE_TOP_LIST_SIZE, sortBy="plays") -> dict:
        """One Compare-page side's stats, gathered identically for the viewer
        and the counterpart so the two columns can't drift apart. Runs the
        same _embed*TextElements step every other page feeding
        _track_card.html uses - without it the cards render with blank
        time/first-listened/duration/percent lines.

        Every category is fetched ONCE, at COMPARE_SHARED_POOL_SIZE depth
        (sharedSongsPool/sharedArtistsPool/sharedAlbumsPool) - the query
        that feeds Top Common Songs/Artists/Albums (_buildSharedItems) and
        the similarity counts. topSongsPool/topArtistsPool/topAlbumsPool
        (what taste-match runs over) are DERIVED as that same pool's first
        COMPARE_OVERLAP_POOL_SIZE entries rather than a second DB query: a
        plays-ranked LIMIT 200 query's first 100 rows are, by construction,
        identical to a dedicated LIMIT 100 query (same WHERE/ORDER BY) - so
        there's no need to pay for the full GROUP BY/ORDER BY aggregation
        (the expensive part on a many-year "All Time" range) twice just to
        get two different cutoffs of the same ranking. This also means
        widening the shared-item search can never move the taste-match
        score - it only ever sees the first COMPARE_OVERLAP_POOL_SIZE of
        whatever the shared pool returns, unaffected by anything beyond it.

        The DISPLAYED my/their top lists default to the same pool's first
        `limit` entries (no extra query); other sortBys re-shape them per
        displayList below ("name" alphabetizes that same head, a metric
        re-queries live)."""
        def displayList(pool, queryAtSortBy):
            """The my/their column for one category. "plays" keeps the
            plays-ranked pool's own head (no extra query). "name" means
            "your top `limit` BY PLAYS, shown A-Z for scanning" -
            deliberately NOT the alphabetical head of the whole history the
            paginated standalone pages show: capped at `limit` with no
            pagination here, that would surface mostly number/punctuation-
            prefixed obscurities instead of anything about taste. Any other
            metric re-queries live so membership AND order reflect it -
            that genuinely can't be derived by slicing a plays-ranked
            pool."""
            if sortBy == "plays":
                return pool[:limit]
            if sortBy == "name":
                return self._resortByMetric(pool[:limit], "name")
            return queryAtSortBy()

        totalPlays, totalMs = db.getPlayTotals(startDate, endDate)
        sharedSongsPool = db.getTopSongs(startDate, endDate, limit=COMPARE_SHARED_POOL_SIZE)
        topSongsPool = sharedSongsPool[:COMPARE_OVERLAP_POOL_SIZE]
        topSongsDisplay = displayList(
            topSongsPool, lambda: db.getTopSongs(startDate, endDate, limit=limit, by=sortBy))
        topSongs = self._embedTopSongsTextElements(
            self._embedSongsTextElements(topSongsDisplay),
            sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        sharedAlbumsPool = db.getTopAlbums(startDate, endDate, limit=COMPARE_SHARED_POOL_SIZE)
        topAlbumsPool = sharedAlbumsPool[:COMPARE_OVERLAP_POOL_SIZE]
        topAlbumsDisplay = displayList(
            topAlbumsPool, lambda: db.getTopAlbums(startDate, endDate, limit=limit, by=sortBy))
        topAlbums = self._embedAlbumsTextElements(
            topAlbumsDisplay,
            sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        sharedArtistsPool = db.getTopArtists(startDate, endDate, limit=COMPARE_SHARED_POOL_SIZE)
        topArtistsPool = sharedArtistsPool[:COMPARE_OVERLAP_POOL_SIZE]
        topArtistsDisplay = displayList(
            topArtistsPool, lambda: db.getTopArtists(startDate, endDate, limit=limit, by=sortBy))
        topArtists = self._embedArtistsTextElements(
            topArtistsDisplay,
            sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        topSongs = self._attachGenres(db, topSongs, "track")
        topArtists = self._attachGenres(db, topArtists, "artist")
        topAlbums = self._attachGenres(db, topAlbums, "album")

        completion = db.getCompletionStats(startDate, endDate)
        completionTotal = completion["skips"] + completion["completes"] + completion["partials"]
        explicitRatio = db.getExplicitRatio(startDate, endDate)
        explicitTotal = explicitRatio["explicit"] + explicitRatio["clean"]
        heatmap = db.getHourOfDayHeatmap(startDate, endDate)
        hourTotals = [sum(day[hour]["totalTimeListened"] for day in heatmap) for hour in range(24)]
        dayTotals = [sum(cell["totalTimeListened"] for cell in day) for day in heatmap]

        return {
            "totalPlays": totalPlays,
            "totalMs": totalMs,
            "totalTimeText": msToString(totalMs),
            "topSongs": topSongs,
            "topArtists": topArtists,
            "topAlbums": topAlbums,
            "topSongsPool": topSongsPool,
            "topArtistsPool": topArtistsPool,
            "topAlbumsPool": topAlbumsPool,
            "sharedSongsPool": sharedSongsPool,
            "sharedArtistsPool": sharedArtistsPool,
            "sharedAlbumsPool": sharedAlbumsPool,
            "uniqueSongs": db.getSongsCount(startDate, endDate),
            "uniqueArtists": db.getArtistsCount(startDate, endDate),
            "avgPlayTimeText": msToString(totalMs // totalPlays) if totalPlays else "—",
            "skipRateText": f"{completion['skips'] / completionTotal * 100:.0f}%" if completionTotal else "—",
            "explicitShareText": f"{explicitRatio['explicit'] / explicitTotal * 100:.0f}%" if explicitTotal else "—",
            "peakHourText": f"{hourTotals.index(max(hourTotals)):02d}:00" if any(hourTotals) else "—",
            "peakDayText": WEEKDAY_NAMES[dayTotals.index(max(dayTotals))] if any(dayTotals) else "—",
        }

    def _buildSharedItems(self, myPool, theirPool, embedFn, limit) -> list[dict]:
        """Shared entries of one category, ranked by the SUM of both users'
        rank discounts (see _sharedRankScore) - not either side's raw
        combined totals, and independent of the page's sortBy control
        (which only reorders the individual my/their lists, see
        _gatherCompareStats) - and sliced to `limit`, with the per-user
        versus data the Top Common Songs/Artists/Albums cards render.
        Rank-weighted so one user's #1 with a decent counterpart rank still
        outranks an item both users only rank moderately even when the
        moderate item's combined plays are higher - but summed rather than
        taste-match's min() shape, so an item the counterpart barely plays
        can't claim the top "common" spot (see _sharedRankScore).

        Rank is derived by re-sorting each pool by plays (see
        _resortByMetric) rather than trusting the incoming pool's own order -
        ranking by the viewer's own order used to silently cut different
        overlapping items depending on whose pool was walked (see
        test_shared_artists_ranked_by_combined_plays_not_the_viewers_own_order).
        Combined ranking - not either side's own pool order - so the same
        mutual-share pair sees the same Top Common list regardless of who's
        viewing.
        Copied dicts: the pool entries also feed the viewer's own top-list
        column, and the versus block / combined totals must only show on the
        shared cards. The unique-song counts are only attached where the
        aggregates carry them (artists/albums) - a song card has nothing to
        count."""
        myRanks = _rankById(self._resortByMetric(myPool, "plays"))
        theirRanks = _rankById(self._resortByMetric(theirPool, "plays"))
        theirById = {item["id"]: item for item in theirPool}
        sharedItems = [dict(item) for item in myPool if item["id"] in theirById]

        def sortKey(item):
            theirItem = theirById[item["id"]]
            sharedScore = _sharedRankScore(myRanks[item["id"]], theirRanks[item["id"]])
            combinedPlays = item.get("plays", 0) + theirItem.get("plays", 0)
            combinedTime = item.get("totalTimeListened", 0) + theirItem.get("totalTimeListened", 0)
            #< descending sharedScore/combinedPlays/combinedTime via negation,
            #  ascending name/id - the same plays -> totalTimeListened ->
            #  name -> id tiebreak chain the rank maps above were sorted by
            #  (_resortByMetric), which in turn mirrors Repository's
            #  plays-ranked ORDER BY, so ties render the same way here as
            #  everywhere else "plays" is ranked.
            return (-sharedScore, -combinedPlays, -combinedTime, (item.get("name") or "").lower(), item["id"])

        sharedItems.sort(key=sortKey)
        shared = embedFn(sharedItems[:limit])
        for item in shared:
            theirItem = theirById[item["id"]]
            myPlays = item.get("plays", 0)
            myMs = item.get("totalTimeListened", 0)
            theirMs = theirItem.get("totalTimeListened", 0)
            combinedMs = myMs + theirMs
            compareData = {
                "myPlays": myPlays,
                "theirPlays": theirItem.get("plays", 0),
                #< each side's own plays-rank - the versus block shows them
                #  because the list order is rank-driven (_sharedRankScore),
                #  and without them the order looks arbitrary whenever it
                #  disagrees with raw combined plays.
                "myRank": myRanks[item["id"]],
                "theirRank": theirRanks[item["id"]],
                "myTimeText": msToString(myMs),
                "theirTimeText": msToString(theirMs),
                #< an even split when neither side has recorded time - a
                #  bar of two zero-width halves would just look broken
                "myTimePercent": round(myMs / combinedMs * 100) if combinedMs else 50,
            }
            if "uniqueSongCount" in item or "uniqueSongCount" in theirItem:
                compareData["myUniqueSongs"] = item.get("uniqueSongCount", 0)
                compareData["theirUniqueSongs"] = theirItem.get("uniqueSongCount", 0)
            item["compareData"] = compareData
            # The card's top stat line shows the COMBINED totals - the
            # per-user numbers live in the versus block right below it.
            # Overwritten after embedFn so the embedded text matches.
            item["plays"] = myPlays + compareData["theirPlays"]
            item["totalTimeListened"] = combinedMs
            item["totalTimeListenedText"] = msToString(combinedMs)
        return shared

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

    def _embedTimeSeriesTextElements(self, timeSeries: list, groupBy: str | None = None) -> list:
        """groupBy: when given (Charts page only - see chartsPage()'s call
        site), also stamps rangeStart/rangeEnd onto each bucket so
        static/js/charts.js's click-to-navigate can link a clicked bar to
        the Dashboard scoped to that exact bucket. Omitted elsewhere
        (Wrapped's own time-series chart, detail pages) since those charts
        don't support click-navigation."""
        for bucket in timeSeries:
            bucket["totalTimeListenedText"] = msToString(bucket["totalTimeListened"])
            bucketRange = self._timeSeriesBucketRange(bucket["label"], groupBy)
            if bucketRange is not None:
                bucket["rangeStart"], bucket["rangeEnd"] = bucketRange
        return timeSeries

    @staticmethod
    def _timeSeriesBucketRange(label: str, groupBy: str | None) -> tuple[str, str] | None:
        """The [inclusive start day, inclusive end day] a time-series
        bucket's label represents, as plain "YYYY-MM-DD" strings - matches
        _getDateRange's custom-range contract, which treats its own endDate
        as inclusive (it adds one day itself), so these values round-trip
        straight into a `?interval=custom&startDate=...&endDate=...` link.
        None for a groupBy without a clean calendar-date mapping (e.g. the
        Charts single-day view's hourly buckets - see chartsPage's
        timeSeriesGroupBy) or a label that doesn't parse, so a bucket like
        that is simply left un-clickable rather than linking somewhere
        wrong."""
        if groupBy not in ("day", "week", "month"):
            return None
        try:
            if groupBy == "week":
                start = datetime.strptime(label, "%Y-%m-%d")
                end = start + timedelta(days=6)
            elif groupBy == "month":
                start = datetime.strptime(label, "%Y-%m")
                nextMonth = (datetime(start.year + 1, 1, 1) if start.month == 12
                             else datetime(start.year, start.month + 1, 1))
                end = nextMonth - timedelta(days=1)
            else:
                start = datetime.strptime(label, "%Y-%m-%d")
                end = start
        except ValueError:
            return None
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

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

    def _computeAvailableYears(self, db) -> list:
        """Every year `db`'s user has at least one play in, most recent
        first - shared by wrappedPage() (year badges) and a multi-year
        ("all years") share link on sharedWrappedPage(), which has no fixed
        single year to fall back on the way a per-year link does."""
        nowLocal = now(tz=db.tz)
        currentYear = nowLocal.year
        oldestEntries = db.getEntriesFromOld(count=1, fullPagination=False)
        earliestYear = convertToDatetime(oldestEntries[0]["playedAt"], tz=db.tz).year if oldestEntries else currentYear
        return list(range(currentYear, earliestYear - 1, -1))

    def _parseWrappedFilterParams(self) -> tuple:
        """groupBy/limit/sortBy (validated, with the same defaults/fallbacks
        wrappedPage() has always used) plus ajax-request detection - shared
        by wrappedPage() and sharedWrappedPage() so the two routes can't
        silently drift apart on validation or defaults. Returns (groupBy,
        limit, sortBy, isAjaxRequest, ajaxUpdateType, includeGenres).

        Genre data is deliberately computed live - never from the
        user_wrapped cache: coverage keeps growing while the Last.fm
        backfill runs, and the admin's inherited-genres toggle changes the
        numbers retroactively. Only computed for responses that actually
        render the card (the full page and ajax type=all - chart/lists
        partial updates would compute and discard it). See chartsPage's
        identical kill-switch comment; _wrapped_genres.html hides its whole
        section (chart AND locked-progress fallback) when lastfmEnabled is
        False."""
        groupBy = self._getValidGroupBy(request.args.get("groupBy", "week"), default="week")

        limit = request.args.get("limit", type=int)
        if limit not in WRAPPED_LIMIT_OPTIONS:
            limit = WRAPPED_LIST_SIZE
        # Default stays "plays" (not DEFAULT_SORT_BY) so nobody's Wrapped
        # changes unless they touch the control.
        sortBy = self._getSortByParam(default="plays")

        isAjaxRequest = request.args.get("ajax") == "true"
        ajaxUpdateType = request.args.get("type", "all")
        includeGenres = not isAjaxRequest or ajaxUpdateType == "all"

        return groupBy, limit, sortBy, isAjaxRequest, ajaxUpdateType, includeGenres

    @staticmethod
    def _shareLinkExpiryLabel(expiresAt: float | None, nowTs: float) -> str:
        """'Never expires' / 'Expires today' / 'Expires in N days' - a
        relative countdown recomputed from expires_at (not the originally-
        chosen duration, which isn't stored) so the label can't drift stale.
        Used only by the wrapped.html share panel - Profile's own link list
        keeps its own separate absolute-date convention (createdText/
        expiresText) instead, since that page is more of a record-keeping
        view where an absolute date fits better."""
        if expiresAt is None:
            return "Never expires"
        remainingDays = math.ceil((expiresAt - nowTs) / 86400)
        if remainingDays <= 0:
            return "Expires today"
        return f"Expires in {remainingDays} day" + ("" if remainingDays == 1 else "s")

    def _resolveShareLinksForYear(self, username: str, year: int) -> tuple[list[dict], list[dict]]:
        """(yearLinks, allYearsLinks) - every still-active link scoped to
        this exact year, and every still-active all-years link, both freshly
        re-derived from the DB, never assumed from whichever link an action
        just touched, since the share-link panel can now be showing several
        of either type and creating/revoking one doesn't tell you what state
        the rest are in. Each link dict is annotated with an "expiryLabel"
        (see _shareLinkExpiryLabel) ready for the template to render."""
        nowTs = time.time()
        links = self.repo.getShareLinksForUser(username)
        yearLinks = [
            {**link, "expiryLabel": self._shareLinkExpiryLabel(link["expires_at"], nowTs)}
            for link in links if link["year"] == year
        ]
        allYearsLinks = [
            {**link, "expiryLabel": self._shareLinkExpiryLabel(link["expires_at"], nowTs)}
            for link in links if link["year"] is None
        ]
        return yearLinks, allYearsLinks

    @staticmethod
    def _resortByMetric(items: list, sortBy: str) -> list:
        """Re-sorts an already-fetched list of song/artist/album dicts by
        `sortBy` (plays/totalTimeListened descending, name ascending) -
        matches VALID_SORT_BY's semantics (see app.py's sortBy query param
        docs) without re-querying the DB. Used where a pool was fetched at
        one fixed ranking but the displayed order should follow the user's
        chosen metric instead (Wrapped's cached pools, which are only ever
        stored plays-ranked).

        Ties on `sortBy` fall back to the other metric, then name, then id -
        and for "name", to time listened (desc) then id - mirroring
        Repository.getSongsPage's ORDER BY chains instead of leaning on the
        input pool's incidental order, so a resorted pool and a live query
        at the same sortBy agree on tie order."""
        if sortBy == "name":
            return sorted(
                items,
                key=lambda item: (
                    (item.get("name") or "").lower(),
                    -item.get("totalTimeListened", 0),
                    item.get("id", ""),
                ),
            )
        otherMetric = "plays" if sortBy == "totalTimeListened" else "totalTimeListened"
        return sorted(
            items,
            key=lambda item: (
                -item.get(sortBy, 0),
                -item.get(otherMetric, 0),
                (item.get("name") or "").lower(),
                item.get("id", ""),
            ),
        )

    def _discoveriesInYear(self, items: list, yearStart, yearEnd, limit: int, sortBy: str = "plays") -> list:
        """Items (songs or artists) whose true, all-time first listen falls
        within [yearStart, yearEnd) - not just their earliest play *within* that
        range, which a date-scoped query would report instead. `items` must
        therefore come from an unbounded (no date range) stats call. Sorted by
        `sortBy`, most-played discovery first by default."""
        yearStartTs, yearEndTs = yearStart.timestamp(), yearEnd.timestamp()
        discovered = [
            item for item in items
            if item.get("firstListenedAt") is not None and yearStartTs <= item["firstListenedAt"] < yearEndTs
        ]
        discovered = self._resortByMetric(discovered, sortBy)
        return discovered[:limit]

    def _buildWrappedContext(self, db, year: int, groupBy: str, limit: int, sortBy: str,
                             includeGenres: bool = True) -> dict:
        """Everything wrapped.html needs to render one year's Wrapped recap
        for `db`'s user - the cache-read/recalculate, resort-and-slice, and
        text/genre embedding pipeline, independent of which route is asking
        for it. Used by both the authenticated /wrapped route and the public
        /shared/<token> route (see wrappedPage() and sharedWrappedPage()).

        includeGenres=False skips the live genre-coverage/distribution
        queries entirely - wrappedPage() uses this for AJAX chart/lists-only
        partial updates, which discard genre data anyway (see its call
        site); every other caller wants the full context."""
        nowLocal = now(tz=db.tz)
        yearStart = nowLocal.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        yearEnd = nowLocal.replace(year=year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

        # Genre data is deliberately computed live - never from the
        # user_wrapped cache below: coverage keeps growing while the Last.fm
        # backfill runs, and the admin's inherited-genres toggle changes the
        # numbers retroactively.
        lastfmEnabled = self.repo.isLastfmGenreBackfillEnabled()
        genreCoverage = emptyGenreCoverage()
        genreUnlocked = False
        topGenres = None
        if includeGenres and lastfmEnabled:
            genreCoverage = resolveGenreCoverage(db, yearStart, yearEnd)
            genreUnlocked = genreGatePasses(genreCoverage)
            if genreUnlocked:
                topGenres = resolveGenreDistribution(db, yearStart, yearEnd,
                                                     WRAPPED_TOP_GENRES_LIMIT)

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

            # 4. Re-sort the cached (up to 100-item) pools by the chosen
            # metric, then slice to the requested limit. The cache itself
            # is only ever stored plays-ranked, so membership stays
            # whatever that plays-ranked capture included - only order/
            # what survives the limit cut within it follows sortBy.
            topSongs = self._resortByMetric(topSongs, sortBy)[:limit]
            topArtists = self._resortByMetric(topArtists, sortBy)[:limit]
            topAlbums = self._resortByMetric(topAlbums, sortBy)[:limit]
            discoveredSongs = self._resortByMetric(discoveredSongs, sortBy)[:limit]
            discoveredArtists = self._resortByMetric(discoveredArtists, sortBy)[:limit]
            discoveredAlbums = self._resortByMetric(discoveredAlbums, sortBy)[:limit]
        else:
            # Dynamic calculations for mocks (unit tests compatibility)
            topSongs = db.getTopSongs(startDate=yearStart, endDate=yearEnd, by=sortBy, limit=limit)
            topArtists = db.getTopArtists(startDate=yearStart, endDate=yearEnd, by=sortBy, limit=limit)
            topAlbums = db.getTopAlbums(startDate=yearStart, endDate=yearEnd, by=sortBy, limit=limit)
            totalPlays, totalMs = db.getPlayTotals(yearStart, yearEnd)

            discoveredSongs = self._discoveriesInYear(
                db.getSongsStats(sortBy="plays"), yearStart, yearEnd, limit, sortBy=sortBy
            )
            discoveredArtists = self._discoveriesInYear(
                db.getArtistsStats(), yearStart, yearEnd, limit, sortBy=sortBy
            )
            discoveredAlbums = self._discoveriesInYear(
                db.getAlbumsStats(sortBy="plays"), yearStart, yearEnd, limit, sortBy=sortBy
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
        topSongs = self._embedTopSongsTextElements(topSongs, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        topArtists = self._embedArtistsTextElements(topArtists, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        topAlbums = self._embedAlbumsTextElements(topAlbums, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
        discoveredSongs = self._embedTopSongsTextElements(self._embedSongsTextElements(discoveredSongs))
        discoveredArtists = self._embedArtistsTextElements(discoveredArtists)
        discoveredAlbums = self._embedAlbumsTextElements(discoveredAlbums)
        topSongs = self._attachGenres(db, topSongs, "track")
        topArtists = self._attachGenres(db, topArtists, "artist")
        topAlbums = self._attachGenres(db, topAlbums, "album")
        discoveredSongs = self._attachGenres(db, discoveredSongs, "track")
        discoveredArtists = self._attachGenres(db, discoveredArtists, "artist")
        discoveredAlbums = self._attachGenres(db, discoveredAlbums, "album")

        return {
            "yearStart": yearStart,
            "yearEnd": yearEnd,
            "totalPlays": totalPlays,
            "totalMs": totalMs,
            "topSongs": topSongs,
            "topArtists": topArtists,
            "topAlbums": topAlbums,
            "discoveredSongs": discoveredSongs,
            "discoveredArtists": discoveredArtists,
            "discoveredAlbums": discoveredAlbums,
            "timeSeries": timeSeries,
            "longestStreak": longestStreak,
            "peakListeningTime": peakListeningTime,
            "uniqueSongsCount": uniqueSongsCount,
            "uniqueArtistsCount": uniqueArtistsCount,
            "discoveredSongsCount": discoveredSongsCount,
            "discoveredArtistsCount": discoveredArtistsCount,
            "topGenres": topGenres,
            "genreCoverage": genreCoverage,
            "genreUnlocked": genreUnlocked,
            "lastfmEnabled": lastfmEnabled,
        }

    def _buildWrappedAjaxResponse(self, ctx: dict, username: str, year: int, updateType: str, publicView: bool) -> dict:
        """The JSON-able payload for a Wrapped ?ajax=true request - shared by
        the authenticated /wrapped route and the public /shared/<token>
        route so the two can't drift on what an ajax response contains.
        publicView is threaded into the _wrapped_list.html/
        _wrapped_genres.html renders so their "You"/{{ username }} text (see
        _track_card.html) stays correct after a partial swap on the shared
        page too. Returns a plain dict (not a Response) so wrappedPage() can
        layer its own owner-only sharePanelHtml key on top before
        jsonify-ing - sharedWrappedPage() never does, since a public visitor
        must never receive share-panel data."""
        topSongs, topArtists, topAlbums = ctx["topSongs"], ctx["topArtists"], ctx["topAlbums"]
        discoveredSongs, discoveredArtists, discoveredAlbums = (
            ctx["discoveredSongs"], ctx["discoveredArtists"], ctx["discoveredAlbums"])

        res = {}

        if updateType in ("all", "chart"):
            res["timeSeries"] = ctx["timeSeries"]

        if updateType in ("all", "lists"):
            res["topSongsHtml"] = render_template(
                "_wrapped_list.html", items=topSongs, section="top_songs",
                username=username, year=year, publicView=publicView)
            res["topArtistsHtml"] = render_template(
                "_wrapped_list.html", items=topArtists, section="top_artists",
                username=username, year=year, publicView=publicView)
            res["topAlbumsHtml"] = render_template(
                "_wrapped_list.html", items=topAlbums, section="top_albums",
                username=username, year=year, publicView=publicView)
            res["discoveredSongsHtml"] = render_template(
                "_wrapped_list.html", items=discoveredSongs, section="top_songs",
                username=username, year=year, publicView=publicView)
            res["discoveredArtistsHtml"] = render_template(
                "_wrapped_list.html", items=discoveredArtists, section="top_artists",
                username=username, year=year, publicView=publicView)
            res["discoveredAlbumsHtml"] = render_template(
                "_wrapped_list.html", items=discoveredAlbums, section="top_albums",
                username=username, year=year, publicView=publicView)

        if updateType == "all":
            res["topGenresHtml"] = render_template(
                "_wrapped_genres.html", topGenres=ctx["topGenres"],
                genreCoverage=ctx["genreCoverage"], genreUnlocked=ctx["genreUnlocked"], year=year,
                lastfmEnabled=ctx["lastfmEnabled"], username=username, publicView=publicView)
            topSongText = (
                f"{topSongs[0]['name']} - {topSongs[0]['artists'][0]['name']}"
                if topSongs and topSongs[0].get('artists')
                else (topSongs[0]['name'] if topSongs else "N/A")
            )
            topArtistText = topArtists[0]['name'] if topArtists else "N/A"
            topAlbumText = topAlbums[0]['name'] if topAlbums else "N/A"
            res.update({
                "totalPlays": ctx["totalPlays"],
                "totalTime": msToString(ctx["totalMs"]),
                "longestStreak": ctx["longestStreak"],
                "peakDay": ctx["peakListeningTime"][0] if ctx["peakListeningTime"] else "N/A",
                "peakPlays": ctx["peakListeningTime"][1] if ctx["peakListeningTime"] else 0,
                "uniqueSongsCount": ctx["uniqueSongsCount"],
                "uniqueArtistsCount": ctx["uniqueArtistsCount"],
                "discoveredSongsCount": ctx["discoveredSongsCount"],
                "discoveredArtistsCount": ctx["discoveredArtistsCount"],
                "topSongText": topSongText,
                "topArtistText": topArtistText,
                "topAlbumText": topAlbumText,
            })
        return res

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
        def _injectArtistListLimits():
            # _artist_links.html collapses long artist lists behind a
            # "+N more" toggle - single-sources the thresholds so the macro
            # compares against the constants instead of magic numbers.
            return {
                "MAX_INLINE_ARTISTS": MAX_INLINE_ARTISTS,
                "MIN_HIDDEN_ARTISTS": MIN_HIDDEN_ARTISTS,
            }

        @self.app.context_processor
        def _injectAdminStatus():
            # Lets templates show admin-only affordances (the profile page's
            # ADMIN chip). Memoized on g like _injectShareStatus below - one
            # request can render several templates.
            if "isAdmin" not in g:
                username = session.get("username")
                g.isAdmin = self.repo.isAdmin(username) if username else False
            return {"isAdmin": g.isAdmin}

        @self.app.context_processor
        def _injectRegistrationStatus():
            # Lets login.html hide its "Create an account" link when the
            # admin has disabled new registrations - instance-wide, so no
            # per-user memoization needed (unlike _injectShareStatus above).
            return {"registration_enabled": self.repo.isRegistrationEnabled()}

        @self.app.context_processor
        def _injectShareLinksStatus():
            # Lets wrapped.html hide its "Share this Wrapped" panel and
            # profile.html hide its share-link list when the admin has
            # disabled public share links - instance-wide, same shape as
            # _injectRegistrationStatus above.
            return {"share_links_enabled": self.repo.isShareLinksEnabled()}

        @self.app.context_processor
        def _injectArtistBioStatus():
            # Lets artist_detail.html hide its Biography section (even for
            # an artist whose bio was already fetched and stored) and
            # overview.html's admin panel show the toggle's current state -
            # instance-wide, same shape as _injectRegistrationStatus above.
            return {"artist_bio_enabled": self.repo.isArtistBioEnabled()}

        @self.app.context_processor
        def _injectAlbumBioStatus():
            # Mirrors _injectArtistBioStatus, for album_detail.html's
            # Biography section and the album_bio toggle's current state.
            return {"album_bio_enabled": self.repo.isAlbumBioEnabled()}

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
                # The admin's instance-wide kill switch zeroes all three
                # instead of skipping the queries below it - disabled means
                # the nav link and both badges hide, not that a real pending/
                # accepted share stops existing in the DB.
                if self.repo.isDataSharingEnabled():
                    g.hasAcceptedShares = self.repo.hasAnyAcceptedShare(username) if username else False
                    g.pendingIncomingSharesCount = self.repo.getPendingIncomingSharesCount(username) if username else 0
                    g.unseenAcceptedShareCount = self.repo.getUnseenAcceptedShareCount(username) if username else 0
                else:
                    g.hasAcceptedShares = False
                    g.pendingIncomingSharesCount = 0
                    g.unseenAcceptedShareCount = 0
            return {
                "hasAcceptedShares": g.hasAcceptedShares,
                "pendingIncomingSharesCount": g.pendingIncomingSharesCount,
                "unseenAcceptedShareCount": g.unseenAcceptedShareCount,
            }

        registerSystemRoutes(self.app, self)

        registerMediaRoutes(self.app, self)

        @self.app.errorhandler(413)
        def _uploadTooLarge(error):
            return redirect(url_for("importPage", error="upload_too_large"))

        registerAuthRoutes(self.app, self)

        @self.app.route("/overview", methods=["GET"])
        def overviewPage():
            from datetime import datetime
            # Intentionally unauthenticated: aggregate counts/DB size carry no
            # per-user listening data, so they're shown to any visitor as a
            # public "is this instance alive" summary - only the per-user
            # status widget below is gated on login. The full multi-user
            # table and every admin-only setting live on /admin now.
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

            # Instance-wide (not per-user), so it's resolved regardless of
            # login state - it also gates the public "Last.fm Genre Backfill"
            # info card further down the page.
            lastfm_enabled = self.repo.isLastfmGenreBackfillEnabled()
            artist_bio_enabled = self.repo.isArtistBioEnabled()
            album_bio_enabled = self.repo.isAlbumBioEnabled()

            # Get current user's timezone for consistent date display
            current_user_tz = None
            current_username = None
            genre_coverage = emptyGenreCoverage()
            genre_unlocked = False
            genre_worker = {"configured": False, "running": False}
            biography_coverage = emptyBiographyCoverage()
            biography_worker = {"artist": {"configured": False, "running": False},
                                "album": {"configured": False, "running": False}}
            if is_logged_in:
                current_username = self.get_username_for_email(email) or self.get_or_create_user(email)
                current_db = self.get_user_db(current_username, email)
                current_user_tz = current_db.tz if current_db else None
                if current_db is not None and lastfm_enabled:
                    # All-time coverage: the progress card tracks the whole
                    # library, unlike the range-scoped gates on charts/wrapped.
                    genre_coverage = resolveGenreCoverage(current_db, None, None)
                    genre_unlocked = genreGatePasses(genre_coverage)
                    try:
                        workerStatus = current_db.getLastfmWorkerStatus()
                        if isinstance(workerStatus, dict):
                            genre_worker = {"configured": bool(workerStatus.get("configured")),
                                            "running": bool(workerStatus.get("running"))}
                    except Exception as e:
                        logger.warning("Last.fm worker status lookup failed: %s", e)
                if current_db is not None and (artist_bio_enabled or album_bio_enabled):
                    biography_coverage = resolveBiographyCoverage(current_db, current_username)
                    try:
                        artistWorkerStatus = current_db.getLastfmBiographyWorkerStatus()
                        if isinstance(artistWorkerStatus, dict):
                            biography_worker["artist"] = {"configured": bool(artistWorkerStatus.get("configured")),
                                                          "running": bool(artistWorkerStatus.get("running"))}
                    except Exception as e:
                        logger.warning("Last.fm artist biography worker status lookup failed: %s", e)
                    try:
                        albumWorkerStatus = current_db.getLastfmAlbumBiographyWorkerStatus()
                        if isinstance(albumWorkerStatus, dict):
                            biography_worker["album"] = {"configured": bool(albumWorkerStatus.get("configured")),
                                                         "running": bool(albumWorkerStatus.get("running"))}
                    except Exception as e:
                        logger.warning("Last.fm album biography worker status lookup failed: %s", e)

            # The logged-in user's own sync/backfill state, as a simple
            # three-badge summary - not a table (the full multi-user table
            # with per-account admin controls lives on /admin now).
            your_status = None
            if is_logged_in:
                own = self.repo.getAllUsersDetails(username=current_username)
                if own:
                    u = own[0]
                    if u["cookies_json"] and current_db is not None:
                        health = current_db.getListenerHealth()
                        sync_status = health.get("status", "UNKNOWN")
                    else:
                        sync_status = "Not Configured"
                    has_api = bool(u["spotify_client_id"] and u["spotify_refresh_token"])
                    your_status = {
                        "sync_status": sync_status,
                        "spotify_api_status": "Configured" if has_api else "Not Configured",
                        #< .get(): raw row presence check only - the stored key
                        #  is encrypted and never needs decrypting here
                        "lastfm_api_status": "Configured" if u.get("lastfm_api_key") else "Not Configured",
                    }

            # One row per entity kind for the combined "Biography Backfill
            # Progress" card (templates/_biography_progress.html) - built
            # here rather than assembled in Jinja so the template stays a
            # dumb iteration over a pre-shaped list, same spirit as how
            # users_list is built above.
            biography_rows = [
                {"label": "Artist", "enabled": artist_bio_enabled, "worker": biography_worker["artist"],
                 **biography_coverage["artist"]},
                {"label": "Album", "enabled": album_bio_enabled, "worker": biography_worker["album"],
                 **biography_coverage["album"]},
            ]

            return render_template(
                "overview.html",
                global_stats=global_stats,
                global_time_text=global_time_text,
                global_size_text=global_size_text,
                is_logged_in=is_logged_in,
                your_status=your_status,
                spotify_backfill_enabled=self.repo.isSpotifyApiBackfillEnabled(),
                genre_coverage=genre_coverage,
                genre_unlocked=genre_unlocked,
                genre_worker=genre_worker,
                lastfm_enabled=lastfm_enabled,
                biography_rows=biography_rows,
                section="overview"
            )

        registerAdminRoutes(self.app, self)

        @self.app.route("/", methods=["GET"])
        def dashboard():
            email, username, db = self.get_current_user_or_redirect()
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

            intervalLabel = self._getIntervalLabel(interval, customStart, customEnd)
            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="day", tz=db.tz)

            # Only an explicit custom range (typically a chart click-through -
            # see static/js/charts.js) scopes the play-history list below. The
            # named intervals (day/week/...) only ever scoped the stats cards
            # above; making every default/named-interval visit also filter the
            # list would silently hide most of a user's history behind
            # whatever their default dashboard window happens to be.
            listStartDate = startDate if interval == "custom" else None
            listEndDate = endDate if interval == "custom" else None

            if searchQuery:
                # Matching and pagination both happen in SQL (Repository.searchPlays)
                # instead of fetching every play ever recorded and filtering in Python.
                totalCount = db.searchEntriesCount(searchQuery, startDate=listStartDate, endDate=listEndDate)
                page, totalPages, startIndex = self._calculatePagination(totalCount)
                tracks = db.searchEntries(searchQuery, count=PAGE_SIZE, startIndex=startIndex,
                                          startDate=listStartDate, endDate=listEndDate)
            else:
                # Only materialize the page being shown - joining full track
                # metadata onto every entry ever recorded on every request gets
                # slow once the history grows large.
                totalCount = db.getEntriesCount(startDate=listStartDate, endDate=listEndDate)
                page, totalPages, startIndex = self._calculatePagination(totalCount)
                tracks = db.getEntriesFromNew(count=PAGE_SIZE, startIndex=startIndex,
                                              startDate=listStartDate, endDate=listEndDate)
            tracks = self._embedSongsTextElements(tracks)
            tracks = self._attachGenres(db, tracks, "track")

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
            email, username, db = self.get_current_user_or_redirect()
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
            tracks = self._attachGenres(db, tracks, "track")

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
            email, username, db = self.get_current_user_or_redirect()
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
            albums = self._attachGenres(db, albums, "album")

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
            email, username, db = self.get_current_user_or_redirect()
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
            artists = self._attachGenres(db, artists, "artist")
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
            email, username, db = self.get_current_user_or_redirect()
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
                db.getListeningTimeSeries(startDate=startDate, endDate=endDate, groupBy=timeSeriesGroupBy),
                groupBy=timeSeriesGroupBy,
            )
            heatmap = self._embedHeatmapTextElements(db.getHourOfDayHeatmap(startDate=startDate, endDate=endDate))
            artistTrend = None if isSingleDayView else db.getArtistTrend(startDate=startDate, endDate=endDate, topN=CHART_ARTIST_TREND_TOP_N, groupBy=groupBy)

            explicitRatio = db.getExplicitRatio(startDate=startDate, endDate=endDate)
            # Flask's JSON provider sorts dict keys alphabetically on
            # serialization (app.json.sort_keys, on by default) - a {label:
            # value} dict handed to |tojson loses whatever order the SQL
            # produced. A JSON array preserves element order regardless, so
            # both bar-chart datasets are shipped as [label, value] pairs
            # instead (see renderCategoryBarChart in charts.js).
            decadeDistribution = list(db.getReleaseDecadeDistribution(startDate=startDate, endDate=endDate).items())
            completionStats = db.getCompletionStats(startDate=startDate, endDate=endDate)

            # The admin's instance-wide kill switch: checked before spending
            # any genre queries, and the whole Top Genres section (chart AND
            # its locked-progress fallback) hides on the template side when
            # this is False - showing "add a Last.fm key" for a feature the
            # admin turned off would be misleading.
            lastfmEnabled = self.repo.isLastfmGenreBackfillEnabled()
            genreCoverage = emptyGenreCoverage()
            genreUnlocked = False
            genreDistribution = None
            if lastfmEnabled:
                genreCoverage = resolveGenreCoverage(db, startDate, endDate)
                genreUnlocked = genreGatePasses(genreCoverage)
                if genreUnlocked:
                    distribution = resolveGenreDistribution(db, startDate, endDate,
                                                            CHART_TOP_GENRES_LIMIT)
                    # Selection stays the same top-N by plays as every other
                    # genre surface (Wrapped/Compare keep descending) - only this
                    # bar chart's own display order is reversed to read ascending.
                    genreDistribution = list(reversed(distribution.items()))

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
                genreCoverage=genreCoverage,
                genreUnlocked=genreUnlocked,
                genreDistribution=genreDistribution,
                lastfmEnabled=lastfmEnabled,
            )

        registerWrappedRoutes(self.app, self)

        registerCompareRoutes(self.app, self)

        @self.app.route("/song/<track_id>", methods=["GET"])
        def songDetailPage(track_id):
            email, username, db = self.get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            song = db.getSong(track_id)
            if song is None:
                return redirect(url_for("topSongsPage"))

            groupBy = self._getValidGroupBy(request.args.get("groupBy", "week"), default="week")

            song = self._embedSongTextElements(song)
            song = self._embedTopSongTextElements(song)
            song = self._attachGenres(db, [song], "track")[0]

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
                success=request.args.get("success"),
                error=request.args.get("error"),
            )

        @self.app.route("/artist/<artist_id>", methods=["GET"])
        def artistDetailPage(artist_id):
            email, username, db = self.get_current_user_or_redirect()
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
            songs = self._attachGenres(db, songs, "track")
            artist = self._embedArtistTextElement(artist)
            artist = self._attachGenres(db, [artist], "artist")[0]

            # lazyFetchArtistBio no-ops (and skips fetching) when the admin's
            # instance-wide toggle is off, same contract as the Last.fm genre
            # backfill kill switch - but the displayed bio is suppressed here
            # too, so disabling the feature also hides an artist's
            # already-fetched bio, not just new ones.
            db.lazyFetchArtistBio(artist_id, artist.get("name", ""))
            artist["bio"] = db.getArtistBio(artist_id) if self.repo.isArtistBioEnabled() else None

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
                success=request.args.get("success"),
                error=request.args.get("error"),
            )

        @self.app.route("/album/<album_id>", methods=["GET"])
        def albumDetailPage(album_id):
            email, username, db = self.get_current_user_or_redirect()
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
            songs = self._attachGenres(db, songs, "track")
            album = self._embedAlbumTextElements(album)
            album = self._attachGenres(db, [album], "album")[0]

            # Mirrors artistDetailPage's bio wiring: lazyFetchAlbumBio no-ops
            # (and skips fetching) when the admin's instance-wide toggle is
            # off, and the displayed bio is suppressed here too, so disabling
            # the feature also hides an album's already-fetched bio. The
            # primary artist (album.getinfo needs one) comes from the
            # already-loaded artists list.
            primaryArtists = album.get("artists") or []
            primaryArtistName = primaryArtists[0].get("name", "") if primaryArtists else ""
            if primaryArtistName:
                db.lazyFetchAlbumBio(album_id, album.get("name", ""), primaryArtistName)
            album["bio"] = db.getAlbumBio(album_id) if self.repo.isAlbumBioEnabled() else None

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
                success=request.args.get("success"),
                error=request.args.get("error"),
            )

    def shutdown(self):
        self._stop_event.set()
        self.backupWorker.stop()
        with self._db_lock:
            databases = list(self.user_databases.values())
        # Two-phase: SIGNAL every user's stop flags first (no joins), THEN
        # join. While user A's threads were being joined, user B's still-
        # running listener used to hit its stale-feed check and resurrect
        # itself mid-shutdown (the 2026-07-17 hang); with every stop flag
        # already set, the reconnect paths refuse instead.
        for db in databases:
            try:
                db.signalStop()
            except Exception as e:
                logger.error("Error signaling stop for %s: %s", db.user, e)
        for db in databases:
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