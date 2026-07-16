"""Last.fm Web API client for the genre backfill.

Only the unauthenticated getTopTags family is used - a per-user API key is
all Last.fm requires for public catalog data (no OAuth/session, unlike the
Spotify integration). Responses are classified into a small outcome taxonomy
the worker acts on:

- OK          definitive: a (possibly empty) tag list - filter and store it
- NOT_FOUND   definitive: Last.fm doesn't know the entity (error 6)
- TRANSIENT   retryable: network trouble, 5xx, malformed JSON, rate limiting
- INVALID_KEY the stored key is broken/suspended - the worker idles instead
              of hammering 4 failing requests per second forever

Requests across ALL users' workers flow through one process-wide
LastfmRateLimiter: Last.fm's ceiling is 5 requests/second and error 29 is
enforced per IP, which every worker on this server shares.

The genre whitelist (lastfm_genres.txt, one genre per line, # comments) is
the MusicBrainz genre list - Last.fm tags are free-form ("seen live",
"favorites", decades, moods), and only tags matching the whitelist survive
filterTagsToGenres. Matching happens on a normalized form (lowercase,
hyphens as spaces), so "Hip-Hop" and "hip hop" merge.
"""
import logging
import threading
import time
from collections import namedtuple
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

LASTFM_API_ROOT = "https://ws.audioscrobbler.com/2.0/"

# Process-wide request budget, with headroom under Last.fm's 5 req/s ceiling.
LASTFM_REQUESTS_PER_SECOND = 4

# Cool-down once Last.fm says we're rate limited (error 29 / HTTP 429) -
# mirrors the Spotify listener's RATE_LIMIT_ERROR_BACKOFF_SECONDS.
LASTFM_RATE_LIMIT_BACKOFF_SECONDS = 60

LASTFM_HTTP_TIMEOUT_SECONDS = 10

# Last.fm API error codes (https://www.last.fm/api/errorcodes).
LASTFM_ERROR_NOT_FOUND = 6         #< "invalid parameters" - unknown artist/album/track
LASTFM_ERROR_INVALID_API_KEY = 10
LASTFM_ERROR_KEY_SUSPENDED = 26
LASTFM_ERROR_RATE_LIMITED = 29

# Tag filtering: whitelisted genres ranked by tag count, top N kept.
GENRE_MAX_TAGS_PER_ENTITY = 5
GENRE_WHITELIST_PATH = Path(__file__).resolve().parent / "lastfm_genres.txt"

# Key validation happens synchronously on a profile-save request thread: it
# shares the worker rate limiter but must never hang the HTTP request when
# workers have the budget saturated - give up after a short wait instead.
LASTFM_VALIDATION_ACQUIRE_TIMEOUT_SECONDS = 5
LASTFM_VALIDATION_ARTIST = "Cher"   #< Last.fm's own docs example; any stable artist works

OUTCOME_OK = "ok"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_TRANSIENT = "transient"
OUTCOME_INVALID_KEY = "invalid_key"

FetchOutcome = namedtuple("FetchOutcome", ["status", "tags"])


class LastfmRateLimiter:
    """Thread-safe slot spacer on the monotonic clock: acquire() blocks until
    the next request slot is free (returning False if the stop event fires or
    the timeout expires first), applyBackoff() pushes every future slot past a
    penalty window. One shared instance paces the whole process."""

    def __init__(self, requestsPerSecond: float):
        self._interval = 1.0 / requestsPerSecond
        self._lock = threading.Lock()
        self._nextSlotAt = 0.0
        self._backoffUntil = 0.0

    def acquire(self, stop_event: threading.Event | None = None,
                timeout: float | None = None) -> bool:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            with self._lock:
                now = time.monotonic()
                readyAt = max(self._nextSlotAt, self._backoffUntil)
                if readyAt <= now:
                    self._nextSlotAt = now + self._interval
                    return True
                waitFor = readyAt - now
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                waitFor = min(waitFor, remaining)
            if stop_event is not None:
                if stop_event.wait(waitFor):
                    return False
            else:
                time.sleep(waitFor)

    def applyBackoff(self, seconds: float) -> None:
        with self._lock:
            # max() so overlapping penalties from concurrent workers never
            # shrink an already-running window.
            self._backoffUntil = max(self._backoffUntil, time.monotonic() + seconds)


# Shared by every per-user worker and the profile page's key validation.
RATE_LIMITER = LastfmRateLimiter(LASTFM_REQUESTS_PER_SECOND)


def normalizeGenreTag(raw: str) -> str:
    return " ".join(raw.strip().lower().replace("-", " ").split())


_whitelistCache: dict[str, str] | None = None
_whitelistLock = threading.Lock()


def loadGenreWhitelist() -> dict[str, str]:
    """normalized form -> canonical genre name, lazily read once from
    lastfm_genres.txt (UTF-8 - genre names carry accents)."""
    global _whitelistCache
    if _whitelistCache is None:
        with _whitelistLock:
            if _whitelistCache is None:
                entries: dict[str, str] = {}
                for line in GENRE_WHITELIST_PATH.read_text(encoding="utf-8").splitlines():
                    name = line.strip()
                    if not name or name.startswith("#"):
                        continue
                    entries.setdefault(normalizeGenreTag(name), name)
                _whitelistCache = entries
    return _whitelistCache


