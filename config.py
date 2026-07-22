"""Instance-wide display and behavior constants.

Extracted from app.py so app.py, the routes/ modules, and the dashboard/ helper
mixins can all pull the same values from one place. app.py re-exports these
(`from config import *`), so `from app import <CONST>` and routes' `appmod.<CONST>`
keep working unchanged. This module imports nothing from the app, so the mixins
can `from config import ...` (including in default arguments) without a cycle.
"""

PAGE_SIZE = 50                  #< list items shown per page
LOGIN_CACHE_TTL_SECONDS = 180  #< seconds to cache isListenerLoggedIn result per user
MEDIA_FOLDER_SIZE_CACHE_TTL_SECONDS = 300  #< seconds to cache the shared media cache folder's on-disk size (getGlobalDatabaseStats) - recomputing it walks/subprocess-scans the whole directory
CHART_ARTIST_TREND_TOP_N = 5   #< how many top artists are plotted on the trend line chart
CHART_TOP_GENRES_LIMIT = 10    #< bars on the Charts page's Top Genres chart
WRAPPED_TOP_GENRES_LIMIT = 5   #< genres listed on the Wrapped genre card
COMPARE_TOP_GENRES_LIMIT = 10  #< per-side genres (and shared genres) shown on Compare
COMPARE_GENRE_POOL_SIZE = 50   #< per-side genre pool the shared-genre intersection is computed over
TRACK_CARD_GENRE_LIMIT = 3     #< genre pills shown per track/artist/album card, position-ordered
ON_THIS_DAY_YEARS_LIMIT = 5    #< max prior years surfaced in the dashboard "On this day" card
LISTEN_TIME_HIDE_SECONDS_ABOVE_HOURS = 10   #< dashboard "Total listen time" drops the seconds component once the total reaches this many hours
RECOMMENDATION_ARTIST_LIMIT = 6    #< artists shown in the dashboard "Discover" recommendations card
RECOMMENDATION_GENRE_POOL = 15     #< how many of the user's top genres candidate artists are matched against
RECOMMENDATION_EXCLUDE_TOP_N = 25  #< user's most-played artists excluded from recommendations (already well-known to them)
GENRE_PAGE_LIST_LIMIT = 12         #< genres shown in the Genres page distribution bars / share donut / chip list
GENRE_MIX_TREND_TOP_N = 6          #< genres plotted on the Genres page "mix over time" multi-line chart (kept small so it stays legible)
GENRE_PAGE_TOP_ARTISTS_LIMIT = 10  #< top artists listed for the selected genre
GENRE_PAGE_TOP_TRACKS_LIMIT = 10   #< top tracks listed for the selected genre
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
# registerRoutes' after_request hook in app.py).
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
