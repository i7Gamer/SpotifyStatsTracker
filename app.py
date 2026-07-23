import logging
import math
import os
import json
import random
import secrets
import sys
import tempfile
import threading
import requests
from pathlib import Path
import time
from datetime import timedelta, datetime, timezone

# When this file is run directly (py app.py), Python registers it in
# sys.modules as "__main__", not "app". The routes/* modules below do
# `import app as appmod` to reach PAGE_SIZE etc.; without this line that
# import can't find "app" in sys.modules and re-executes this file from
# scratch, re-entering the routes.charts import mid-flight before its
# register() is defined - a circular ImportError. No-op on a normal
# `import app`, since sys.modules["app"] is already this module by then.
sys.modules.setdefault("app", sys.modules[__name__])

from flask import Flask, render_template, redirect, request, url_for, jsonify, send_from_directory, session, g, abort, Response, stream_with_context, make_response
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

from Database.database import Database
from Database.backup import (
    BackupWorker, _envInt, BACKUP_INTERVAL_ENV_VAR, BACKUP_RETENTION_ENV_VAR,
    DEFAULT_BACKUP_INTERVAL_HOURS, DEFAULT_BACKUP_RETENTION_COUNT,
)
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
    resolveGenreTrends, resolveGenreStats, resolveTopArtistsForGenre,
    resolveTopTracksForGenre, resolveGenreHeatmap, emptyHeatmapGrid,
    resolveGenreArtistCounts,
)
# Taste-match scoring lives in services/taste_match.py; the compare route calls
# _tasteMatchPercent/_markLinkExternally and _buildSharedItems calls
# _rankById/_sharedRankScore.
from services.taste_match import (
    _tasteMatchPercent, _markLinkExternally, _rankById, _sharedRankScore,
)
from services.milestones import detectMilestones
from routes.media import register as registerMediaRoutes
from routes.admin import register as registerAdminRoutes
from routes.charts import register as registerChartsRoutes
from routes.genres import register as registerGenresRoutes
from routes.compare import register as registerCompareRoutes
from routes.wrapped import register as registerWrappedRoutes
from routes.auth import register as registerAuthRoutes
from routes.system import register as registerSystemRoutes
import SpotipyFree
from SpotipyFree import saveSession, parseCookieString

logger = logging.getLogger(__name__)

# Instance-wide display/behavior constants live in config.py; imported * here so
# app.py, its routes (via `import app as appmod`) and the test suite
# (`from app import <CONST>`) all keep reaching them through `app`.
from config import *  # noqa: E402,F401,F403


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


def _hstsEnabled() -> bool:
    """Whether to send a Strict-Transport-Security header, per the ENABLE_HSTS
    env var. Off by default: the app is normally self-hosted over plain HTTP
    (see SECURITY_HEADERS in config.py), where pinning HTTPS for the origin
    would break access. Enable it only when a TLS-terminating reverse proxy is
    in front. Read live per response so a flip doesn't need a restart."""
    return os.environ.get(ENABLE_HSTS_ENV_VAR, "").strip().lower() in TRUTHY_ENV_VALUES


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


from dashboard.view_models import ViewModelMixin
from dashboard.pagination import PaginationMixin
from dashboard.date_ranges import DateRangeMixin
from dashboard.wrapped_builder import WrappedBuilderMixin
from dashboard.compare_stats import CompareStatsMixin


