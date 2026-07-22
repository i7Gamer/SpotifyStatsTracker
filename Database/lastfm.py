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
import html
import logging
import re
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

# Curated aliases for hugely common Last.fm tags that miss the MusicBrainz
# whitelist but have one unambiguous genre reading. Keys are pre-normalized
# (normalizeGenreTag form); the map is only consulted on a whitelist miss, so
# a source that is also a whitelisted genre would silently never alias - a
# test enforces sources stay out of the whitelist and targets resolve in it.
GENRE_TAG_ALIASES = {
    "rap": "hip hop",
    "alternative": "alternative rock",
    "alt rock": "alternative rock",
    "indie": "indie rock",
    "rnb": "r&b",
    "r and b": "r&b",
    "r n b": "r&b",
    "synthpop": "synth-pop",
    "lofi": "lo-fi",
    "kpop": "k-pop",
    "jpop": "j-pop",
    "cpop": "c-pop",
    "dnb": "drum and bass",
    "alt pop": "alternative pop",
    "ost": "soundtrack",
    "game score": "soundtrack",
    "movie soundtrack": "soundtrack",
}




def normalizeArtistLookupName(name: str) -> list[str]:
    r"""Returns candidate transformed artist names to retry when verbatim lookup
    yields no tags/bio on Last.fm. Handles slash separators (/\ or \/ -> &),
    plus signs (+ -> and), and multi-artist credit joiners. Returns an empty
    list if no transformations apply."""
    candidates: list[str] = []

    if "/\\" in name or "\\/" in name:
        slash_fixed = name.replace("/\\", "&").replace("\\/", "&")
        candidates.append(slash_fixed)

    if " + " in name:
        plus_fixed = name.replace(" + ", " and ")
        candidates.append(plus_fixed)
        if " + The " in name:
            candidates.append(name.replace(" + The ", " and the "))

    current = name
    if "/\\" in current or "\\/" in current:
        current = current.replace("/\\", "&").replace("\\/", "&")
    if " + " in current:
        current = current.replace(" + ", " and ")
    if current != name and current not in candidates:
        candidates.append(current)

    return candidates



# Spotify title decorations: version/credit qualifiers appended to the
# canonical name ("Song - Radio Edit", "Song (feat. X) [Y Remix]") that
# Last.fm's catalog often doesn't know. Word-bounded so "Alive" doesn't
# trigger on "live"; only segments matching these are stripped - a dash or
# parenthetical that is part of the actual title ("Party - Ich will abgehn",
# "(I Can't Get No) Satisfaction") stays.
_LOOKUP_NAME_DECORATION = re.compile(
    r"\b(feat\.?|featuring|with|from|remaster(?:ed)?|remix(?:ed)?|version|edit|mix|"
    r"live|acoustic|deluxe|bonus|mono|stereo|single|sped up|slowed|anniversary|"
    r"re-?recorded|demo|instrumental|extended|radio|session)\b",
    re.IGNORECASE)
_LOOKUP_NAME_GROUP = re.compile(r"\s*\(([^()]*)\)|\s*\[([^\[\]]*)\]")
LOOKUP_NAME_DASH_SEPARATOR = " - "

# Stylized-artist-name fallback: Last.fm's community tagging resolves real
# diacritics fine on the first try (Måneskin, Emilíana Torrini already carry
# genres under their stored spelling), but some artists use lookalike
# Latin-Extended letters or decorative marks purely as visual styling that
# the *real* Last.fm entry doesn't carry - confirmed live against the API,
# not a guess: "HUGØ" has no tags, "HUGO" does; "Jinka †" has no tags,
# "Jinka" does. Only tried as a last-resort retry after the exact stored
# name comes back empty, so it never masks a genuine first-try match.
_STYLIZED_LETTER_MAP = str.maketrans({
    "Ø": "O", "ø": "o", "Æ": "AE", "æ": "ae", "Å": "A", "å": "a",
    "ß": "ss", "Đ": "D", "đ": "d", "Ł": "L", "ł": "l",
    "Œ": "OE", "œ": "oe", "Þ": "Th", "þ": "th",
})
# Decorative dingbats/marks (daggers, stars, braille-blank padding) seen
# appended to stylized artist names - visual flourish, not letters.
_DECORATIVE_CODEPOINT_RANGES = (
    (0x2020, 0x2021),   #< † ‡
    (0x2022, 0x2022),   #< •
    (0x2600, 0x27BF),   #< misc symbols & dingbats (★ ☆ ✦ ♪ ...)
    (0x2800, 0x28FF),   #< braille patterns (used as invisible padding)
)

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

