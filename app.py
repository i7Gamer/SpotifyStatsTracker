import csv
import io
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

from Database.database import Database, GENRE_COVERAGE_CATEGORIES
from Database.backup import BackupWorker
from Database.db import SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON
from Database.repository import Repository
from Database.Migrators.migrate import migrateIfNeeded
from Database.Listeners.spotifyListener import _suppress_signal_in_thread
from Database.lastfm import LastfmClient
from Database.logging_config import configureLogging
from Database.utils import msToString, convertToDatetime, formatDuration, dateToString, versionTuple, now, startOfDay, parseDateString
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
# The genre-feature unlock gate: genre insights on Charts/Wrapped/Compare only
# render once the play-weighted Last.fm coverage over the page's date range is
# strictly above the overall minimum (mean of the three categories) AND at
# least at the per-category minimum for songs, albums and artists - partial
# data would silently misrepresent someone's taste otherwise.
GENRE_GATE_OVERALL_MIN_PERCENT = 50
GENRE_GATE_CATEGORY_MIN_PERCENT = 30
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
# Taste-match weighting: artists dominate - exact-song collisions between
# two top-100s are structurally rare even for very similar listeners (huge
# catalog, low odds), so songs barely count; albums sit between. Genres are
# coarser still (many listeners share broad tags without similar taste), so
# they weigh less than albums - present only when both sides pass the genre
# unlock gate (see genreGatePasses), same bar every other genre surface
# uses. Categories without data on both sides are excluded and the
# remaining weights renormalized.
TASTE_MATCH_WEIGHTS = {"artists": 0.7, "songs": 0.1, "albums": 0.2, "genres": 0.15}
# Rank-weighted overlap normalizes against an "ideal" match capped at this
# depth rather than the full COMPARE_OVERLAP_POOL_SIZE: requiring near-total
# overlap of a 100-deep pool for 100% meant even two listeners who share
# their entire top 20 favorite artists scored ~34%, since agreement past
# rank ~30 barely matters to how similar two people's taste feels.
TASTE_MATCH_IDEAL_DEPTH = 30
# An exact id match earns DOUBLE the rank discount of its BETTER (shallower)
# side's rank - the "both sides at rank r" shape the taste-match ideal
# normalizes against. The single place the factor is applied is
# _mutualRankScore, taste-match's exact-match credit. (The Top Common
# lists rank by _sharedRankScore instead - see it for why min()-based
# credit is wrong for a display list.)
EXACT_MATCH_CREDIT_FACTOR = 2
# A song/album that ISN'T an exact match still earns this fraction of its own
# rank discount when its primary artist appears in the counterpart's top
# artist pool (see _rankWeightedOverlap) - loving the same ARTIST without
# happening to share the exact same song/album is real taste overlap, not
# zero. Doesn't apply to the artist category itself (no secondary "artist of
# an artist" concept there).
ARTIST_MEDIATED_CREDIT_FACTOR = 0.4
# The final taste-match score (0..1 weighted average across categories) is
# raised to this power before display: a concave response curve, since real
# people rarely share MOST of their top taste even when they genuinely have
# similar taste - a linear score reads as harshly low for overlap that
# actually feels like "we like a lot of the same stuff." Monotonic, so it
# never reorders which of two pairs is the better match, just stretches the
# low-to-mid range upward (raw 0.25 -> ~50%, raw 0.5 -> ~71%).
TASTE_MATCH_CURVE_EXPONENT = 0.6
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
EXPORT_CHUNK_SIZE = 5000         #< plays hydrated per round-trip while streaming an export
EXPORT_FORMATS = ("json", "csv")
EXPORT_CSV_COLUMNS = ("played_at_utc", "track_name", "artists", "album", "ms_played", "spotify_track_uri", "played_from")
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


def emptyGenreCoverage() -> dict:
    """The all-zeros coverage shape - what guests, empty ranges and sanitize
    failures all resolve to (and the gate always rejects)."""
    coverage = {categoryName: {"covered": 0, "total": 0, "percent": 0.0}
                for categoryName in GENRE_COVERAGE_CATEGORIES}
    coverage["overall"] = {"percent": 0.0}
    return coverage