class SpotifyDashboardApp(ViewModelMixin, PaginationMixin, DateRangeMixin, WrappedBuilderMixin, CompareStatsMixin):
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
        # Size the shared background thread pools from admin settings, once,
        # before any Database instance (and its workers) is constructed. A
        # changed value applies only after a restart - see configureWorkerPools.
        Database.configureWorkerPools(self.repo)
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
        # Per-user {username: (totalPlays, totalMs)} from the last milestone
        # pass, so an idle background cycle skips the heavy streak/top-artist
        # queries when a user's totals haven't moved (see detectMilestones).
        # In-process is sufficient: detection only runs from the single
        # _checkLoginLoop thread, and this app is single-process by design.
        self._milestoneChangeCache: dict = {}
        # Snapshots the shared database on a schedule (see Database/backup.py) -
        # a manual backup command in the README protects nobody who doesn't run it.
        # Interval/retention come from admin settings, falling back to the env
        # vars then the code defaults; read once here, so changes apply on restart.
        self.backupWorker = BackupWorker(
            intervalHours=self.repo.getBackupIntervalHours(
                _envInt(BACKUP_INTERVAL_ENV_VAR, DEFAULT_BACKUP_INTERVAL_HOURS)),
            retentionCount=self.repo.getBackupRetentionCount(
                _envInt(BACKUP_RETENTION_ENV_VAR, DEFAULT_BACKUP_RETENTION_COUNT)),
        )
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

    def _detectMilestonesSafely(self, db, username):
        """Run one user's milestone-detection pass from the periodic background
        loop (_ensureAllUsersLogin), never the request path - so a page render
        never pays for the aggregate queries or the DB write, and the badge
        just reads an already-computed count. Failures are logged and
        swallowed so one user's bad pass can't stall the loop. Only users with
        stored cookies are covered (that's who the loop iterates); an
        import-only account with no live session first gets its milestones on
        its next cookie login."""
        try:
            detectMilestones(db, db.repo, username, changeCache=self._milestoneChangeCache)
        except Exception as e:
            logger.warning("Milestone detection failed for %s: %s", username, e)

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
                # Folded into this existing per-user pass rather than a loop of
                # its own. On a cycle where the user's play totals haven't moved
                # it costs just the one getPlayTotals scan (the change signal) -
                # the heavier streak/top-artist queries are skipped via
                # _milestoneChangeCache; see detectMilestones.
                self._detectMilestonesSafely(db, username)
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

    _GENRE_RESOLVERS = {
        "track": resolveGenresForTrack,
        "album": resolveGenresForAlbum,
        "artist": resolveGenresForArtist,
    }

    def registerRoutes(self):
        @self.app.after_request
        def _setSecurityHeaders(response):
            for header, value in SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
            if _hstsEnabled():
                response.headers.setdefault("Strict-Transport-Security", HSTS_HEADER_VALUE)
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
        def _injectLastfmGenreStatus():
            # Lets layout.html's nav show the "Genres" link only when the
            # admin's instance-wide Last.fm genre backfill is enabled - the
            # same kill switch the Charts genre section already respects, so
            # the nav never advertises a page whose entire content is off.
            # Cheap settings read, instance-wide (no per-user memoization).
            return {"lastfm_genre_enabled": self.repo.isLastfmGenreBackfillEnabled()}

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

        @self.app.context_processor
        def _injectSpotifyReauthStatus():
            # Topbar badge for "Web API backfill is stuck because the stored
            # Spotify authorization is missing a scope" (see
            # Listener.on_scope_status_change) - otherwise the only place
            # this ever surfaces is the Connection Status card on /profile,
            # which nothing prompts a user to go check. Gated on
            # SPOTIFY_CALLBACK_URL like every other Spotify Developer API
            # route/link: with it unset, /spotify-authorize 404s, so a badge
            # pointing there would be a dead end. Memoized on g for the same
            # reason as _injectShareStatus above.
            if "spotifyNeedsReauthBadge" not in g:
                username = session.get("username")
                g.spotifyNeedsReauthBadge = (
                    bool(os.environ.get("SPOTIFY_CALLBACK_URL"))
                    and bool(username)
                    and self.repo.getSpotifyNeedsReauth(username)
                )
            return {"spotifyNeedsReauthBadge": g.spotifyNeedsReauthBadge}

        @self.app.context_processor
        def _injectMilestoneStatus():
            # Topbar badge for unacknowledged achievement milestones (new
            # play/listen-time/streak thresholds or a new #1 artist), cleared
            # when the user opens the Milestones section on /profile
            # (markMilestonesSeen). Memoized on g like _injectShareStatus: one
            # request can render several templates and each re-runs every
            # context processor, so this cheap indexed count must not repeat per
            # partial. No is_user_logged_in check, for the same reason
            # _injectShareStatus skips it - the worst case is a badge that 302s
            # to login like every other nav item.
            if "unseenMilestoneCount" not in g:
                username = session.get("username")
                g.unseenMilestoneCount = self.repo.getUnseenMilestoneCount(username) if username else 0
            return {"unseenMilestoneCount": g.unseenMilestoneCount}

        registerSystemRoutes(self.app, self)

        registerMediaRoutes(self.app, self)

        @self.app.errorhandler(413)
        def _uploadTooLarge(error):
            return redirect(url_for("importPage", error="upload_too_large"))

        registerAuthRoutes(self.app, self)

        registerChartsRoutes(self.app, self)

        registerGenresRoutes(self.app, self)

        registerAdminRoutes(self.app, self)

        registerWrappedRoutes(self.app, self)

        registerCompareRoutes(self.app, self)

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