# artist.getinfo's result for the artist-bio feature - same outcome status
# taxonomy as FetchOutcome, but carrying one cleaned bio string (or None)
# instead of a tag list, so it gets its own named tuple rather than
# overloading FetchOutcome.tags with a different kind of value.
ArtistInfoOutcome = namedtuple("ArtistInfoOutcome", ["status", "bio"])

# album.getinfo's result for the album-bio feature - same shape as
# ArtistInfoOutcome, one per entity kind rather than a shared tuple, since
# the two are extracted from differently-shaped payloads (artist.bio vs
# album.wiki).
AlbumInfoOutcome = namedtuple("AlbumInfoOutcome", ["status", "bio"])


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


def cleanLookupName(name: str) -> str:
    """`name` with Spotify version/credit decorations removed - the retry
    form for a lookup whose exact name found nothing on Last.fm. A " - X"
    suffix goes only when X matches a decoration keyword, and each "(...)"/
    "[...]" group goes only when its content does. Returns the original name
    unchanged when nothing qualifies or stripping would leave nothing."""
    base, separator, suffix = name.partition(LOOKUP_NAME_DASH_SEPARATOR)
    cleaned = base if separator and _LOOKUP_NAME_DECORATION.search(suffix) else name

    def _dropDecoratedGroup(match: re.Match) -> str:
        content = match.group(1) if match.group(1) is not None else match.group(2)
        return "" if _LOOKUP_NAME_DECORATION.search(content) else match.group(0)

    cleaned = _LOOKUP_NAME_GROUP.sub(_dropDecoratedGroup, cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned if cleaned and cleaned != name else name


def _isDecorativeChar(char: str) -> bool:
    codepoint = ord(char)
    return any(low <= codepoint <= high for low, high in _DECORATIVE_CODEPOINT_RANGES)


def foldStylizedArtistName(name: str) -> str:
    """`name` with stylized Latin-Extended letters swapped for their plain
    ASCII form and decorative marks stripped - the retry form for an artist
    lookup whose exact name found nothing on Last.fm (see
    _STYLIZED_LETTER_MAP). Also collapses stray whitespace, since a trailing
    space alone can be the whole reason a name never matched. Returns the
    original name unchanged when nothing qualifies."""
    folded = name.translate(_STYLIZED_LETTER_MAP)
    folded = "".join(char for char in folded if not _isDecorativeChar(char))
    folded = " ".join(folded.split())
    return folded if folded and folded != name else name


def filterTagsToGenres(tags: list) -> list[str]:
    """The whitelisted genres in a Last.fm tag list, ranked by tag count
    (alphabetical on the constant ties Last.fm's 0-100 normalized counts
    produce), deduped after normalization keeping each genre's best count,
    capped at GENRE_MAX_TAGS_PER_ENTITY. Tags missing the whitelist get one
    more chance through GENRE_TAG_ALIASES. Malformed entries are skipped."""
    whitelist = loadGenreWhitelist()
    bestCounts: dict[str, int] = {}
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        name = tag.get("name")
        if not isinstance(name, str):
            continue
        normalized = normalizeGenreTag(name)
        canonical = whitelist.get(normalized)
        if canonical is None:
            aliasTarget = GENRE_TAG_ALIASES.get(normalized)
            if aliasTarget is not None:
                canonical = whitelist.get(normalizeGenreTag(aliasTarget))
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
    """toptags.tag from a Last.fm *.gettoptags payload. A single tag arrives
    as a bare dict instead of a 1-element list - normalize both shapes;
    anything else reads as "no tags"."""
    toptags = payload.get("toptags") if isinstance(payload, dict) else None
    if not isinstance(toptags, dict):
        return []
    tags = toptags.get("tag")
    if isinstance(tags, dict):
        return [tags]
    if isinstance(tags, list):
        return tags
    return []


def _extractAlbumInfoTags(payload) -> list:
    """album.tags.tag from an album.getinfo payload - the fallback source for
    albums where album.gettoptags comes back empty even though Last.fm has
    real tag data (confirmed reproducible against the live API: not a
    caching/autocorrect artifact, a persistent gap between the two endpoints'
    backing data for some albums). Same bare-dict-vs-list normalization as
    _extractTags; `tags` can also arrive as an empty string when the album
    has no info-page tags either, which isn't a dict and reads as "no tags"."""
    album = payload.get("album") if isinstance(payload, dict) else None
    if not isinstance(album, dict):
        return []
    tagsContainer = album.get("tags")
    if not isinstance(tagsContainer, dict):
        return []
    tags = tagsContainer.get("tag")
    if isinstance(tags, dict):
        return [tags]
    if isinstance(tags, list):
        return tags
    return []


def _extractTrackInfoTags(payload) -> list:
    """track.toptags.tag from a track.getinfo payload - the fallback source
    for tracks where track.gettoptags comes back empty, mirroring
    _extractAlbumInfoTags for the same confirmed-live gettoptags-vs-getinfo
    server-side inconsistency (Last.fm's own API docs show track.getInfo
    embeds toptags.tag under the `track` key, the same shape album.getInfo
    embeds tags.tag under `album`). Same bare-dict-vs-list normalization as
    _extractTags."""
    track = payload.get("track") if isinstance(payload, dict) else None
    if not isinstance(track, dict):
        return []
    tagsContainer = track.get("toptags")
    if not isinstance(tagsContainer, dict):
        return []
    tags = tagsContainer.get("tag")
    if isinstance(tags, dict):
        return [tags]
    if isinstance(tags, list):
        return tags
    return []


# Artist-bio cleaning: Last.fm bios carry embedded HTML (a trailing "Read
# more on Last.fm" link) - that HTML is never rendered as-is (no sanitizer
# dependency added just for this; the tags are simply stripped, converting
# the bio to safe plain text), and the now-dead link *text* is dropped too
# rather than left as an orphaned, unclickable "Read more on Last.fm" phrase.
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# artist.getinfo's bio.summary is Last.fm's OWN excerpt of bio.content,
# truncated at a fixed character budget with no regard for sentence
# boundaries - confirmed against the live API: it routinely cuts off
# mid-sentence ("...As she grew interested in pursuing a music career").
# bio.content is the full, untruncated biography, so it's always preferred;
# summary is only a fallback for the rare response missing content. Since
# content can run several thousand characters, it's truncated ourselves
# (see _truncateBioToSentence) to a length that snaps back to a real
# sentence end instead of Last.fm's mid-word cut.
BIO_MAX_LENGTH = 600

# Both bio.summary and bio.content end with this same suffix - a "Read more"
# link, and (content only) a Creative Commons attribution sentence Last.fm
# requires for reproducing wiki text. Both are HTML-stripped by the time this
# runs, so the anchor text reads as plain "Read more on Last.fm". Removing
# them as one unit (rather than a plain substring replace of just the link
# text) avoids leaving a stray "sentence. ." where the link used to sit
# between two periods. The attribution is captured separately so it can be
# re-appended after truncation - it must never itself be cut off, and its
# canonical wording (_BIO_ATTRIBUTION_TEXT) is used instead of whatever
# whitespace/punctuation variant was actually captured.
_BIO_TRAILING_BOILERPLATE_RE = re.compile(
    r"\s*Read more on Last\.fm\.?\s*"
    r"(?P<cc>User-contributed text is available under the Creative Commons By-SA License;?"
    r"\s*additional terms may apply\.?)?\s*$",
    re.IGNORECASE)
_BIO_ATTRIBUTION_TEXT = ("User-contributed text is available under the Creative Commons "
                         "By-SA License; additional terms may apply.")

# Some artist names (e.g. containing a literal "+") resolve on Last.fm's side
# to a distinct "incorrect tag" merge/redirect entity instead of the real
# artist - confirmed via the live API (see the genre backfill investigation).
# That entity's own "bio" is Last.fm's boilerplate explanation of the
# mismatch, not a biography - showing it on an artist page would be
# confusing, not informative, so it's treated the same as "no bio available".
_INCORRECT_TAG_BIO_MARKER = "is an incorrect tag for"


def _truncateBioToSentence(text: str, maxLength: int) -> str:
    """Cuts `text` back to at most `maxLength` characters, snapping to the
    last sentence-ending punctuation so a displayed bio never stops mid-
    sentence the way Last.fm's own bio.summary truncation does. Falls back
    to the last word boundary only if no sentence end exists within the
    budget (one unusually long first sentence) - never cuts mid-word, and
    never fabricates an ellipsis."""
    if len(text) <= maxLength:
        return text
    window = text[:maxLength]
    boundary = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if boundary != -1:
        return window[:boundary + 1]
    cutoff = window.rfind(" ")
    return window[:cutoff] if cutoff > 0 else window


def _extractArtistBio(payload) -> str | None:
    """Cleaned, length-capped plain-text artist.getinfo biography, or None if
    there's nothing usable (missing/empty, or Last.fm's own "incorrect tag"
    merge-redirect boilerplate - see _INCORRECT_TAG_BIO_MARKER). Prefers the
    full bio.content over the pre-truncated bio.summary (see BIO_MAX_LENGTH's
    comment) and re-appends the Creative Commons attribution sentence after
    truncation when the source carried one, so it's always complete."""
    artist = payload.get("artist") if isinstance(payload, dict) else None
    if not isinstance(artist, dict):
        return None
    bio = artist.get("bio")
    if not isinstance(bio, dict):
        return None
    text = bio.get("content")
    if not isinstance(text, str) or not text.strip():
        text = bio.get("summary")
    if not isinstance(text, str):
        return None

    text = html.unescape(_HTML_TAG_RE.sub("", text))
    match = _BIO_TRAILING_BOILERPLATE_RE.search(text)
    hasAttribution = False
    if match is not None:
        hasAttribution = match.group("cc") is not None
        text = text[:match.start()]
    text = " ".join(text.split())
    if not text or _INCORRECT_TAG_BIO_MARKER in text:
        return None

    text = _truncateBioToSentence(text, BIO_MAX_LENGTH)
    if hasAttribution:
        text = f"{text} {_BIO_ATTRIBUTION_TEXT}"
    return text


def _extractAlbumBio(payload) -> str | None:
    """Cleaned, length-capped plain-text album.getinfo biography, or None if
    there's nothing usable. Same cleaning pipeline as _extractArtistBio
    (HTML stripping, trailing boilerplate/attribution handling, sentence-
    boundary truncation, the "incorrect tag" merge-redirect guard - album
    wiki text carries the same Last.fm boilerplate patterns as artist bios),
    just reading album.getinfo's `wiki` field instead of artist.getinfo's
    `bio` field."""
    album = payload.get("album") if isinstance(payload, dict) else None
    if not isinstance(album, dict):
        return None
    wiki = album.get("wiki")
    if not isinstance(wiki, dict):
        return None
    text = wiki.get("content")
    if not isinstance(text, str) or not text.strip():
        text = wiki.get("summary")
    if not isinstance(text, str):
        return None

    text = html.unescape(_HTML_TAG_RE.sub("", text))
    match = _BIO_TRAILING_BOILERPLATE_RE.search(text)
    hasAttribution = False
    if match is not None:
        hasAttribution = match.group("cc") is not None
        text = text[:match.start()]
    text = " ".join(text.split())
    if not text or _INCORRECT_TAG_BIO_MARKER in text:
        return None

    text = _truncateBioToSentence(text, BIO_MAX_LENGTH)
    if hasAttribution:
        text = f"{text} {_BIO_ATTRIBUTION_TEXT}"
    return text


class LastfmClient:
    def __init__(self, apiKey: str, rateLimiter: LastfmRateLimiter | None = None):
        self.apiKey = apiKey
        self.rateLimiter = rateLimiter if rateLimiter is not None else RATE_LIMITER

    def _lookupWithArtistNameFallback(self, artistName: str, fetchOne, isHit, logLabel: str):
        """Runs `fetchOne(artistName)` first. On a definitive-but-no-hit
        result (isHit(outcome) is False) - which includes NOT_FOUND (error 6):
        a verbatim name with no Last.fm match at all is exactly the case the
        slash/plus transforms exist for, e.g. "Axwell /\\ Ingrosso" 404s
        verbatim but resolves under "Axwell & Ingrosso" - retries fetchOne
        under each of normalizeArtistLookupName's transformed spellings. A
        candidate that doesn't hit (including a transient hiccup on that one
        name) is silently skipped rather than aborting the rest, since it
        says nothing about whether a *different* spelling would work.
        foldStylizedArtistName's plain-ASCII fold is then tried once more as
        the last resort, and its outcome (hit or not, even an aborted None)
        is returned unconditionally - the only later attempt allowed to
        override the original verbatim result, since it's the final word
        this lookup has left to give. Never replaces a real result and costs
        nothing extra on the majority of names with nothing to transform or
        fold. `fetchOne(name)` -> outcome | None; `isHit(outcome)` -> bool."""
        outcome = fetchOne(artistName)
        if outcome is None or outcome.status not in (OUTCOME_OK, OUTCOME_NOT_FOUND) or isHit(outcome):
            return outcome

        for candidate in normalizeArtistLookupName(artistName):
            if candidate == artistName:
                continue
            altOutcome = fetchOne(candidate)
            if altOutcome is not None and altOutcome.status == OUTCOME_OK and isHit(altOutcome):
                logger.info("[Lastfm] %s: artist name transformation recovered a result for %r "
                            "(tried as %r)", logLabel, artistName, candidate)
                return altOutcome

        folded = foldStylizedArtistName(artistName)
        if folded == artistName:
            return outcome
        fallback = fetchOne(folded)
        if fallback is not None and fallback.status == OUTCOME_OK and isHit(fallback):
            logger.info("[Lastfm] %s: stylized-name fold recovered a result for %r (tried as %r)",
                       logLabel, artistName, folded)
        return fallback

    def getArtistTopTags(self, artistName: str,
                         stop_event: threading.Event | None = None) -> FetchOutcome | None:
        return self._lookupWithArtistNameFallback(
            artistName,
            lambda name: self._fetchTopTags("artist.gettoptags", {"artist": name}, stop_event),
            lambda outcome: bool(outcome.tags),
            "artist tags")

    def getAlbumTopTags(self, artistName: str, albumName: str,
                        stop_event: threading.Event | None = None) -> FetchOutcome | None:
        return self._lookupWithArtistNameFallback(
            artistName,
            lambda name: self._fetchAlbumTopTagsForArtist(name, albumName, stop_event),
            lambda outcome: bool(outcome.tags),
            "album tags")

    def _fetchAlbumTopTagsForArtist(self, artistName: str, albumName: str,
                                    stop_event: threading.Event | None) -> FetchOutcome | None:
        """One album.gettoptags call for `artistName`/`albumName`, falling
        back to album.getinfo's embedded tags on a definitive-empty OR
        not-found result - album.gettoptags is confirmed unreliable for some
        albums: Last.fm's album.getinfo carries tag data (in its embedded
        `tags` field) that gettoptags misses for the identical (artist,
        album) pair, a persistent server-side inconsistency verified
        directly against the live API (not a caching or autocorrect
        artifact); the same divergence between the two endpoints can also
        surface as gettoptags 404ing (error 6) on a pair getinfo still
        resolves, so NOT_FOUND gets the same fallback as OK-with-no-tags.
        Never replaces a real result, and costs nothing extra on the (large)
        majority of albums where gettoptags already succeeds. This is the
        per-artist-name unit that getAlbumTopTags retries under alternate
        spellings via _lookupWithArtistNameFallback."""
        outcome = self._fetchTopTags("album.gettoptags",
                                     {"artist": artistName, "album": albumName}, stop_event)
        if outcome is None or outcome.status not in (OUTCOME_OK, OUTCOME_NOT_FOUND) or outcome.tags:
            return outcome
        fallback = self._fetchTopTags("album.getinfo",
                                      {"artist": artistName, "album": albumName}, stop_event,
                                      extractFn=_extractAlbumInfoTags)
        if fallback is not None and fallback.status == OUTCOME_OK and fallback.tags:
            logger.info("[Lastfm] album.getinfo fallback recovered %d tag(s) for %r / %r "
                       "after an empty album.gettoptags result", len(fallback.tags),
                       artistName, albumName)
        return fallback

    def getTrackTopTags(self, artistName: str, trackName: str,
                        stop_event: threading.Event | None = None) -> FetchOutcome | None:
        return self._lookupWithArtistNameFallback(
            artistName,
            lambda name: self._fetchTrackTopTagsForArtist(name, trackName, stop_event),
            lambda outcome: bool(outcome.tags),
            "track tags")

    def _fetchTrackTopTagsForArtist(self, artistName: str, trackName: str,
                                    stop_event: threading.Event | None) -> FetchOutcome | None:
        """One track.gettoptags call for `artistName`/`trackName`, falling
        back to track.getinfo's embedded tags on a definitive-empty OR
        not-found result - mirrors _fetchAlbumTopTagsForArtist for the same
        confirmed-live gettoptags-vs-getinfo server-side inconsistency (see
        _extractTrackInfoTags), including gettoptags 404ing (error 6) on a
        pair getinfo still resolves. Never replaces a real result, and costs
        nothing extra on the majority of tracks where gettoptags already
        succeeds. This is the per-artist-name unit that getTrackTopTags
        retries under alternate spellings via _lookupWithArtistNameFallback."""
        outcome = self._fetchTopTags("track.gettoptags",
                                     {"artist": artistName, "track": trackName}, stop_event)
        if outcome is None or outcome.status not in (OUTCOME_OK, OUTCOME_NOT_FOUND) or outcome.tags:
            return outcome
        fallback = self._fetchTopTags("track.getinfo",
                                      {"artist": artistName, "track": trackName}, stop_event,
                                      extractFn=_extractTrackInfoTags)
        if fallback is not None and fallback.status == OUTCOME_OK and fallback.tags:
            logger.info("[Lastfm] track.getinfo fallback recovered %d tag(s) for %r / %r "
                       "after an empty track.gettoptags result", len(fallback.tags),
                       artistName, trackName)
        return fallback

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
                      timeout: float | None = None,
                      extractFn=_extractTags) -> FetchOutcome | None:
        """None only when the rate-limit slot was never granted (stopping
        worker / validation timeout) - no request went out. `extractFn` pulls
        the tag list out of a successful payload - defaults to the
        *.gettoptags shape; pass `_extractAlbumInfoTags` for *.getinfo calls,
        whose tag data lives one level deeper under the entity key."""
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
        return self._classifyResponse(method, response, extractFn)

    def _classifyResponse(self, method: str, response, extractFn=_extractTags) -> FetchOutcome:
        status, payload = self._classifyResponseStatus(method, response)
        if status is not None:
            return FetchOutcome(status, [])
        return FetchOutcome(OUTCOME_OK, extractFn(payload))

    def getArtistInfo(self, artistName: str,
                      stop_event: threading.Event | None = None,
                      timeout: float | None = None) -> ArtistInfoOutcome | None:
        """One artist.getinfo lookup for the artist-bio feature - used both by
        lazyFetchArtistBio's on-demand one-shot fetch and by the background
        biography backfiller's own 30-day retry cycle (a separate schedule
        from the genre workers' gettoptags traffic), sharing the same
        process-wide rate limiter since it's still real load against the
        same per-IP ceiling. Retries under normalizeArtistLookupName's and
        foldStylizedArtistName's transformed spellings on a definitive
        no-bio result, same as getArtistTopTags - see
        _lookupWithArtistNameFallback."""
        return self._lookupWithArtistNameFallback(
            artistName,
            lambda name: self._fetchArtistInfoSingle(name, stop_event, timeout),
            lambda outcome: outcome.bio is not None,
            "artist bio")

    def _fetchArtistInfoSingle(self, artistName: str,
                               stop_event: threading.Event | None = None,
                               timeout: float | None = None) -> ArtistInfoOutcome | None:
        if not self.rateLimiter.acquire(stop_event=stop_event, timeout=timeout):
            return None
        query = {"method": "artist.getinfo", "api_key": self.apiKey, "format": "json",
                 "autocorrect": "1", "artist": artistName}
        try:
            response = requests.get(LASTFM_API_ROOT, params=query,
                                    timeout=LASTFM_HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as e:
            logger.warning("[Lastfm] artist.getinfo request failed: %s", e)
            return ArtistInfoOutcome(OUTCOME_TRANSIENT, None)
        status, payload = self._classifyResponseStatus("artist.getinfo", response)
        if status is not None:
            return ArtistInfoOutcome(status, None)
        return ArtistInfoOutcome(OUTCOME_OK, _extractArtistBio(payload))


    def getAlbumInfo(self, artistName: str, albumName: str,
                     stop_event: threading.Event | None = None,
                     timeout: float | None = None) -> AlbumInfoOutcome | None:
        """One album.getinfo lookup for the album-bio feature - mirrors
        getArtistInfo, sharing the same process-wide rate limiter. A
        dedicated call (not piggybacked on getAlbumTopTags's own
        album.getinfo fallback, which only fires when album.gettoptags comes
        back empty - the minority case) so bio coverage isn't starved for
        the majority of albums where gettoptags already succeeds. Retries
        under normalizeArtistLookupName's and foldStylizedArtistName's
        transformed artist spellings on a definitive no-bio result, same as
        getArtistInfo - see _lookupWithArtistNameFallback."""
        return self._lookupWithArtistNameFallback(
            artistName,
            lambda name: self._fetchAlbumInfoSingle(name, albumName, stop_event, timeout),
            lambda outcome: outcome.bio is not None,
            "album bio")

    def _fetchAlbumInfoSingle(self, artistName: str, albumName: str,
                              stop_event: threading.Event | None = None,
                              timeout: float | None = None) -> AlbumInfoOutcome | None:
        if not self.rateLimiter.acquire(stop_event=stop_event, timeout=timeout):
            return None
        query = {"method": "album.getinfo", "api_key": self.apiKey, "format": "json",
                 "autocorrect": "1", "artist": artistName, "album": albumName}
        try:
            response = requests.get(LASTFM_API_ROOT, params=query,
                                    timeout=LASTFM_HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as e:
            logger.warning("[Lastfm] album.getinfo request failed: %s", e)
            return AlbumInfoOutcome(OUTCOME_TRANSIENT, None)
        status, payload = self._classifyResponseStatus("album.getinfo", response)
        if status is not None:
            return AlbumInfoOutcome(status, None)
        return AlbumInfoOutcome(OUTCOME_OK, _extractAlbumBio(payload))

    def _classifyResponseStatus(self, method: str, response) -> tuple:
        """(status, payload). status is None only on a genuine success (200,
        parseable, no error field) - payload is then the parsed JSON body,
        ready for the caller's own extraction. Any other status is the
        caller's final outcome and payload is None. Shared by every *.gettoptags
        / *.getinfo call regardless of what shape of data it ultimately wants
        out of a successful payload."""
        if response.status_code == 429:
            self.rateLimiter.applyBackoff(LASTFM_RATE_LIMIT_BACKOFF_SECONDS)
            logger.warning("[Lastfm] HTTP 429 on %s - backing off %ds", method,
                           LASTFM_RATE_LIMIT_BACKOFF_SECONDS)
            return OUTCOME_TRANSIENT, None

        try:
            payload = response.json()
        except ValueError:
            logger.warning("[Lastfm] Unparseable response on %s (status %d)",
                           method, response.status_code)
            return OUTCOME_TRANSIENT, None

        # Last.fm reports errors in the body (sometimes with HTTP 200) - the
        # error code, not the HTTP status, is authoritative.
        if isinstance(payload, dict) and "error" in payload:
            code = payload.get("error")
            if code == LASTFM_ERROR_RATE_LIMITED:
                self.rateLimiter.applyBackoff(LASTFM_RATE_LIMIT_BACKOFF_SECONDS)
                logger.warning("[Lastfm] Rate limited (error 29) on %s - backing off %ds",
                               method, LASTFM_RATE_LIMIT_BACKOFF_SECONDS)
                return OUTCOME_TRANSIENT, None
            if code in (LASTFM_ERROR_INVALID_API_KEY, LASTFM_ERROR_KEY_SUSPENDED):
                return OUTCOME_INVALID_KEY, None
            if code == LASTFM_ERROR_NOT_FOUND:
                return OUTCOME_NOT_FOUND, None
            logger.warning("[Lastfm] Error %s on %s: %s", code, method,
                           payload.get("message", ""))
            return OUTCOME_TRANSIENT, None

        if response.status_code != 200:
            return OUTCOME_TRANSIENT, None

        return None, payload