def filterTagsToGenres(tags: list) -> list[str]:
    """The whitelisted genres in a Last.fm tag list, ranked by tag count
    (alphabetical on the constant ties Last.fm's 0-100 normalized counts
    produce), deduped after normalization keeping each genre's best count,
    capped at GENRE_MAX_TAGS_PER_ENTITY. Malformed entries are skipped."""
    whitelist = loadGenreWhitelist()
    bestCounts: dict[str, int] = {}
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        name = tag.get("name")
        if not isinstance(name, str):
            continue
        canonical = whitelist.get(normalizeGenreTag(name))
        if canonical is None:
            continue
        try:
            count = int(tag.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if canonical not in bestCounts or count > bestCounts[canonical]:
            bestCounts[canonical] = count
    ranked = sorted(bestCounts.items(), key=lambda item: (-item[1], item[0]))
    return [genre for genre, _ in ranked[:GENRE_MAX_TAGS_PER_ENTITY]]


def _extractTags(payload) -> list:
    """toptags.tag from a Last.fm payload. A single tag arrives as a bare dict
    instead of a 1-element list - normalize both shapes; anything else reads
    as "no tags"."""
    toptags = payload.get("toptags") if isinstance(payload, dict) else None
    if not isinstance(toptags, dict):
        return []
    tags = toptags.get("tag")
    if isinstance(tags, dict):
        return [tags]
    if isinstance(tags, list):
        return tags
    return []


class LastfmClient:
    def __init__(self, apiKey: str, rateLimiter: LastfmRateLimiter | None = None):
        self.apiKey = apiKey
        self.rateLimiter = rateLimiter if rateLimiter is not None else RATE_LIMITER

    def getArtistTopTags(self, artistName: str,
                         stop_event: threading.Event | None = None) -> FetchOutcome | None:
        return self._fetchTopTags("artist.gettoptags", {"artist": artistName}, stop_event)

    def getAlbumTopTags(self, artistName: str, albumName: str,
                        stop_event: threading.Event | None = None) -> FetchOutcome | None:
        return self._fetchTopTags("album.gettoptags",
                                  {"artist": artistName, "album": albumName}, stop_event)

    def getTrackTopTags(self, artistName: str, trackName: str,
                        stop_event: threading.Event | None = None) -> FetchOutcome | None:
        return self._fetchTopTags("track.gettoptags",
                                  {"artist": artistName, "track": trackName}, stop_event)

    def validateApiKey(self) -> dict:
        """One cheap lookup to vet a key before storing it. {"ok": bool,
        "error": None|"invalid_key"|"unreachable"|"busy"} - "busy" means the
        shared limiter had no slot within the validation timeout."""
        outcome = self._fetchTopTags("artist.gettoptags",
                                     {"artist": LASTFM_VALIDATION_ARTIST},
                                     stop_event=None,
                                     timeout=LASTFM_VALIDATION_ACQUIRE_TIMEOUT_SECONDS)
        if outcome is None:
            return {"ok": False, "error": "busy"}
        if outcome.status == OUTCOME_INVALID_KEY:
            return {"ok": False, "error": "invalid_key"}
        if outcome.status == OUTCOME_TRANSIENT:
            return {"ok": False, "error": "unreachable"}
        return {"ok": True, "error": None}

    def _fetchTopTags(self, method: str, params: dict,
                      stop_event: threading.Event | None,
                      timeout: float | None = None) -> FetchOutcome | None:
        """None only when the rate-limit slot was never granted (stopping
        worker / validation timeout) - no request went out."""
        if not self.rateLimiter.acquire(stop_event=stop_event, timeout=timeout):
            return None
        query = {"method": method, "api_key": self.apiKey, "format": "json",
                 "autocorrect": "1", **params}
        try:
            response = requests.get(LASTFM_API_ROOT, params=query,
                                    timeout=LASTFM_HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as e:
            logger.warning("[Lastfm] %s request failed: %s", method, e)
            return FetchOutcome(OUTCOME_TRANSIENT, [])
        return self._classifyResponse(method, response)

    def _classifyResponse(self, method: str, response) -> FetchOutcome:
        if response.status_code == 429:
            self.rateLimiter.applyBackoff(LASTFM_RATE_LIMIT_BACKOFF_SECONDS)
            logger.warning("[Lastfm] HTTP 429 on %s - backing off %ds", method,
                           LASTFM_RATE_LIMIT_BACKOFF_SECONDS)
            return FetchOutcome(OUTCOME_TRANSIENT, [])

        try:
            payload = response.json()
        except ValueError:
            logger.warning("[Lastfm] Unparseable response on %s (status %d)",
                           method, response.status_code)
            return FetchOutcome(OUTCOME_TRANSIENT, [])

        # Last.fm reports errors in the body (sometimes with HTTP 200) - the
        # error code, not the HTTP status, is authoritative.
        if isinstance(payload, dict) and "error" in payload:
            code = payload.get("error")
            if code == LASTFM_ERROR_RATE_LIMITED:
                self.rateLimiter.applyBackoff(LASTFM_RATE_LIMIT_BACKOFF_SECONDS)
                logger.warning("[Lastfm] Rate limited (error 29) on %s - backing off %ds",
                               method, LASTFM_RATE_LIMIT_BACKOFF_SECONDS)
                return FetchOutcome(OUTCOME_TRANSIENT, [])
            if code in (LASTFM_ERROR_INVALID_API_KEY, LASTFM_ERROR_KEY_SUSPENDED):
                return FetchOutcome(OUTCOME_INVALID_KEY, [])
            if code == LASTFM_ERROR_NOT_FOUND:
                return FetchOutcome(OUTCOME_NOT_FOUND, [])
            logger.warning("[Lastfm] Error %s on %s: %s", code, method,
                           payload.get("message", ""))
            return FetchOutcome(OUTCOME_TRANSIENT, [])

        if response.status_code != 200:
            return FetchOutcome(OUTCOME_TRANSIENT, [])

        return FetchOutcome(OUTCOME_OK, _extractTags(payload))