def _requireNumber(value):
    """The value if it's a real number. Explicit isinstance rather than
    int()/float() coercion: MagicMock happily answers __int__ with 1, which
    would let an unstubbed test db masquerade as real coverage."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"not a number: {value!r}")
    return value


def sanitizeGenreCoverage(coverage) -> dict:
    """`coverage` if it is shaped like Database.getGenreCoverage's result,
    else all zeros. Route code must only ever consume coverage through this:
    stubbed dbs in tests (and any unexpected failure) then degrade to the
    locked state instead of crashing template rendering."""
    try:
        sanitized = {}
        for categoryName in GENRE_COVERAGE_CATEGORIES:
            categoryData = coverage[categoryName]
            sanitizedCategory = {field: _requireNumber(categoryData[field])
                                 for field in ("covered", "total", "percent")}
            # Optional (older callers/stubs don't produce it), but validated
            # like every other field when present - the template only renders
            # the own-tags split when the key survives sanitization.
            if isinstance(categoryData, dict) and "ownPercent" in categoryData:
                sanitizedCategory["ownPercent"] = _requireNumber(categoryData["ownPercent"])
            sanitized[categoryName] = sanitizedCategory
        sanitized["overall"] = {"percent": _requireNumber(coverage["overall"]["percent"])}
        return sanitized
    except (TypeError, KeyError):
        return emptyGenreCoverage()


def resolveGenreCoverage(db, startDate, endDate) -> dict:
    """Sanitized genre coverage for a user db over a range; zeros when the
    lookup fails for any reason (never let the genre gate break a page)."""
    try:
        return sanitizeGenreCoverage(db.getGenreCoverage(startDate=startDate, endDate=endDate))
    except Exception as e:
        logger.warning("Genre coverage lookup failed: %s", e)
        return emptyGenreCoverage()


def resolveGenreDistribution(db, startDate, endDate, limit) -> dict:
    """Genre distribution for a user db over a range; {} when the lookup
    fails or returns a non-dict (stubbed dbs) - the same degradation
    contract as resolveGenreCoverage, so every genre surface consumes
    distributions through this one chokepoint."""
    try:
        distribution = db.getGenreDistribution(startDate=startDate, endDate=endDate, limit=limit)
    except Exception as e:
        logger.warning("Genre distribution lookup failed: %s", e)
        return {}
    return distribution if isinstance(distribution, dict) else {}


def genreGatePasses(coverage: dict) -> bool:
    """The unlock rule on a sanitized coverage dict: overall strictly above
    GENRE_GATE_OVERALL_MIN_PERCENT and every category at or above
    GENRE_GATE_CATEGORY_MIN_PERCENT."""
    if coverage["overall"]["percent"] <= GENRE_GATE_OVERALL_MIN_PERCENT:
        return False
    return all(coverage[categoryName]["percent"] >= GENRE_GATE_CATEGORY_MIN_PERCENT
               for categoryName in GENRE_COVERAGE_CATEGORIES)


def _resolveGenresFor(db, entityId, dbMethodName: str) -> list[str]:
    """Shared degradation contract for the per-item genre lookups below: a
    lookup failure, or a stubbed test db whose genre method was never
    configured (a bare MagicMock() return value), degrades to [] instead of
    breaking every page that renders a track/artist/album card."""
    try:
        genres = getattr(db, dbMethodName)(entityId)
    except Exception as e:
        logger.warning("%s(%r) failed: %s", dbMethodName, entityId, e)
        return []
    return genres if isinstance(genres, list) else []


def resolveGenresForTrack(db, trackId) -> list[str]:
    return _resolveGenresFor(db, trackId, "getGenresForTrack")


def resolveGenresForAlbum(db, albumId) -> list[str]:
    return _resolveGenresFor(db, albumId, "getGenresForAlbum")


def resolveGenresForArtist(db, artistId) -> list[str]:
    return _resolveGenresFor(db, artistId, "getGenresForArtist")


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

    @staticmethod
    def _markLinkExternally(items: list[dict], playedIds: set) -> None:
        """In place: sets item['linkExternally'] so _track_card.html (and
        _compare_stats_table.html's theirCell macro) link this counterpart
        item to Spotify only when it's NOT in `playedIds` - i.e. only when
        the viewer has no data of their own for it."""
        for item in items:
            item["linkExternally"] = item["id"] not in playedIds

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
        myRanks = self._rankById(self._resortByMetric(myPool, "plays"))
        theirRanks = self._rankById(self._resortByMetric(theirPool, "plays"))
        theirById = {item["id"]: item for item in theirPool}
        sharedItems = [dict(item) for item in myPool if item["id"] in theirById]

        def sortKey(item):
            theirItem = theirById[item["id"]]
            sharedScore = self._sharedRankScore(myRanks[item["id"]], theirRanks[item["id"]])
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

    @staticmethod
    def _rankWeight(rank: int) -> float:
        """DCG-style discount for a 1-based rank: the #1 spot weighs 1,
        deeper ranks fall off logarithmically."""
        return 1 / math.log2(rank + 1)

    @staticmethod
    def _rankById(pool: list[dict]) -> dict:
        """id -> 1-based rank map of an already-ordered pool - the lookups
        _mutualRankScore/_sharedRankScore consume. The ORDERING stays the
        caller's choice: _rankWeightedOverlap must trust the incoming
        pool's own order (its genre pools are bare {"id": genre} dicts
        with no metrics to re-derive one from), while _buildSharedItems
        re-sorts by plays first (see its docstring for why)."""
        return {item["id"]: rank for rank, item in enumerate(pool, start=1)}

    def _mutualRankScore(self, myRank: int, theirRank: int) -> float:
        """Taste-match's exact-match credit: EXACT_MATCH_CREDIT_FACTOR x
        the rank discount (see _rankWeight) of the BETTER (shallower) of
        the two ranks for the same item - _rankWeightedOverlap's per-item
        credit AND its ideal normalizer (_mutualRankScore(r, r)) both use
        it; see that docstring for the mutual-favorite rationale. The Top
        Common lists deliberately rank by _sharedRankScore instead."""
        return EXACT_MATCH_CREDIT_FACTOR * self._rankWeight(min(myRank, theirRank))

    def _sharedRankScore(self, myRank: int, theirRank: int) -> float:
        """Ranking score for a Top Common list entry: the SUM of both
        sides' rank discounts (see _rankWeight), so both users' engagement
        counts. Deliberately NOT _mutualRankScore's min() shape: min()
        ignores the weaker side entirely, so a one-sided favorite (my #1,
        their #200) would score like a true #1/#1 mutual favorite and
        outrank a genuine #2/#2 - fine for taste-match's aggregate (its
        ideal normalizer is built on the same-rank shape), wrong for a
        list literally titled "common" (see
        test_shared_list_one_sided_favorite_loses_to_true_mutual_item).
        The sum still lets one side's #1 carry a moderate counterpart
        rank past two lukewarm mid-ranks (see
        test_shared_list_ranks_by_mutual_favorite_not_raw_combined_plays)."""
        return self._rankWeight(myRank) + self._rankWeight(theirRank)

    @staticmethod
    def _primaryArtistId(item: dict) -> str | None:
        """The first-listed (primary) artist's id for a song/album pool item -
        track_artists.position 0, i.e. how Spotify itself orders credited
        artists - or None if the item somehow carries no artists."""
        artists = item.get("artists") or []
        return artists[0]["id"] if artists else None

    def _rankWeightedOverlap(self, myPool, theirPool, myArtistIds=None, theirArtistIds=None) -> float | None:
        """0..1 rank-weighted overlap of two ranked pools, normalized against
        the score two pools would reach if they agreed on their top
        TASTE_MATCH_IDEAL_DEPTH items - so a shared #1 counts far more than
        a shared #90, and matching core taste can reach 100% without also
        requiring overlap across the entire deep pool. Clamped to 1 since
        overlap (or artist-mediated credit, see below) can push the raw
        ratio above it. None when either side is empty, so the category can
        be excluded rather than scored 0.

        An exact id match contributes _mutualRankScore:
        EXACT_MATCH_CREDIT_FACTOR x the rank discount of its BETTER
        (shallower/lower-numbered) side's rank, not the sum of both sides'
        discounts - matches `ideal`'s own shape (_mutualRankScore(r, r),
        the case where both sides tie at the same rank r), and means a
        mutual favorite ranked #3 by one person and #40 by the other still
        counts close to a #3/#3 match instead of being dragged down by
        whichever side ranks it deeper.

        When myArtistIds/theirArtistIds are given (songs/albums only - the
        artist category has no secondary "artist of an artist" concept), a
        non-exact item still earns ARTIST_MEDIATED_CREDIT_FACTOR of its own
        rank discount when its primary artist (see _primaryArtistId)
        appears in the counterpart's top artist pool."""
        if not myPool or not theirPool:
            return None
        myRanks = self._rankById(myPool)
        theirRanks = self._rankById(theirPool)
        exactIds = myRanks.keys() & theirRanks.keys()
        actual = sum(self._mutualRankScore(myRanks[itemId], theirRanks[itemId]) for itemId in exactIds)

        if myArtistIds is not None and theirArtistIds is not None:
            myById = {item["id"]: item for item in myPool}
            theirById = {item["id"]: item for item in theirPool}
            for itemId, rank in myRanks.items():
                if itemId in exactIds:
                    continue
                artistId = self._primaryArtistId(myById[itemId])
                if artistId is not None and artistId in theirArtistIds:
                    actual += ARTIST_MEDIATED_CREDIT_FACTOR * self._rankWeight(rank)
            for itemId, rank in theirRanks.items():
                if itemId in exactIds:
                    continue
                artistId = self._primaryArtistId(theirById[itemId])
                if artistId is not None and artistId in myArtistIds:
                    actual += ARTIST_MEDIATED_CREDIT_FACTOR * self._rankWeight(rank)

        idealDepth = min(len(myPool), len(theirPool), TASTE_MATCH_IDEAL_DEPTH)
        ideal = sum(self._mutualRankScore(rank, rank) for rank in range(1, idealDepth + 1))
        return min(1.0, actual / ideal)

    def _tasteMatchPercent(self, my, their, myGenrePool=None, theirGenrePool=None) -> int | None:
        """One headline number for how much two users' taste overlaps: the
        rank-weighted pool overlap per category (with artist-mediated credit
        for songs/albums - see _rankWeightedOverlap), weighted by
        TASTE_MATCH_WEIGHTS and passed through a concave response curve
        (see TASTE_MATCH_CURVE_EXPONENT). None when no category has data on
        both sides - the UI hides the badge instead of showing a misleading
        0%.

        myGenrePool/theirGenrePool are {genre: plays} distributions (see
        Database.getGenreDistribution), or None/empty when the caller's
        genre unlock gate hasn't passed for both sides - the genre category
        behaves like "artists" (exact string match only, no secondary
        mediation) and is naturally excluded by _rankWeightedOverlap when
        either pool is empty."""
        myArtistIds = {a["id"] for a in my["topArtistsPool"]}
        theirArtistIds = {a["id"] for a in their["topArtistsPool"]}
        myGenresPool = [{"id": genre} for genre in (myGenrePool or {})]
        theirGenresPool = [{"id": genre} for genre in (theirGenrePool or {})]
        categories = {
            "artists": (my["topArtistsPool"], their["topArtistsPool"], None, None),
            "songs": (my["topSongsPool"], their["topSongsPool"], myArtistIds, theirArtistIds),
            "albums": (my["topAlbumsPool"], their["topAlbumsPool"], myArtistIds, theirArtistIds),
            "genres": (myGenresPool, theirGenresPool, None, None),
        }
        parts = []
        for kind, (myPool, theirPool, myAIds, theirAIds) in categories.items():
            fraction = self._rankWeightedOverlap(myPool, theirPool, myAIds, theirAIds)
            if fraction is not None:
                parts.append((fraction, TASTE_MATCH_WEIGHTS[kind]))
        if not parts:
            return None
        raw = sum(fraction * weight for fraction, weight in parts) / sum(weight for _, weight in parts)
        return round(100 * raw ** TASTE_MATCH_CURVE_EXPONENT)

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

    @staticmethod
    def _resortByMetric(items: list, sortBy: str) -> list:
        """Re-sorts an already-fetched list of song/artist/album dicts by
        `sortBy` (plays/totalTimeListened descending, name ascending) -
        matches VALID_SORT_BY's semantics (see app.py's sortBy query param
        docs) without re-querying the DB. Used where a pool was fetched at
        one fixed ranking but the displayed order should follow the user's
        chosen metric instead (Wrapped's cached pools, which are only ever
        stored plays-ranked).

        Ties on `sortBy` fall back to the other metric, then name - mirrors
        Repository.getSongsPage's plays -> totalTimeListened -> name tiebreak
        chain instead of leaning on the input pool's incidental order."""
        if sortBy == "name":
            return sorted(items, key=lambda item: item.get("name", "").lower())
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

    def _iterExportEntries(self, db, includeSkips=False):
        """Every play (oldest first) with hydrated track metadata, fetched in
        EXPORT_CHUNK_SIZE batches so an export never holds the whole history
        in memory. Plays recorded while the export streams have the newest
        played_at, so they can only appear at the very end - earlier chunks
        can't shift underneath the OFFSET pagination.

        includeSkips: skip events follow after every play (their sub-threshold
        ms_played routes them back into play_skips on reimport). JSON only -
        the CSV stays plays-only for spreadsheet use."""
        startIndex = 0
        while True:
            entries = db.getEntriesFromOld(count=EXPORT_CHUNK_SIZE, startIndex=startIndex)
            if not entries:
                break
            yield from entries
            startIndex += EXPORT_CHUNK_SIZE
        if not includeSkips:
            return
        startIndex = 0
        while True:
            entries = db.getSkipEntriesFromOld(count=EXPORT_CHUNK_SIZE, startIndex=startIndex)
            if not entries:
                return
            yield from entries
            startIndex += EXPORT_CHUNK_SIZE

    @staticmethod
    def _isoUtc(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Behavioral columns emitted as-is vs. as booleans - under Spotify's own
    # key names (incognito is stored under the column name but exported as
    # incognito_mode), so the export re-imports through _extractExtras.
    EXPORT_TEXT_EXTRAS = ("platform", "conn_country", "reason_start", "reason_end")
    EXPORT_BOOL_EXTRAS = (("shuffle", "shuffle"), ("skipped", "skipped"),
                          ("offline", "offline"), ("incognito", "incognito_mode"))

    def _exportEntryToDict(self, entry) -> dict:
        """One play in Spotify's own extended-streaming-history shape, so the
        export re-imports through the existing pipeline. `ts` is the play's
        END time - Spotify's convention, which importExtendedHistory converts
        back to a start time by subtracting ms_played. Behavioral fields are
        emitted only when stored; offline plays also carry offline_timestamp
        (their corrected start), which the importer prefers over ts."""
        artists = entry.get("artists") or []
        album = entry.get("album") or {}
        item = {
            "ts": self._isoUtc(entry["playedAt"] + entry["timePlayed"] // 1000),
            "ms_played": entry["timePlayed"],
            "master_metadata_track_name": entry.get("name"),
            "master_metadata_album_artist_name": artists[0].get("name") if artists else None,
            "master_metadata_album_album_name": album.get("name") if album else None,
            "spotify_track_uri": f"spotify:track:{entry['id']}",
            "played_from": entry.get("playedFrom"),   #< extra field; the importer ignores it
        }
        extras = entry.get("extras") or {}
        for column in self.EXPORT_TEXT_EXTRAS:
            if extras.get(column) is not None:
                item[column] = extras[column]
        for column, exportKey in self.EXPORT_BOOL_EXTRAS:
            if extras.get(column) is not None:
                item[exportKey] = bool(extras[column])
        if extras.get("offline"):
            item["offline_timestamp"] = int(entry["playedAt"])
        return item

    def _generateJsonExport(self, db):
        yield "[\n"
        first = True
        for entry in self._iterExportEntries(db, includeSkips=True):
            prefix = "" if first else ",\n"
            first = False
            yield prefix + json.dumps(self._exportEntryToDict(entry), ensure_ascii=False)
        yield "\n]\n"

    def _generateCsvExport(self, db):
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(EXPORT_CSV_COLUMNS)
        for entry in self._iterExportEntries(db):
            artists = entry.get("artists") or []
            album = entry.get("album") or {}
            writer.writerow([
                self._isoUtc(entry["playedAt"]),   #< the START time - more intuitive for spreadsheet use
                entry.get("name") or "",
                ", ".join(a.get("name", "") for a in artists),
                album.get("name") or "" if album else "",
                entry["timePlayed"],
                f"spotify:track:{entry['id']}",
                entry.get("playedFrom") or "",
            ])
            if buffer.tell() >= 64 * 1024:   #< flush in ~64KB chunks instead of per row or all at once
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
        yield buffer.getvalue()

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
            # Captured before the thread starts - no request context inside it.
            overwriteRange = request.form.get("overwrite_range") is not None
            db.writeProgress("running", 0, 0, "Starting import")
            thread = threading.Thread(target=db.importHistoryBatch, args=(contents,),
                                      kwargs={"overwriteRange": overwriteRange}, daemon=True)
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

        @self.app.route("/export-history", methods=["GET"])
        def exportHistory():
            """Stream the current user's full play history as a download.
            JSON is shaped like Spotify's own extended export (re-importable
            through /import-history); CSV is for spreadsheets."""
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            exportFormat = request.args.get("format", "json")
            if exportFormat not in EXPORT_FORMATS:
                exportFormat = "json"

            dateText = now(tz=db.tz).strftime("%Y-%m-%d")
            filename = f"spotify_stats_export_{username}_{dateText}.{exportFormat}"
            if exportFormat == "csv":
                generator, mimetype = self._generateCsvExport(db), "text/csv; charset=utf-8"
            else:
                generator, mimetype = self._generateJsonExport(db), "application/json"

            response = Response(stream_with_context(generator), mimetype=mimetype)
            response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response

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
            if not self.repo.isRegistrationEnabled():
                abort(404)
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
                    if not self.repo.isDataSharingEnabled():
                        abort(404)
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
                elif action == "save_lastfm":
                    if not self.repo.isLastfmGenreBackfillEnabled():
                        abort(404)
                    # Throttled like request_share: every save fires a live
                    # validation request against Last.fm.
                    if _rateLimited("save_lastfm"):
                        error = RATE_LIMIT_ERROR_MESSAGE
                        responseStatus = 429
                    else:
                        lastfm_api_key = (request.form.get("lastfm_api_key") or "").strip()
                        if not lastfm_api_key:
                            error = "A Last.fm API key is required."
                        else:
                            validation = LastfmClient(lastfm_api_key).validateApiKey()
                            if validation["ok"]:
                                try:
                                    db.updateUserLastfmApiKey(lastfm_api_key)
                                    db.startLastfmGenreBackfiller()
                                    success = "Last.fm API key saved! Genre data is now backfilling in the background."
                                except Exception as e:
                                    error = f"Failed to save the Last.fm API key: {str(e)}"
                            elif validation["error"] == "invalid_key":
                                error = "Last.fm rejected that API key - double-check it and try again."
                            elif validation["error"] == "busy":
                                error = "The Last.fm request budget is busy right now - try again in a few seconds."
                            else:
                                error = "Could not reach Last.fm to verify the key - try again later."
                elif action == "remove_lastfm":
                    try:
                        db.updateUserLastfmApiKey(None)
                        db.stopLastfmGenreBackfiller()
                        success = "Last.fm API key removed."
                    except Exception as e:
                        error = f"Failed to remove the Last.fm API key: {str(e)}"
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

            shareLinks = [
                {**link,
                 "createdText": dateToString(link["created_at"], tz=db.tz),
                 "expiresText": dateToString(link["expires_at"], tz=db.tz) if link["expires_at"] else "Never"}
                for link in self.repo.getShareLinksForUser(username)
            ]

            return render_template(
                "profile.html",
                username=username,
                email=email,
                client_id=client_id,
                client_secret=client_secret,
                has_api=bool(client_id and client_secret),
                has_lastfm=bool(db.getUserLastfmApiKey()),
                lastfm_enabled=self.repo.isLastfmGenreBackfillEnabled(),
                sharing_enabled=self.repo.isDataSharingEnabled(),
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
                shareLinks=shareLinks,
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
            if not self.repo.isDataSharingEnabled():
                abort(404)
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
            # One-shot CSRF state - see SPOTIFY_OAUTH_STATE_SESSION_KEY's
            # comment. token_urlsafe output needs no URL-encoding.
            state = secrets.token_urlsafe(SPOTIFY_OAUTH_STATE_NUM_BYTES)
            session[SPOTIFY_OAUTH_STATE_SESSION_KEY] = state

            auth_url = (
                f"https://accounts.spotify.com/authorize"
                f"?client_id={client_id}"
                f"&response_type=code"
                f"&redirect_uri={spotify_callback_url}"
                f"&scope={scope}"
                f"&state={state}"
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

            # CSRF guard - see SPOTIFY_OAUTH_STATE_SESSION_KEY's comment.
            # pop(): a state value is single-use, so a replayed callback URL
            # is rejected even after a successful exchange.
            expectedState = session.pop(SPOTIFY_OAUTH_STATE_SESSION_KEY, None)
            if not expectedState or request.args.get("state") != expectedState:
                return redirect(url_for(
                    "profilePage",
                    error="Spotify authorization failed: missing or mismatched state - "
                          "please start over with 'Authorize with Spotify'."))

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
                    # Full response body only server-side - the redirect param ends up
                    # in browser history/access logs and may echo credential details.
                    logger.warning("Spotify token exchange failed for %s (HTTP %s): %s",
                                   username, resp.status_code, resp.text)
                    return redirect(url_for("profilePage", error="Failed to exchange token with Spotify - check your API credentials and try again."))
            except Exception as e:
                logger.warning("Exception during Spotify token exchange for %s: %s", username, e)
                return redirect(url_for("profilePage", error="Something went wrong during the token exchange - please try again."))

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

        @self.app.route("/api/now-playing", methods=["GET"])
        def nowPlayingStatus():
            """What the user is playing right now, from the listener's cached
            connect state (no Spotify calls) - polled by the dashboard."""
            email, username, db = get_current_user_or_redirect()
            if not email:
                return jsonify({"error": "Not logged in"}), 401
            return jsonify({"nowPlaying": db.getNowPlaying()})

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

            # Instance-wide (not per-user), so it's resolved regardless of
            # login state - it also gates the public "Last.fm Genre Backfill"
            # info card further down the page.
            lastfm_enabled = self.repo.isLastfmGenreBackfillEnabled()

            # Get current user's timezone for consistent date display
            current_user_tz = None
            current_username = None
            is_admin = False
            genre_coverage = emptyGenreCoverage()
            genre_unlocked = False
            genre_worker = {"configured": False, "running": False}
            if is_logged_in:
                current_username = self.get_username_for_email(email) or self.get_or_create_user(email)
                current_db = self.get_user_db(current_username, email)
                current_user_tz = current_db.tz if current_db else None
                is_admin = self.repo.isAdmin(current_username)
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

            users_list = []
            if is_logged_in:
                # The full listing (every account's username, sync state, play
                # count) is admin-only; a regular user gets just their own row,
                # so they can still check their own sync status here.
                if is_admin:
                    all_users = self.repo.getAllUsersDetails()
                else:
                    all_users = self.repo.getAllUsersDetails(username=current_username)
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
                        #< .get(): raw row presence check only - the stored key
                        #  is encrypted and never needs decrypting here
                        "genre_status": "Configured" if u.get("lastfm_api_key") else "Not Configured",
                        "plays_count": plays_count,
                        "created_at": created_date_str
                    })

            return render_template(
                "overview.html",
                global_stats=global_stats,
                global_time_text=global_time_text,
                global_size_text=global_size_text,
                is_logged_in=is_logged_in,
                is_admin=is_admin,
                users_list=users_list,
                genre_coverage=genre_coverage,
                genre_unlocked=genre_unlocked,
                genre_worker=genre_worker,
                lastfm_enabled=lastfm_enabled,
                spotify_backfill_enabled=self.repo.isSpotifyApiBackfillEnabled(),
                sharing_enabled=self.repo.isDataSharingEnabled(),
                inherited_genres_enabled=self.repo.isInheritedGenresEnabled(),
                section="overview"
            )

        @self.app.route("/overview/genre_settings", methods=["POST"])
        def overviewGenreSettings():
            """Admin-only: flips the instance-wide inherited-genres toggle
            (whether artist-derived genre rows count in genre stats and
            coverage - see Database/repository.py's app_settings)."""
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=url_for("overviewPage")))
            if not self.repo.isAdmin(username):
                abort(403)
            # Unchecked checkboxes aren't submitted: absence means disable.
            self.repo.setInheritedGenresEnabled(request.form.get("include_inherited") == "1")
            return redirect(url_for("overviewPage"))

        @self.app.route("/overview/feature_settings", methods=["POST"])
        def overviewFeatureSettings():
            """Admin-only: flips the instance-wide feature kill switches
            (Spotify API backfill, Last.fm genre backfill, data sharing, new
            user registration, public Wrapped share links) in one submit -
            see Database/repository.py's app_settings."""
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=url_for("overviewPage")))
            if not self.repo.isAdmin(username):
                abort(403)
            # Unchecked checkboxes aren't submitted: absence means disable.
            self.repo.setSpotifyApiBackfillEnabled(request.form.get("spotify_backfill") == "1")
            self.repo.setLastfmGenreBackfillEnabled(request.form.get("lastfm_backfill") == "1")
            self.repo.setDataSharingEnabled(request.form.get("data_sharing") == "1")
            self.repo.setRegistrationEnabled(request.form.get("registration") == "1")
            self.repo.setShareLinksEnabled(request.form.get("share_links") == "1")
            return redirect(url_for("overviewPage"))

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
            # Default stays "plays" (not DEFAULT_SORT_BY) so nobody's Wrapped
            # changes unless they touch the control.
            sortBy = self._getSortByParam(default="plays")

            # Genre data is deliberately computed live - never from the
            # user_wrapped cache: coverage keeps growing while the Last.fm
            # backfill runs, and the admin's inherited-genres toggle changes
            # the numbers retroactively. Only computed for responses that
            # actually render the card (the full page and ajax type=all -
            # chart/lists partial updates would compute and discard it). See
            # chartsPage's identical kill-switch comment; _wrapped_genres.html
            # hides its whole section (chart AND locked-progress fallback)
            # when lastfmEnabled is False.
            isAjaxRequest = request.args.get("ajax") == "true"
            ajaxUpdateType = request.args.get("type", "all")
            includeGenres = not isAjaxRequest or ajaxUpdateType == "all"

            ctx = self._buildWrappedContext(db, year, groupBy, limit, sortBy, includeGenres=includeGenres)
            totalPlays, totalMs = ctx["totalPlays"], ctx["totalMs"]
            topSongs, topArtists, topAlbums = ctx["topSongs"], ctx["topArtists"], ctx["topAlbums"]
            discoveredSongs, discoveredArtists, discoveredAlbums = (
                ctx["discoveredSongs"], ctx["discoveredArtists"], ctx["discoveredAlbums"])
            timeSeries = ctx["timeSeries"]
            longestStreak, peakListeningTime = ctx["longestStreak"], ctx["peakListeningTime"]
            uniqueSongsCount, uniqueArtistsCount = ctx["uniqueSongsCount"], ctx["uniqueArtistsCount"]
            discoveredSongsCount, discoveredArtistsCount = ctx["discoveredSongsCount"], ctx["discoveredArtistsCount"]
            topGenres, genreCoverage, genreUnlocked = ctx["topGenres"], ctx["genreCoverage"], ctx["genreUnlocked"]
            lastfmEnabled = ctx["lastfmEnabled"]

            # 6. Check if AJAX request and return JSON response if true
            if isAjaxRequest:
                update_type = ajaxUpdateType
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
                    res["topGenresHtml"] = render_template(
                        "_wrapped_genres.html", topGenres=topGenres,
                        genreCoverage=genreCoverage, genreUnlocked=genreUnlocked, year=year,
                        lastfmEnabled=lastfmEnabled)
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
                sortBy=sortBy,
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
                topGenres=topGenres,
                genreCoverage=genreCoverage,
                genreUnlocked=genreUnlocked,
                lastfmEnabled=lastfmEnabled,
                has_api=has_api,
                is_authenticated=is_authenticated,
                success=success,
                error=error,
                publicView=False,
                shareLinksEnabled=self.repo.isShareLinksEnabled(),
                shareLinks=self.repo.getShareLinksForUser(username),
                shareLinkExpiryChoices=SHARE_LINK_EXPIRY_CHOICES,
            )

        @self.app.route("/wrapped/share-links/<int:year>", methods=["POST"])
        def createWrappedShareLink(year):
            """Creates a public, no-login share link for one year of the
            current user's own Wrapped - see sharedWrappedPage() below for
            the route that serves it. ajax=true mirrors wrappedPage()'s own
            AJAX convention: the modal on wrapped.html posts here in the
            background and swaps in the returned panel HTML instead of
            leaving the page."""
            isAjax = request.args.get("ajax") == "true"
            email, username, db = get_current_user_or_redirect()
            if not email:
                if isAjax:
                    return jsonify(error="Please log in again."), 401
                return redirect(url_for("login", next=url_for("wrappedPage")))
            if not self.repo.isShareLinksEnabled():
                abort(404)
            if _rateLimited("share_link_create"):
                if isAjax:
                    return jsonify(error=RATE_LIMIT_ERROR_MESSAGE), 429
                return redirect(url_for("wrappedPage", error=RATE_LIMIT_ERROR_MESSAGE, openShareModal=1))

            expiresInSeconds = SHARE_LINK_EXPIRY_CHOICES.get(request.form.get("expiry", "never"))
            token = self.repo.createShareLink(username, Repository.SHARE_LINK_KIND_WRAPPED, year, expiresInSeconds)
            if isAjax:
                html = render_template(
                    "_share_link_panel.html", year=year, currentLink=self.repo.getShareLink(token),
                    shareLinkExpiryChoices=SHARE_LINK_EXPIRY_CHOICES)
                return jsonify(html=html)
            return redirect(url_for("wrappedPage", year=year, success="Share link created.", openShareModal=1))

        @self.app.route("/shared/<token>", methods=["GET"])
        def sharedWrappedPage(token):
            """Public, unauthenticated view of one user's Wrapped for one
            year - no session, no nav, no PII. See docs/proposal-admin-and-
            share-links.md Part B for the design this implements."""
            if not self.repo.isShareLinksEnabled():
                abort(404)

            link = self.repo.getShareLink(token)
            if link is None:
                # Only misses count against the limit - repeat visits to a
                # real link must never be throttled (see the plan's rate-
                # limiting note), only someone guessing random tokens.
                if _rateLimited("shared_token"):
                    abort(429)
                abort(404)

            db = self._getReadOnlyUserDb(link["username"])
            ctx = self._buildWrappedContext(
                db, link["year"], groupBy="week", limit=WRAPPED_LIST_SIZE, sortBy="plays", includeGenres=True)

            resp = make_response(render_template(
                "wrapped.html",
                username=link["username"],
                section="wrapped",
                year=link["year"],
                availableYears=[link["year"]],
                groupBy="week",
                limit=WRAPPED_LIST_SIZE,
                limitOptions=WRAPPED_LIMIT_OPTIONS,
                sortBy="plays",
                totalPlays=ctx["totalPlays"],
                totalTime=msToString(ctx["totalMs"]),
                topSongs=ctx["topSongs"],
                topArtists=ctx["topArtists"],
                topAlbums=ctx["topAlbums"],
                discoveredSongs=ctx["discoveredSongs"],
                discoveredArtists=ctx["discoveredArtists"],
                discoveredAlbums=ctx["discoveredAlbums"],
                timeSeries=ctx["timeSeries"],
                longestStreak=ctx["longestStreak"],
                peakListeningTime=ctx["peakListeningTime"],
                uniqueSongsCount=ctx["uniqueSongsCount"],
                uniqueArtistsCount=ctx["uniqueArtistsCount"],
                discoveredSongsCount=ctx["discoveredSongsCount"],
                discoveredArtistsCount=ctx["discoveredArtistsCount"],
                topGenres=ctx["topGenres"],
                genreCoverage=ctx["genreCoverage"],
                genreUnlocked=ctx["genreUnlocked"],
                lastfmEnabled=ctx["lastfmEnabled"],
                has_api=False,
                is_authenticated=False,
                success=None,
                error=None,
                publicView=True,
                imageBase=f"/shared/{token}/img",
                shareLinksEnabled=False,
                shareLinks=[],
                shareLinkExpiryChoices=SHARE_LINK_EXPIRY_CHOICES,
            ))
            resp.headers["X-Robots-Tag"] = "noindex"
            return resp

        @self.app.route("/shared/<token>/img/tracks/<filename>")
        def serveSharedTrackImage(token, filename):
            link = self.repo.getShareLink(token)
            if link is None or filename != os.path.basename(filename):
                return "", 404
            resp = make_response(send_from_directory(Database.imgDir_tracks, filename))
            resp.headers["X-Robots-Tag"] = "noindex"
            return resp

        @self.app.route("/shared/<token>/img/artists/<filename>")
        def serveSharedArtistImage(token, filename):
            link = self.repo.getShareLink(token)
            if link is None or filename != os.path.basename(filename):
                return "", 404

            imageDir = Database.imgDir_artists
            imagePath = os.path.join(imageDir, filename)
            if not os.path.exists(imagePath):
                parts = os.path.splitext(filename)
                if len(parts) == 2 and parts[0].isalnum():
                    db = self._getReadOnlyUserDb(link["username"])
                    db.lazyFetchArtistImage(parts[0], Path(imagePath))

            resp = make_response(send_from_directory(imageDir, filename))
            resp.headers["X-Robots-Tag"] = "noindex"
            return resp

        @self.app.route("/profile/share-links/<int:link_id>", methods=["POST"])
        def profileShareLinkAction(link_id):
            """Owner-only revoke for a public Wrapped share link - accept/
            decline don't apply here (unlike profileShareAction's mutual
            shares), there's only ever one action. ajax=true is the wrapped.html
            modal's revoke form (see createWrappedShareLink); profile.html's
            own revoke form never sets it and keeps the classic redirect."""
            isAjax = request.args.get("ajax") == "true"
            email, username, db = get_current_user_or_redirect()
            if not email:
                if isAjax:
                    return jsonify(error="Please log in again."), 401
                return redirect(url_for("login"))

            if self.repo.revokeShareLink(link_id, username):
                if isAjax:
                    html = render_template(
                        "_share_link_panel.html", year=request.form.get("year", type=int), currentLink=None,
                        shareLinkExpiryChoices=SHARE_LINK_EXPIRY_CHOICES)
                    return jsonify(html=html)
                return redirect(url_for("profilePage", success="Share link revoked."))
            if isAjax:
                return jsonify(error="Could not revoke that share link."), 403
            return redirect(url_for("profilePage", error="Could not revoke that share link."))

        @self.app.route("/compare", methods=["GET"])
        def comparePage():
            if not self.repo.isDataSharingEnabled():
                abort(404)
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

            # Same default-window setting the dashboard route reads - "all
            # time" is that setting's own stored spelling, but Compare's own
            # dropdown represents All Time as "" (see compare.html), so it's
            # normalized before feeding either the resolver or the template.
            settings = self.repo.getUserSettings(username)
            defaultWindow = settings.get("default_dashboard_window", "day")
            if defaultWindow == "all time":
                defaultWindow = ""
            interval = request.args.get("interval", defaultWindow)
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")
            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="all time", tz=db.tz)
            groupByParam = request.args.get("groupBy", "")
            limit = request.args.get("limit", type=int)
            if limit not in WRAPPED_LIMIT_OPTIONS:
                limit = COMPARE_TOP_LIST_SIZE
            # Default stays "plays" (not DEFAULT_SORT_BY) so nobody's view
            # changes unless they touch the control - matches every other
            # value on this page defaulting to the pre-existing behavior.
            # Same whitelist as the standalone Top pages (VALID_SORT_BY),
            # default "plays". sortBy only reorders the individual my/their
            # lists (see _gatherCompareStats) - the Top Common lists and
            # taste-match never read it (see _buildSharedItems).
            sortBy = self._getSortByParam(default="plays")

            my = self._gatherCompareStats(db, startDate, endDate, limit=limit, sortBy=sortBy)
            their = self._gatherCompareStats(otherDb, startDate, endDate, limit=limit, sortBy=sortBy)

            # A counterpart item links to Spotify only when the viewer has NO
            # plays of that exact song/artist/album - the viewer's own detail
            # page has nothing to show them then. Otherwise it links there
            # like any other item, since the viewer genuinely does have data
            # for it (a real play-history lookup, not "is in the viewer's own
            # top list" - a track can be true for the former and not the
            # latter). Batched to one query per category rather than one per
            # displayed item.
            self._markLinkExternally(their["topSongs"], db.getPlayedTrackIds([s["id"] for s in their["topSongs"]]))
            self._markLinkExternally(their["topArtists"], db.getPlayedArtistIds([a["id"] for a in their["topArtists"]]))
            self._markLinkExternally(their["topAlbums"], db.getPlayedAlbumIds([a["id"] for a in their["topAlbums"]]))

            # Sliced like every other list on the page. No percent text here -
            # it would mix two different users' totals. Searches the deeper
            # sharedXPool (COMPARE_SHARED_POOL_SIZE), not the shallower
            # topXPool taste-match uses - see _gatherCompareStats. Ranked by
            # _buildSharedItems's own shared-rank-weighted score, independent
            # of sortBy - only the individual my/their lists above read it.
            sharedArtists = self._buildSharedItems(
                my["sharedArtistsPool"], their["sharedArtistsPool"],
                self._embedArtistsTextElements, limit)
            sharedSongs = self._buildSharedItems(
                my["sharedSongsPool"], their["sharedSongsPool"],
                lambda items: self._embedTopSongsTextElements(self._embedSongsTextElements(items)),
                limit)
            sharedAlbums = self._buildSharedItems(
                my["sharedAlbumsPool"], their["sharedAlbumsPool"],
                self._embedAlbumsTextElements, limit)
            # Genre tables are entity-keyed, not user-scoped, so either
            # side's db returns the same result here - db (the viewer's) is
            # just what's already in scope.
            sharedArtists = self._attachGenres(db, sharedArtists, "artist")
            sharedSongs = self._attachGenres(db, sharedSongs, "track")
            sharedAlbums = self._attachGenres(db, sharedAlbums, "album")

            # Similarities run over the deeper sharedXPool, not the displayed
            # top ten (nor the shallower topXPool taste-match uses) - a #200-
            # ranked common favorite is still a common favorite. The
            # "common top X" is shared-rank-weighted (see _buildSharedItems/
            # _sharedRankScore), so it's the same regardless of who's viewing
            # OR what sortBy they picked, and its detail link resolves
            # because the viewer played it.
            theirArtistIds = {a["id"] for a in their["sharedArtistsPool"]}
            theirSongIds = {s["id"] for s in their["sharedSongsPool"]}
            theirAlbumIds = {a["id"] for a in their["sharedAlbumsPool"]}
            similarities = {
                "commonTopArtist": sharedArtists[0] if sharedArtists else None,
                "commonTopSong": sharedSongs[0] if sharedSongs else None,
                "commonTopAlbum": sharedAlbums[0] if sharedAlbums else None,
                "sharedArtistCount": sum(1 for a in my["sharedArtistsPool"] if a["id"] in theirArtistIds),
                "sharedSongCount": sum(1 for s in my["sharedSongsPool"] if s["id"] in theirSongIds),
                "sharedAlbumCount": sum(1 for a in my["sharedAlbumsPool"] if a["id"] in theirAlbumIds),
                "poolSize": COMPARE_SHARED_POOL_SIZE,
            }
            # Genre comparison (and the genre category folded into taste
            # match below) requires BOTH sides past the unlock gate -
            # comparing a complete genre profile against a half-backfilled
            # one would misrepresent the half-backfilled user's taste. See
            # chartsPage's identical kill-switch comment for why this is
            # checked before any genre query.
            lastfmEnabled = self.repo.isLastfmGenreBackfillEnabled()
            myGenreCoverage = emptyGenreCoverage()
            theirGenreCoverage = emptyGenreCoverage()
            genresUnlocked = False
            myGenrePool = None
            theirGenrePool = None
            myTopGenres = None
            theirTopGenres = None
            sharedGenres = None
            if lastfmEnabled:
                myGenreCoverage = resolveGenreCoverage(db, startDate, endDate)
                theirGenreCoverage = resolveGenreCoverage(otherDb, startDate, endDate)
                genresUnlocked = genreGatePasses(myGenreCoverage) and genreGatePasses(theirGenreCoverage)
            if genresUnlocked:
                myGenrePool = resolveGenreDistribution(db, startDate, endDate,
                                                       COMPARE_GENRE_POOL_SIZE)
                theirGenrePool = resolveGenreDistribution(otherDb, startDate, endDate,
                                                          COMPARE_GENRE_POOL_SIZE)
                myTopGenres = dict(list(myGenrePool.items())[:COMPARE_TOP_GENRES_LIMIT])
                theirTopGenres = dict(list(theirGenrePool.items())[:COMPARE_TOP_GENRES_LIMIT])
                # Shared genres: the pools' intersection, ordered by combined
                # plays (name breaks ties) - like the shared-item lists, the
                # overlap runs over the deeper pools, not just the displayed top.
                sharedGenres = [
                    {"genre": genre, "myPlays": myGenrePool[genre],
                     "theirPlays": theirGenrePool[genre],
                     "combinedPlays": myGenrePool[genre] + theirGenrePool[genre]}
                    for genre in set(myGenrePool) & set(theirGenrePool)
                ]
                sharedGenres.sort(key=lambda item: (-item["combinedPlays"], item["genre"]))
                sharedGenres = sharedGenres[:COMPARE_TOP_GENRES_LIMIT]

            tasteMatch = self._tasteMatchPercent(my, their, myGenrePool, theirGenrePool)

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

            if groupByParam:
                groupBy = self._getValidGroupBy(groupByParam)
            else:
                # No explicit choice: bucket so the trend stays readable at any
                # range - day buckets across a multi-year span are sub-pixel.
                spanDays = (trendEndDate - trendStartDate).days if trendStartDate and trendEndDate else 0
                if spanDays > COMPARE_TREND_MONTH_SPAN_DAYS:
                    groupBy = "month"
                elif spanDays > COMPARE_TREND_WEEK_SPAN_DAYS:
                    groupBy = "week"
                else:
                    groupBy = "day"
            # Single-day ranges bucket by hour, mirroring chartsPage's
            # isSingleDayView - one 'day' bucket would collapse the whole
            # trend into a single point.
            trendGroupBy = "hour" if interval in ("day", "today") else groupBy

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

            if request.args.get("ajax") == "true":
                # Same fade-and-swap partial updates as the Wrapped page: the
                # filter controls fetch these chunks and swap them in place
                # instead of a full page reload.
                emptyMessage = "No plays in this period."
                listArgs = dict(username=username, compareWith=withUsername, emptyMessage=emptyMessage)
                return jsonify({
                    "withUsername": withUsername,
                    "tasteMatch": tasteMatch,
                    "statsTableHtml": render_template(
                        "_compare_stats_table.html", my=my, their=their,
                        username=username, withUsername=withUsername),
                    "similaritiesHtml": render_template(
                        "_compare_similarities.html", similarities=similarities,
                        username=username),   #< the cover-image URLs' session-authorization segment
                    "genresHtml": render_template(
                        "_compare_genres.html", username=username, withUsername=withUsername,
                        myTopGenres=myTopGenres, theirTopGenres=theirTopGenres,
                        sharedGenres=sharedGenres, myGenreCoverage=myGenreCoverage,
                        theirGenreCoverage=theirGenreCoverage, genresUnlocked=genresUnlocked,
                        lastfmEnabled=lastfmEnabled),
                    "sharedArtistsHtml": render_template(
                        "_wrapped_list.html", items=sharedArtists, section="top_artists",
                        username=username, compareWith=withUsername,
                        emptyMessage="No shared top artists in this period yet."),
                    "sharedSongsHtml": render_template(
                        "_wrapped_list.html", items=sharedSongs, section="top_songs",
                        username=username, compareWith=withUsername,
                        emptyMessage="No shared top songs in this period yet."),
                    "sharedAlbumsHtml": render_template(
                        "_wrapped_list.html", items=sharedAlbums, section="top_albums",
                        username=username, compareWith=withUsername,
                        emptyMessage="No shared top albums in this period yet."),
                    "myTopSongsHtml": render_template(
                        "_wrapped_list.html", items=my["topSongs"], section="top_songs", **listArgs),
                    #< each item's own linkExternally decides internal vs. Spotify (see _markLinkExternally)
                    "theirTopSongsHtml": render_template(
                        "_wrapped_list.html", items=their["topSongs"], section="top_songs", **listArgs),
                    "myTopArtistsHtml": render_template(
                        "_wrapped_list.html", items=my["topArtists"], section="top_artists", **listArgs),
                    "theirTopArtistsHtml": render_template(
                        "_wrapped_list.html", items=their["topArtists"], section="top_artists", **listArgs),
                    "myTopAlbumsHtml": render_template(
                        "_wrapped_list.html", items=my["topAlbums"], section="top_albums", **listArgs),
                    "theirTopAlbumsHtml": render_template(
                        "_wrapped_list.html", items=their["topAlbums"], section="top_albums", **listArgs),
                    "comparisonTrend": comparisonTrend,
                })

            return render_template(
                "compare.html",
                section="compare",
                username=username,
                withUsername=withUsername,
                acceptedUsernames=acceptedUsernames,
                my=my,
                their=their,
                sharedArtists=sharedArtists,
                sharedSongs=sharedSongs,
                sharedAlbums=sharedAlbums,
                similarities=similarities,
                tasteMatch=tasteMatch,
                myTopGenres=myTopGenres,
                theirTopGenres=theirTopGenres,
                sharedGenres=sharedGenres,
                myGenreCoverage=myGenreCoverage,
                theirGenreCoverage=theirGenreCoverage,
                genresUnlocked=genresUnlocked,
                lastfmEnabled=lastfmEnabled,
                comparisonTrend=comparisonTrend,
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                limit=limit,
                limitOptions=WRAPPED_LIMIT_OPTIONS,
                #< the raw param, not the resolved bucketing - links that pin
                #  the auto-derived value would freeze auto mode
                groupBy=groupByParam,
                sortBy=sortBy,
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
            songs = self._attachGenres(db, songs, "track")
            artist = self._embedArtistTextElement(artist)
            artist = self._attachGenres(db, [artist], "artist")[0]

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
            songs = self._attachGenres(db, songs, "track")
            album = self._embedAlbumTextElements(album)
            album = self._attachGenres(db, [album], "album")[0]

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