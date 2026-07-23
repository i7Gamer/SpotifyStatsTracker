from __future__ import annotations
import datetime
import logging
import os
import tempfile
import threading
import time
import json
from pathlib import Path
from io import BytesIO

import requests
from PIL import Image
import concurrent.futures

try:
    from Database.Formatters.spotifyClient import Client
    from Database.Importers.StreamingHistoryImporter import Importer
    from Database.Importers.AutoImporter import AutoImporter
    from Database.Listeners.spotifyListener import Listener
    from Database.repository import (
        Repository, IMAGE_KIND_TRACK, IMAGE_KIND_ARTIST, IMAGE_STATUS_OK, IMAGE_STATUS_FAILED,
    )
    from Database.db import BEHAVIORAL_COLUMNS, SKIP_THRESHOLD_MS
    from Database.utils import parseError, convertToDatetime, dateToString, startOfDay, startOfWeek, startOfMonth, timeToInt, getTimezone
    from Database.lastfm import LastfmClient, filterTagsToGenres, cleanLookupName, OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_TRANSIENT, OUTCOME_INVALID_KEY
except ModuleNotFoundError:
    from Formatters.spotifyClient import Client
    from Importers.StreamingHistoryImporter import Importer
    from Importers.AutoImporter import AutoImporter
    from Listeners.spotifyListener import Listener
    from repository import Repository, IMAGE_KIND_TRACK, IMAGE_KIND_ARTIST, IMAGE_STATUS_OK, IMAGE_STATUS_FAILED
    from db import BEHAVIORAL_COLUMNS, SKIP_THRESHOLD_MS
    from utils import parseError, convertToDatetime, dateToString, startOfDay, startOfWeek, startOfMonth, timeToInt, getTimezone
    from lastfm import LastfmClient, filterTagsToGenres, cleanLookupName, OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_TRANSIENT, OUTCOME_INVALID_KEY

logger = logging.getLogger(__name__)

TRUTHY_DEBUG_VALUES = {"1", "true"}

# The genre-coverage categories (also the SQL alias prefixes in
# getGenreCoverage). The overall percentage is the mean across these, so the
# count must track this tuple - never a bare literal.
GENRE_COVERAGE_CATEGORIES = ("song", "album", "artist")

# How far back getCurrentStreak scans for the ongoing daily streak. A live
# streak is always far shorter; this only bounds the bucket query so it never
# walks the whole history to compute a number that can't exceed this anyway.
CURRENT_STREAK_LOOKBACK_DAYS = 400

IMAGE_DOWNLOAD_WORKERS = 5   #< bounds total concurrent image downloads for the whole process, not per user

ARTIST_BIO_FETCH_WORKERS = 2   #< bounds concurrent artist-bio fetches for the whole process; each is one
                                #  small artist.getinfo call (no image-style download/resize work), so a
                                #  much smaller pool than IMAGE_DOWNLOAD_WORKERS is enough

ALBUM_BIO_FETCH_WORKERS = 2    #< separate pool from ARTIST_BIO_FETCH_WORKERS so album lazy-fetches never
                                #  queue behind artist lazy-fetches (or vice versa)

# getCompletionStats' complete-vs-partial boundary is now an admin setting
# (COMPLETION_COMPLETE_PERCENT_KEY, default 80%): among real plays (is_skip=0), a
# listen at/over that percent of the track's duration counts as complete, else
# partial. Skips are the is_skip=1 rows (the admin-tunable skip threshold that
# replaced both the old 30s line and the play_skips table). See getCompletionStats.

# Images are shared across every user (album art / artist photos are the same
# bytes for everyone), so they live in one directory tree instead of under each
# user's own folder. Inside Data/ (see Database/db.py's DEFAULT_DB_PATH) so the
# Docker volume mount that persists the database also covers it.
MEDIA_DIR = Path(__file__).resolve().parent / "Data" / "Media"

_SPOTIFY_IMAGE_CDN = "https://i.scdn.co/image/"


def _imageIdFromConnectMeta(meta) -> str | None:
    """Extract the album imageId (album ID string) from a connect-state
    metadata dict or Metadata dataclass.  Returns None if unavailable.

    The connect-state metadata carries the album URI in the form
    'spotify:album:<id>'; the album ID is what the rest of the system
    uses as imageId (matches the on-disk filename <albumId>.jpeg)."""
    album_uri = (meta.get("album_uri") if isinstance(meta, dict)
                 else getattr(meta, "album_uri", None))
    if not album_uri:
        return None
    parts = album_uri.rsplit(":", 1)
    return parts[-1] if len(parts) == 2 and parts[-1] else None


def _imageUrlFromConnectMeta(meta) -> str | None:
    """Build the Spotify CDN URL for the track's cover art from the
    connect-state metadata dict or Metadata dataclass.  Returns None if
    unavailable.

    The connect-state carries the image as 'spotify:image:<hash>'; the
    CDN URL is https://i.scdn.co/image/<hash>."""
    spotify_uri = (meta.get("image_xlarge_url") or meta.get("image_url")
                   if isinstance(meta, dict)
                   else getattr(meta, "image_xlarge_url", None)
                        or getattr(meta, "image_url", None))
    if not spotify_uri:
        return None
    parts = spotify_uri.rsplit(":", 1)
    if len(parts) != 2 or not parts[-1]:
        return None
    return _SPOTIFY_IMAGE_CDN + parts[-1]


class _LastfmInvalidKeyError(Exception):

    """The stored Last.fm key is invalid/suspended (error 10/26): raised out
    of a worker batch so the loop idles instead of burning 4 failing requests
    per second. A fixed key is picked up on the next cycle's fresh read."""


class _ImportRunState:
    """Play rows written by the current import run (or multi-file batch).

    The import's duplicate reconciliation matches each incoming entry against
    nearby existing plays and "corrects" a single differing match instead of
    inserting. That is only valid against rows from *other* sources (live
    listener / Web API backfill, where played_at semantics can differ) - but
    inserts happen inside the same transaction the matching reads from, so
    without this state an entry would also match the play a previous entry of
    the same run just wrote, collapsing two genuine plays (e.g. a short skip
    immediately followed by a replay of the same track) into one row.

    Invariant: an existing row can be claimed by at most one import entry per
    run - one physical play corresponds to exactly one export entry."""

    def __init__(self):
        self.claimedRowIds: set[int] = set()      #< existing rows updated or confirmed identical by this run
        self.insertedPlayKeys: set[tuple] = set() #< (track_id, played_at) of rows inserted by this run
        self.correctedYears: set[int] = set()     #< years to drop from Wrapped cache once a deferred-commit
                                                   #  batch (atomic overwrite) actually commits - see
                                                   #  _importHistoryLocked's deferCommit
        self.pendingImageTracks: dict[str, dict] = {}  #< tracks awaiting saveImagesFromTrack() once a
                                                   #  deferred-commit batch actually commits (same reason
                                                   #  as correctedYears - image-claiming self-commits too)

    def isOwnWrite(self, trackId: str, play: dict) -> bool:
        return play["id"] in self.claimedRowIds or (trackId, play["played_at"]) in self.insertedPlayKeys


from Database.media_fetch import MediaFetchMixin
from Database.import_service import ImportMixin
from Database.workers import WorkerLifecycleMixin
from services.listening_calendar import buildListeningCalendar, CALENDAR_WEEKS


class Database(MediaFetchMixin, ImportMixin, WorkerLifecycleMixin):
    PROGRESS_UPDATE_INTERVAL = 10   #< Write import progress to disk every N entries instead of every entry
    RECONNECT_MAX_RETRIES = 10  #< max reconnection attempts before giving up (~30 min window with backoff)
    RECONNECT_INITIAL_DELAY = 1  #< initial backoff in seconds
    RECONNECT_MAX_DELAY = 300  #< cap backoff at 5 minutes
    LISTENER_STOP_LOCK_TIMEOUT_SECONDS = 2  #< bound how long stop() waits for an in-flight
                                             #  startListener (a live Spotify login, ~15s) to release
                                             #  the listener lock - on timeout stop() proceeds and the
                                             #  in-flight call sees _stopping afterwards and tears its
                                             #  own freshly-built listener down
    BACKFILL_INSERT_GUARD_EXTRA_SECONDS = 60  #< margin added on top of a track's own duration for the
                                               #  wide, backfill-only insert-time dedup guard (see
                                               #  appendTrackData) - accounts for Spotify's played_at
                                               #  field being documented as inconsistent about whether
                                               #  it reports a track's start or end time (spotify/web-api#1083)
    IMPORT_MATCH_START_WINDOW_SECONDS = 15     #< an existing play starting within this window of an imported
                                               #  play is treated as the same physical play recorded with a
                                               #  slightly different timestamp (e.g. listener vs export)
    IMPORT_MATCH_END_WINDOW_SECONDS = 60       #< same idea for sources whose played_at recorded the track's
                                               #  end instead of its start (see the start/end ambiguity note
                                               #  on BACKFILL_INSERT_GUARD_EXTRA_SECONDS): imported start +
                                               #  track duration must land within this window of the DB row
    DUPLICATE_RECORDING_TOLERANCE_SECONDS = 5  #< max gap between two same-track local plays for them to count as
                                                #  the same real listen recorded twice (once by the live listener,
                                                #  once by Web API backfill) rather than a genuine replay. Proximity
                                                #  alone is NOT proof - real exports contain skip-then-restart pairs
                                                #  seconds apart - so reconciliation additionally requires the
                                                #  cluster to span different sources (see _reconcileWithWebApiHistory)
    WEB_API_BACKFILL_SOURCE = "web_api_backfill"  #< play source recorded by the Web API backfill; its
                                                   #  created_reason is "<source>_play (user: ...)" (appendTrackData)

    NOW_PLAYING_STALE_GRACE_MS = 60_000        #< a "playing" track whose duration (plus this) has fully elapsed
                                                #  since the last connect-state update is a frozen/stale feed,
                                                #  not a real playback - report nothing instead

    LISTENER_DURATION_CORRUPTION_FACTOR = 10   #< a listener-reported play duration more than this many times
                                                #  the track's own length is SpotipyFree corruption (e.g.
                                                #  7062895ms for a 171s track) - the play is recorded with the
                                                #  track's actual length instead of being dropped

    BACKFILLER_MIN_START_DELAY = 30            #< random startup-offset bounds for the metadata backfiller,
    BACKFILLER_MAX_START_DELAY = 90            #  in seconds - staggers per-user threads after a restart

    WRAPPED_WORKER_MIN_START_DELAY = 60        #< minimum initial random startup delay in seconds
    WRAPPED_WORKER_MAX_START_DELAY = 300       #< maximum initial random startup delay in seconds
    WRAPPED_WORKER_LOOP_INTERVAL = 900         #< interval between consecutive checks in seconds (15 minutes)
    WRAPPED_YEAR_DELAY_SECONDS = 5             #< breathing room delay in seconds between recalculating years

    BACKFILLER_ALBUM_QUEUE_SIZE = 80           #< number of albums queued from DB for backfilling
    BACKFILLER_IDLE_WAIT_SECONDS = 300         #< wait between metadata-backfill cycles when there's nothing
                                                #  to do (kill switch off, queue drained) or after a cycle

    LASTFM_BACKFILLER_MIN_START_DELAY = 30     #< random startup-offset bounds for the Last.fm genre
    LASTFM_BACKFILLER_MAX_START_DELAY = 90     #  backfiller, in seconds - staggers per-user threads
    LASTFM_QUEUE_BATCH_SIZE = 30               #< entities claimed per kind (artists/albums/tracks) per cycle
    LASTFM_IDLE_WAIT_SECONDS = 300             #< wait between cycles once both queues are drained (or after errors)

    # The biography backfiller runs as its own thread alongside the genre one
    # (not sequentially after it) - a later startup window just gives genres a
    # head start on the shared rate limiter, not a hard ordering guarantee.
    LASTFM_BIOGRAPHY_BACKFILLER_MIN_START_DELAY = 120
    LASTFM_BIOGRAPHY_BACKFILLER_MAX_START_DELAY = 180
    LASTFM_BIOGRAPHY_QUEUE_BATCH_SIZE = 20     #< smaller than LASTFM_QUEUE_BATCH_SIZE: one artist.getinfo
                                                #  call per entity, sharing the same rate limiter as genres
    LASTFM_BIOGRAPHY_IDLE_WAIT_SECONDS = 300   #< wait between cycles once the queue is drained (or after errors)

    # The album biography backfiller runs as its own thread alongside the
    # artist one (not sequentially after it) - same independent-thread shape
    # as the genre-vs-biography split above.
    LASTFM_ALBUM_BIOGRAPHY_BACKFILLER_MIN_START_DELAY = 120
    LASTFM_ALBUM_BIOGRAPHY_BACKFILLER_MAX_START_DELAY = 180
    LASTFM_ALBUM_BIOGRAPHY_QUEUE_BATCH_SIZE = 20     #< one album.getinfo call per entity, same rate limiter
    LASTFM_ALBUM_BIOGRAPHY_IDLE_WAIT_SECONDS = 300   #< wait between cycles once the queue is drained (or after errors)

    WORKER_HEALTH_FAILING_THRESHOLD = 3        #< consecutive failed cycles before a periodic worker counts
                                                #  as FAILING on /admin - matches the "3 consecutive" precedent
                                                #  already used for the Spotify re-auth flake filter

    # Shared across every Database instance (every user) in this process. Image
    # download de-duplication is enforced by the `images` table (atomic across
    # threads *and* users), so a single bounded pool for the whole process is
    # enough - there's no need for one per user, and no need for the old
    # per-user in-memory id sets / metadata.json files this replaces.
    imgDir_tracks = MEDIA_DIR / "tracks"
    imgDir_artists = MEDIA_DIR / "artists"
    _imageDownloadExecutor = concurrent.futures.ThreadPoolExecutor(max_workers=IMAGE_DOWNLOAD_WORKERS)
    # Same shape as _imageDownloadExecutor, but for the artist-bio feature's
    # lazy fetch: a much smaller pool since each task is one lightweight
    # artist.getinfo call, not a download+resize.
    _artistBioFetchExecutor = concurrent.futures.ThreadPoolExecutor(max_workers=ARTIST_BIO_FETCH_WORKERS)
    # Same idea, for the album-bio feature's lazy fetch - its own pool (see
    # ALBUM_BIO_FETCH_WORKERS) rather than sharing _artistBioFetchExecutor.
    _albumBioFetchExecutor = concurrent.futures.ThreadPoolExecutor(max_workers=ALBUM_BIO_FETCH_WORKERS)
    _active_backfills = set()
    _backfill_lock = threading.Lock()
    # Same idea for the Last.fm genre backfillers: catalog entities are shared,
    # so two users' workers must not fetch the same (kind, id) concurrently.
    _lastfm_active = set()
    _lastfm_active_lock = threading.Lock()

    @classmethod
    def configureWorkerPools(cls, repo) -> None:
        """Resize the shared background thread pools from admin settings, read
        once at startup (SpotifyDashboardApp.__init__, after migrations). These
        pools are process-wide and sized before any task is submitted, so a
        changed setting only takes effect after a restart - the admin panel
        labels them accordingly. Each falls back to its code default. Recreating
        the executors here is safe: ThreadPoolExecutor spawns no threads until a
        task is submitted, and none has been at startup, so the import-time
        placeholders are discarded without leaking threads."""
        cls._imageDownloadExecutor = concurrent.futures.ThreadPoolExecutor(
            max_workers=repo.getImageDownloadWorkers(IMAGE_DOWNLOAD_WORKERS))
        cls._artistBioFetchExecutor = concurrent.futures.ThreadPoolExecutor(
            max_workers=repo.getArtistBioFetchWorkers(ARTIST_BIO_FETCH_WORKERS))
        cls._albumBioFetchExecutor = concurrent.futures.ThreadPoolExecutor(
            max_workers=repo.getAlbumBioFetchWorkers(ALBUM_BIO_FETCH_WORKERS))

    def __init__(self, user: str, cookiesFile: str | None = None, email: str | None = None, dbPath=None,
                 shutdown_event: threading.Event | None = None):
        if not user:
            raise ValueError("Database user must be specified and cannot be empty.")
        self.user = user
        self.cookiesFile = cookiesFile
        self.email = email
        self.listener = None
        self.baseDir = Path(__file__).resolve().parent

        # Shutdown coordination. shutdown_event is the app-wide "we are
        # exiting" signal (SpotifyDashboardApp shares its _stop_event here);
        # _stopping is this instance's own end-of-life flag, set by
        # signalStop()/stop() and never cleared. Both gate startListener and
        # the onStale reconnect so a stale-feed check firing during shutdown
        # can no longer resurrect a listener nothing can reach (the 2026-07-17
        # hang). _listener_lock serializes startListener against stop() and
        # against concurrent reconnects (health check vs onStale), whose
        # interleaved stop/swap used to orphan a running listener.
        self.shutdown_event = shutdown_event if shutdown_event is not None else threading.Event()
        self._stopping = False
        self._listener_lock = threading.Lock()

        # Health monitoring: track listener state for graceful degradation
        self._health_lock = threading.RLock()
        self.listener_health = "INITIALIZING"  # INITIALIZING, HEALTHY, DEGRADED, DEAD
        self.listener_last_poll_time = None  # timestamp of last successful poll
        self.listener_error_count = 0  # consecutive errors
        self.listener_last_error = None  # last error message

        # All Database instances (one per user) share the same underlying SQLite
        # file - catalog data (tracks/artists/albums/images) is global, so it's
        # stored once regardless of how many users have played a given track.
        self.repo = Repository(dbPath) if dbPath is not None else Repository()
        self.repo.upsertUser(user, email)

        self.refreshSettings()

        self.autoImportFolderPath = self.baseDir / ".." / "autoImport" / self.user

        # Serializes this user's imports across entry points - the web upload
        # route runs importHistoryBatch on its own thread while AutoImporter
        # runs it on the watchdog thread, with nothing else coordinating them.
        # Concurrent runs interleave their staged transactions and defeat the
        # batch-scoped duplicate reconciliation (_ImportRunState); serialized,
        # a double-submit resolves cleanly instead (the second run sees the
        # first's recorded file hash and skips). RLock: importHistoryBatch
        # calls importHistory, which takes the same lock.
        self._importLock = threading.RLock()

        filterKeyword = os.environ.get("IMPORT_KEYWORD", None)
        logger.info("auto import filtering by %s", filterKeyword)
        # importHistoryBatch (not importHistory): files dropped together share
        # one import run state, so a skip/replay pair straddling a file
        # boundary isn't collapsed - and a bad file doesn't abort the rest.
        self.autoImporter = AutoImporter(folderPath=self.autoImportFolderPath,
                                         importCallback=self.importHistoryBatch,
                                         pollInterval=5,
                                         keyword=filterKeyword)

        self._initWorkerTelemetry()

        self.backfiller_thread = None
        self.backfiller_stop_event = threading.Event()
        self.startMetadataBackfiller()

        self.wrapped_thread = None
        self.wrapped_stop_event = threading.Event()
        # Guards the lazily-created per-year locks below (not the recalculation
        # itself) so the periodic worker and an on-demand /wrapped recalculation
        # never both run _calculateAndSaveWrapped for the same year at once.
        self._wrapped_recalc_locks_guard = threading.Lock()
        self._wrapped_recalc_locks: dict[int, threading.Lock] = {}
        self.startWrappedCalculationsWorker()

        self.lastfm_thread = None
        self.lastfm_stop_event = threading.Event()
        # No-op for users without a stored Last.fm key (no idle thread); the
        # profile page's key save re-invokes it once a key lands.
        self.startLastfmGenreBackfiller()

        self.lastfm_biography_thread = None
        self.lastfm_biography_stop_event = threading.Event()
        self.startLastfmBiographyBackfiller()

        self.lastfm_album_biography_thread = None
        self.lastfm_album_biography_stop_event = threading.Event()
        self.startLastfmAlbumBiographyBackfiller()

    def refreshSettings(self) -> None:
        from zoneinfo import ZoneInfo
        import Database.utils as utils
        try:
            self.settings = self.repo.getUserSettings(self.user)
            tz_name = self.settings.get("timezone")
            self.tz = ZoneInfo(tz_name) if tz_name else utils.getTimezone()
        except Exception:
            self.tz = utils.getTimezone()

    def _materializeCookiesFile(self) -> Path:
        """SpotipyFree/spotapi only know how to read a Spotify session from a file
        path (spotapi.saver.JSONSaver), not from a dict - write this user's
        cookies (the database is the source of truth) to a short-lived temp file
        in the same [{"identifier", "cookies"}, ...] shape SpotipyFree.saveSession
        produces. The caller is responsible for deleting it once the client
        holding it has been constructed - it's only read at construction time."""
        cookies = self.repo.getUserCookies(self.user) or {}
        email = self.repo.getEmailForUsername(self.user) or self.email
        tmpFd, tmpPath = tempfile.mkstemp(prefix=f"cookies_{self.user}_", suffix=".json")
        os.close(tmpFd)
        tmpPath = Path(tmpPath)
        payload = [{"identifier": email, "cookies": cookies}]
        tmpPath.write_text(json.dumps(payload), encoding="utf-8")
        if os.environ.get("FLASK_DEBUG"):
            logger.debug(
                "Materialized cookies file for user=%s: path=%s, identifier=%s, has_cookies=%s",
                self.user, tmpPath, email, bool(cookies)
            )
        return tmpPath

    def _withCookiesFile(self, factory):
        """Call `factory(cookiesFilePath)` using either an explicitly-provided
        self.cookiesFile (manual/dev usage, e.g. this module's __main__ block) or
        a temp file materialized from this user's cookies in the database (the
        normal app path, where Database is constructed without a cookiesFile)."""
        if self.cookiesFile:
            return factory(self.cookiesFile)
        tmpPath = self._materializeCookiesFile()
        try:
            return factory(str(tmpPath))
        finally:
            tmpPath.unlink(missing_ok=True)

    @staticmethod
    def _splitEntryAndTrack(metadata: dict) -> tuple[dict, dict]:
        entry = {
            "id": metadata["id"],
            "playedAt": metadata["playedAt"],
            "timePlayed": metadata["timePlayed"],
            "playedFrom": metadata.get("playedFrom"),
            # Importer-decided routing/enrichment info (absent on listener metas)
            "isSkip": metadata.get("isSkip", False),
            "importExtras": metadata.get("importExtras"),
        }
        track = {k: v for k, v in metadata.items()
                 if k not in ("playedAt", "timePlayed", "playedFrom", "isSkip", "importExtras")}
        return entry, track

    @staticmethod
    def _mergeEntryWithTrack(entry: dict, track: dict) -> dict:
        meta = track.copy()
        meta["playedAt"] = entry["playedAt"]
        meta["timePlayed"] = entry["timePlayed"]
        meta["playedFrom"] = entry.get("playedFrom")
        meta["extras"] = entry.get("extras")   #< behavioral columns, when the read carried them
        return meta

    def _paginateEntries(self, entries: list) -> list:
        """Merge each play entry with its track's catalog metadata. Track
        metadata for every distinct id in `entries` is fetched in one batched
        round-trip (Repository.getTracksByIds) rather than once per entry -
        hydrating a page of history used to cost 3 queries per play. A track
        id that isn't in the catalog yet (rare) falls back to the single-track
        path, which also handles fetching it live from the listener."""
        trackIds = list({entry["id"] for entry in entries})
        tracksById = self.repo.getTracksByIds(trackIds)

        result = []
        for entry in entries:
            track = tracksById.get(entry["id"])
            if track is None:
                track = self._ensureTrackMetadata(entry["id"])
            if track is None:
                continue
            result.append(self._mergeEntryWithTrack(entry, track))
        return result

    @staticmethod
    def _splitContextUri(contextUri: str) -> tuple[str, str] | None:
        """('type', 'id') from a playedFrom value like "playlist:xyz"/"album:xyz",
        or None if malformed. playedFrom is only ever written in that shape (see
        spotifyClient.formatTrack), so a colon-less value means corrupt data -
        degrade to "no known context" instead of a ValueError that would 500 the
        history page."""
        parts = contextUri.split(":", 1)
        if len(parts) != 2:
            logger.warning("Malformed playedFrom context %r - expected 'type:id'", contextUri)
            return None
        return parts[0], parts[1]

    def playlistName(self, playlistUri: str | None) -> str | None:
        """Return the playlist name for a Spotify playlist URI or id, caching it on first lookup."""
        if not playlistUri:
            return None
        parsed = self._splitContextUri(playlistUri)
        if parsed is None:
            return None
        contextType, playlistId = parsed
        return self.repo.getPlaylistName(playlistId, contextType)

    def updatePlaylists(self, playlist: str | None) -> None:
        if playlist is None:
            return
        parsed = self._splitContextUri(playlist)
        if parsed is None:
            return
        contextType, playlistId = parsed
        if self.repo.playlistKnown(playlistId, contextType):
            return
        try:
            if contextType == "album":
                name = self.listener.albumName(playlistId)
            else:
                name = self.listener.playlistName(playlistId)
        except Exception as e:
            logger.warning(
                "Error occurred while fetching playlist name for %s (probably due to playlist being private): %s",
                playlistId, e,
            )
            name = None
        self.repo.upsertPlaylistName(playlistId, contextType, name)

    # ---- history / entries ----------------------------------------------------------

    def getEntriesCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> int:
        """Return total number of entries in the database, optionally scoped
        to [startDate, endDate) - see getEntriesFromNew's identical param."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getPlaysCount(self.user, startTs=startTs, endTs=endTs)

    def getEntriesFromNew(self, count: int | None = None, startIndex: int = 0, fullPagination: bool = True,
                           startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> list:
        """ Return the latest `count` entries from history, sorted from newest to oldest. If count is None, return all entries.
        startDate/endDate optionally scope this to a half-open [startDate, endDate) range - used by the Dashboard's
        chart click-through (see app.py's dashboard()), not by its default unscoped view."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        entries = self.repo.getPlaysNewestFirst(self.user, count=count, startIndex=startIndex, startTs=startTs, endTs=endTs)
        return self._paginateEntries(entries) if fullPagination else entries

    def getEntriesFromOld(self, count: int | None = None, startIndex: int = 0, fullPagination: bool = True) -> list:
        """ Return the oldest `count` entries from history, sorted from oldest to newest. If count is None, return all entries. """
        entries = self.repo.getPlaysOldestFirst(self.user, count=count, startIndex=startIndex)
        return self._paginateEntries(entries) if fullPagination else entries

    def getSkipEntriesFromOld(self, count: int | None = None, startIndex: int = 0, fullPagination: bool = True) -> list:
        """Skip events (plays.is_skip=1) oldest first, hydrated like plays - the
        JSON export's trailing section, so skips round-trip between
        instances (they re-import as sub-threshold entries)."""
        entries = self.repo.getSkipsOldestFirst(self.user, count=count, startIndex=startIndex)
        return self._paginateEntries(entries) if fullPagination else entries

    def searchEntries(self, query: str, count: int | None = None, startIndex: int = 0,
                       startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> list:
        """Entries (newest first) whose track/artist/album/playlist matches
        `query`, paginated in SQL (Repository.searchPlays) rather than
        filtering the whole history in Python. startDate/endDate: see
        getEntriesFromNew's identical param."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        entries = self.repo.searchPlays(self.user, query, limit=count, offset=startIndex, startTs=startTs, endTs=endTs)
        return self._paginateEntries(entries)

    def searchEntriesCount(self, query: str, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> int:
        """The paging counterpart to searchEntries() - total matching entries,
        for computing total page count without fetching every match."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.searchPlaysCount(self.user, query, startTs=startTs, endTs=endTs)

    def writeProgress(self, status: str, current: int = 0, total: int = 0, message: str = "", error: bool = False):
        self.repo.writeProgress(self.user, status, current, total, message, error)

    def readProgress(self) -> dict:
        progress = self.repo.readProgress(self.user)
        if progress is None:
            return {"status": "idle", "current": 0, "total": 0, "percentage": 0, "message": "", "error": False}
        return progress

    def resetProgress(self):
        self.writeProgress("idle", 0, 0, "", False)

    # A play exactly at a year boundary's midnight belongs to the NEXT year -
    # covered-year delete segments stop this far short of the boundary so it
    # only goes when its own year is covered.
    YEAR_SEGMENT_BOUNDARY_EPSILON_SECONDS = 0.001

    # ---- stats -------------------------------------------------------------------------

    @staticmethod
    def _dateRangeToTimestamps(startDate: datetime.datetime | None, endDate: datetime.datetime | None):
        startTs = startDate.timestamp() if startDate else None
        endTs = endDate.timestamp() if endDate else None
        return startTs, endTs

    def getExplicitRatio(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> dict:
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        conn = self.repo._conn()
        params = [self.user]
        rangeClause = self.repo._dateRangeClause(params, startTs, endTs, column="p.played_at")
        # Single aggregated row instead of GROUP BY t.explicit: NULL and 0
        # both mean "not explicit" and must land in the same clean count.
        query = f"""
            SELECT
                COALESCE(SUM(CASE WHEN t.explicit THEN 1 ELSE 0 END), 0) AS explicit_count,
                COALESCE(SUM(CASE WHEN t.explicit THEN 0 ELSE 1 END), 0) AS clean_count
            FROM plays p
            JOIN tracks t ON p.track_id = t.id
            WHERE p.username = ? AND p.is_skip = 0{rangeClause}
        """
        row = conn.execute(query, params).fetchone()
        return {"explicit": row["explicit_count"], "clean": row["clean_count"]}

    def getReleaseDecadeDistribution(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> dict:
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        conn = self.repo._conn()
        # Decades computed fully in SQL. Release dates are stored as
        # midnight-UTC timestamps of a calendar date, so the year is read
        # back in UTC too - applying the app timezone here (as the Python
        # loop this replaced did) shifted every Jan 1 release into the
        # previous year whenever the offset was negative. HAVING drops the
        # NULL decade a timestamp outside strftime's supported year range
        # would produce, matching the old loop's swallow-and-skip.
        params = [self.user]
        rangeClause = self.repo._dateRangeClause(params, startTs, endTs, column="p.played_at")
        query = f"""
            SELECT (CAST(strftime('%Y', al.release_date, 'unixepoch') AS INTEGER) / 10) * 10 AS decade,
                   COUNT(*) AS count
            FROM plays p
            JOIN tracks t ON p.track_id = t.id
            JOIN albums al ON t.album_id = al.id
            WHERE p.username = ? AND p.is_skip = 0{rangeClause}
              AND al.release_date IS NOT NULL
              AND al.release_date != 0
            GROUP BY decade
            HAVING decade IS NOT NULL
            ORDER BY decade
        """
        rows = conn.execute(query, params).fetchall()
        return {f"{row['decade']}s": row["count"] for row in rows}

    def _resolveIncludeInherited(self, includeInherited: bool | None) -> int:
        """None means "whatever the admin's instance-wide toggle says" - the
        default for every genre stat, so flipping the toggle changes charts/
        wrapped/compare/coverage everywhere without touching callers."""
        if includeInherited is None:
            includeInherited = self.repo.isInheritedGenresEnabled()
        return 1 if includeInherited else 0

    def getGenreCoverage(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                         includeInherited: bool | None = None) -> dict:
        """Play-weighted Last.fm genre coverage over a date range: for each
        category, the share of this user's plays whose song/album/primary
        artist carries at least one genre row. All three categories share the
        same denominator (every play has exactly one track, album and primary
        artist - a play whose track lacks a position-0 artist row just never
        counts as artist-covered). "overall" is the mean of the three - the
        unlock gate for genre features compares against it. Each category
        also reports "ownPercent": the share covered by own (non-inherited)
        tags regardless of the toggle, so the coverage panel can show how
        much of the number rests on inheritance (equal to percent for
        artists, which have no inherited concept)."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        conn = self.repo._conn()
        # Play-weighted coverage as set membership: materialize each "has a
        # (filtered) genre" id set once, then probe this user's distinct played
        # tracks against them. The previous form fired 5 correlated EXISTS
        # subqueries per distinct track (~87k index probes on a large library);
        # this scans the three small genre tables once and joins, ~48% faster
        # for identical output. tracks is LEFT-joined so a play whose track row
        # is missing still counts toward the denominator (it just can't be
        # album/artist covered), matching the old plays-only outer scan.
        params = [self.user]
        rangeClause = self.repo._dateRangeClause(params, startTs, endTs, column="played_at")
        params.extend([inherited, inherited])
        query = f"""
            WITH played AS (
                SELECT track_id, COUNT(*) AS cnt
                FROM plays
                WHERE username = ? AND is_skip = 0{rangeClause}
                GROUP BY track_id
            ),
            covered_tracks  AS (SELECT DISTINCT track_id  FROM track_genres WHERE (? OR inherited = 0)),
            own_tracks      AS (SELECT DISTINCT track_id  FROM track_genres WHERE inherited = 0),
            covered_albums  AS (SELECT DISTINCT album_id  FROM album_genres WHERE (? OR inherited = 0)),
            own_albums      AS (SELECT DISTINCT album_id  FROM album_genres WHERE inherited = 0),
            covered_artists AS (SELECT DISTINCT artist_id FROM artist_genres)
            SELECT
                COALESCE(SUM(p.cnt), 0) AS total,
                COALESCE(SUM(CASE WHEN ct.track_id   IS NOT NULL THEN p.cnt ELSE 0 END), 0) AS song_covered,
                COALESCE(SUM(CASE WHEN ca.album_id   IS NOT NULL THEN p.cnt ELSE 0 END), 0) AS album_covered,
                COALESCE(SUM(CASE WHEN car.artist_id IS NOT NULL THEN p.cnt ELSE 0 END), 0) AS artist_covered,
                COALESCE(SUM(CASE WHEN ot.track_id   IS NOT NULL THEN p.cnt ELSE 0 END), 0) AS song_own,
                COALESCE(SUM(CASE WHEN oa.album_id   IS NOT NULL THEN p.cnt ELSE 0 END), 0) AS album_own
            FROM played p
            LEFT JOIN tracks t          ON t.id = p.track_id
            LEFT JOIN track_artists ta  ON ta.track_id = p.track_id AND ta.position = 0
            LEFT JOIN covered_tracks  ct  ON ct.track_id   = p.track_id
            LEFT JOIN own_tracks      ot  ON ot.track_id   = p.track_id
            LEFT JOIN covered_albums  ca  ON ca.album_id   = t.album_id
            LEFT JOIN own_albums      oa  ON oa.album_id   = t.album_id
            LEFT JOIN covered_artists car ON car.artist_id = ta.artist_id
        """
        row = conn.execute(query, params).fetchone()
        total = row["total"]

        def category(covered: int, ownCovered: int) -> dict:
            def percentOf(value: int) -> float:
                return round(value / total * 100, 1) if total else 0.0
            return {"covered": covered, "total": total,
                    "percent": percentOf(covered), "ownPercent": percentOf(ownCovered)}

        ownByCategory = {"song": row["song_own"], "album": row["album_own"],
                         "artist": row["artist_covered"]}
        coverage = {name: category(row[f"{name}_covered"], ownByCategory[name])
                    for name in GENRE_COVERAGE_CATEGORIES}
        coveredSum = sum(row[f"{name}_covered"] for name in GENRE_COVERAGE_CATEGORIES)
        overallPercent = (round(coveredSum / (len(GENRE_COVERAGE_CATEGORIES) * total) * 100, 1)
                          if total else 0.0)
        coverage["overall"] = {"percent": overallPercent}
        return coverage

    def getGenreDistribution(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                             limit: int | None = None, includeInherited: bool | None = None) -> dict:
        """{genre: plays} over the range, most-played first (name breaks ties -
        Last.fm counts tie constantly). A play with N genres counts once per
        genre, the standard reading for tag distributions. Track-level genres
        only: they're the finest granularity, and inherited rows already carry
        artist genres down to tag-less tracks when the toggle allows."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        conn = self.repo._conn()
        params = [inherited, self.user]
        rangeClause = self.repo._dateRangeClause(params, startTs, endTs, column="p.played_at")
        limitClause = ""
        if limit is not None:
            limitClause = " LIMIT ?"
        query = f"""
            SELECT g.genre AS genre, COUNT(*) AS plays
            FROM plays p
            JOIN track_genres g ON g.track_id = p.track_id
            WHERE (? OR g.inherited = 0) AND p.username = ? AND p.is_skip = 0{rangeClause}
            GROUP BY g.genre
            ORDER BY plays DESC, g.genre ASC{limitClause}
        """
        if limit is not None:
            params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return {row["genre"]: row["plays"] for row in rows}

    def getGenreTrends(self, genres: list[str], startDate: datetime.datetime = None,
                       endDate: datetime.datetime = None, includeInherited: bool | None = None) -> dict:
        """Plays per local-time month per genre, in the {"buckets", "series"}
        shape the multi-line trend chart consumes (same shape as getArtistTrend).
        `buckets` is the sorted union of the "%Y-%m" months in which any of the
        requested genres has a play; each series' `data` aligns to it (0 where
        that genre had no play that month). Requested-genre order is preserved;
        genres with no plays at all are dropped. Empty input or no plays ->
        {"buckets": [], "series": []}."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        rows = self.repo.getGenrePlayRows(self.user, genres, inherited, startTs, endTs)

        counts: dict = {}  # genre -> {month: count}
        months: set = set()
        for row in rows:
            month = convertToDatetime(row["played_at"], tz=self.tz).strftime("%Y-%m")
            months.add(month)
            genreMonths = counts.setdefault(row["genre"], {})
            genreMonths[month] = genreMonths.get(month, 0) + 1

        if not months:
            return {"buckets": [], "series": []}

        buckets = sorted(months)
        series = [
            {"name": genre, "data": [counts[genre].get(month, 0) for month in buckets]}
            for genre in genres if genre in counts
        ]
        return {"buckets": buckets, "series": series}

    def getGenreStats(self, genre: str, startDate: datetime.datetime = None,
                      endDate: datetime.datetime = None, includeInherited: bool | None = None) -> dict:
        """{plays, listenMs, firstPlayedTs, sharePercent} for one genre.
        sharePercent is this genre's plays as a share of all genre-tagged
        plays in range (0.0 when the user has none)."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        stats = self.repo.getGenrePlayStats(self.user, genre, inherited, startTs, endTs)
        total = self.repo.getTotalGenreTaggedPlays(self.user, inherited, startTs, endTs)
        stats["sharePercent"] = round(stats["plays"] / total * 100, 1) if total else 0.0
        return stats

    def getTopArtistsForGenre(self, genre: str, limit: int, startDate: datetime.datetime = None,
                              endDate: datetime.datetime = None, includeInherited: bool | None = None) -> list[dict]:
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        return self.repo.getTopArtistsForGenre(self.user, genre, inherited, limit, startTs, endTs)

    def getTopTracksForGenre(self, genre: str, limit: int, startDate: datetime.datetime = None,
                             endDate: datetime.datetime = None, includeInherited: bool | None = None) -> list[dict]:
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        return self.repo.getTopTracksForGenre(self.user, genre, inherited, limit, startTs, endTs)

    def getGenreArtistCounts(self, genres: list[str], includeInherited: bool | None = None) -> dict:
        """{genre: distinct artist count} for the given genres - the breadth
        view that pairs with the play-weighted share donut on the Genres page."""
        inherited = self._resolveIncludeInherited(includeInherited)
        return self.repo.getArtistCountsByGenres(self.user, genres, inherited)

    def getGenreHourOfDayHeatmap(self, genre: str, startDate: datetime.datetime = None,
                                 endDate: datetime.datetime = None, includeInherited: bool | None = None) -> list:
        """7x24 grid (Monday=0..Sunday=6 x hour 0-23) of listening time/plays
        for one genre - the per-genre "listening clock". Genre-scoped analogue
        of getHourOfDayHeatmap; same local weekday/hour mapping in Python."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        rows = self.repo.getGenreBucketedPlayTotals(self.user, genre, inherited, startTs, endTs)
        grid = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        for row in rows:
            date = convertToDatetime(row["bucketStartTs"], tz=self.tz)
            cell = grid[date.weekday()][date.hour]
            cell["totalTimeListened"] += row["totalTimeListened"]
            cell["plays"] += row["plays"]
        return grid

    def getRecommendedArtists(self, limit: int, genrePool: int, excludeTopN: int) -> list[dict]:
        """"Discover" recommendations: under-played artists already in the
        user's library whose genres overlap the user's top `genrePool` genres,
        excluding the user's `excludeTopN` most-played artists. Returns [] when
        the user has no genre data at all. Callers gate this behind the genre
        coverage unlock (see genre_gate) so it only surfaces once the tag data
        is dense enough to be meaningful."""
        topGenres = list(self.getGenreDistribution(limit=genrePool).keys())
        if not topGenres:
            return []
        topArtistIds = [artist["id"] for artist in self.getTopArtists(by="plays", limit=excludeTopN)]
        return self.repo.getArtistsByGenres(self.user, topGenres, topArtistIds, limit)

    def getGenresForTrack(self, trackId: str, includeInherited: bool | None = None) -> list[str]:
        """This track's own genre names, position-ordered - the track-card
        badge's data source. Respects the same inherited-genre toggle as
        every other genre stat (None = read the admin's instance-wide
        setting)."""
        inherited = self._resolveIncludeInherited(includeInherited)
        return [row["genre"] for row in self.repo.getTrackGenres(trackId)
                if inherited or not row["inherited"]]

    def getGenresForAlbum(self, albumId: str, includeInherited: bool | None = None) -> list[str]:
        """This album's own genre names, position-ordered - see
        getGenresForTrack."""
        inherited = self._resolveIncludeInherited(includeInherited)
        return [row["genre"] for row in self.repo.getAlbumGenres(albumId)
                if inherited or not row["inherited"]]

    def getGenresForArtist(self, artistId: str) -> list[str]:
        """This artist's own genre names, position-ordered. Artists have no
        inherited concept (nothing to inherit FROM), so no toggle here."""
        return self.repo.getArtistGenres(artistId)

    def getCompletionStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> dict:
        """Skip/complete/partial breakdown for the Charts pie chart and the
        Compare page's Skip Rate. A skip is any play with is_skip=1 - the single
        admin-tunable skip threshold, materialized per row (it replaced both the
        old 30s line and the separate play_skips table). Among real plays
        (is_skip=0), a listen at/over the admin-set complete percent of the
        track's duration counts as complete (unknown <=0 durations count as
        complete since partial can't be told apart), else partial."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        conn = self.repo._conn()
        # Fully classified in SQL - one aggregate row instead of a row per
        # distinct (time_played, duration) pair.
        ratio = self.repo.getCompletionCompletePercent() / 100.0
        params = [ratio, ratio, self.user]
        rangeClause = self.repo._dateRangeClause(params, startTs, endTs, column="p.played_at")
        query = f"""
            SELECT
                COALESCE(SUM(CASE WHEN p.is_skip = 1 THEN 1 ELSE 0 END), 0) AS skips,
                COALESCE(SUM(CASE WHEN p.is_skip = 0
                                   AND (t.duration_ms <= 0 OR p.time_played >= t.duration_ms * ?)
                                  THEN 1 ELSE 0 END), 0) AS completes,
                COALESCE(SUM(CASE WHEN p.is_skip = 0
                                   AND t.duration_ms > 0 AND p.time_played < t.duration_ms * ?
                                  THEN 1 ELSE 0 END), 0) AS partials
            FROM plays p
            JOIN tracks t ON p.track_id = t.id
            WHERE p.username = ?{rangeClause}
        """
        row = conn.execute(query, params).fetchone()
        return {"skips": row["skips"], "completes": row["completes"], "partials": row["partials"]}

    def getSongsStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                       sortBy: str = "plays", limit: int | None = None, offset: int = 0,
                       trackId: str | None = None, artistId: str | None = None,
                       albumId: str | None = None, searchQuery: str | None = None) -> list:
        """Return songs sorted by `sortBy` with full song metadata and listen
        totals - sorted/paged in SQL via a single batched query (see
        Repository.getSongsPage) rather than hydrating every song ever played
        just to discard all but the requested page. `trackId`/`artistId`/
        `albumId` narrow this to a single song's stats, an artist's songs, or an
        album's songs (see Repository.getSongsPage). `searchQuery` narrows to
        songs whose name, artist(s), or album match."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getSongsPage(self.user, startTs, endTs, sortBy=sortBy, limit=limit, offset=offset,
                                       trackId=trackId, artistId=artistId, albumId=albumId, searchQuery=searchQuery)

    def getSong(self, trackId: str) -> dict | None:
        """A single song's full metadata plus all-time listen totals - the
        song-detail page's lookup."""
        results = self.getSongsStats(sortBy="plays", limit=1, trackId=trackId)
        return results[0] if results else None

    def getPlayedTrackIds(self, trackIds: list[str]) -> set[str]:
        """The subset of `trackIds` this user has at least one play of - see
        Repository.getPlayedTrackIds."""
        return self.repo.getPlayedTrackIds(self.user, trackIds)

    def getSongsCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                       searchQuery: str | None = None) -> int:
        """Number of distinct songs played in range - the paging counterpart to
        getSongsStats(), for computing total page count without fetching every
        song's metadata."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getSongsCount(self.user, startTs, endTs, searchQuery=searchQuery)

    def getPlayTotals(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> tuple[int, int]:
        """(play count, total time listened) across the whole range - cheap
        aggregate that doesn't require fetching per-song metadata."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getPlayTotals(self.user, startTs, endTs)

    def _getPlayDateSet(self, startTs: float | None, endTs: float | None) -> set[str]:
        """Distinct local ("%Y-%m-%d") dates on which this user has any play in
        [startTs, endTs). Works off SQL-side buckets (getBucketedPlayTotals) -
        a bucket's start shares its local date with every play inside it, so the
        set is identical to a per-play scan's. Shared by the longest-streak and
        current-streak calculations."""
        rows = self.repo.getBucketedPlayTotals(self.user, startTs, endTs)
        return {
            convertToDatetime(r["bucketStartTs"], tz=self.tz).strftime("%Y-%m-%d")
            for r in rows
        }

    def getCurrentStreak(self, now: datetime.datetime = None) -> dict:
        """The user's ongoing consecutive-days listening streak as of `now`
        (defaults to the current local time). Returns {"days", "activeToday"}:
        - activeToday is True when there's already a play logged today.
        - The streak stays "alive" (days > 0) if the most recent play was
          today OR yesterday - a day with no play yet doesn't break it until it
          ends. Two or more empty days since the last play -> days = 0.
        Only scans the last CURRENT_STREAK_LOOKBACK_DAYS (any live streak is far
        shorter, and this keeps the bucket query bounded)."""
        nowLocal = now.astimezone(self.tz) if now is not None else datetime.datetime.now(tz=self.tz)
        today = nowLocal.date()
        startTs = (nowLocal - datetime.timedelta(days=CURRENT_STREAK_LOOKBACK_DAYS)).timestamp()
        play_dates = self._getPlayDateSet(startTs, None)

        yesterday = today - datetime.timedelta(days=1)
        todayStr = today.strftime("%Y-%m-%d")
        if todayStr in play_dates:
            anchor, activeToday = today, True
        elif yesterday.strftime("%Y-%m-%d") in play_dates:
            anchor, activeToday = yesterday, False
        else:
            return {"days": 0, "activeToday": False}

        days = 0
        cursor = anchor
        while cursor.strftime("%Y-%m-%d") in play_dates:
            days += 1
            cursor -= datetime.timedelta(days=1)
        return {"days": days, "activeToday": activeToday}

    def getListeningCalendar(self, now: datetime.datetime = None,
                             weeks: int = CALENDAR_WEEKS) -> dict:
        """Per-day listening grid for the dashboard's streak calendar: the last
        `weeks` week-columns of daily play counts, each day shaded by volume
        relative to the window's busiest day - reinforcing the getCurrentStreak
        card shown beside it. `now` (defaults to the current local time) fixes
        'today', mirroring getCurrentStreak. Reuses getBucketedPlayTotals and
        the same bucket->local-date mapping as _getPlayDateSet, folding the
        15-minute buckets up to per-day counts, then hands off to
        buildListeningCalendar for the pure grid layout."""
        nowLocal = now.astimezone(self.tz) if now is not None else datetime.datetime.now(tz=self.tz)
        today = nowLocal.date()
        lastMonday = today - datetime.timedelta(days=today.weekday())
        firstMonday = lastMonday - datetime.timedelta(weeks=weeks - 1)
        startTs = datetime.datetime(firstMonday.year, firstMonday.month, firstMonday.day,
                                    tzinfo=self.tz).timestamp()

        dayCounts: dict = {}
        for row in self.repo.getBucketedPlayTotals(self.user, startTs, None):
            dateStr = convertToDatetime(row["bucketStartTs"], tz=self.tz).strftime("%Y-%m-%d")
            dayCounts[dateStr] = dayCounts.get(dateStr, 0) + row["plays"]
        return buildListeningCalendar(dayCounts, today, weeks=weeks)

    def getOnThisDay(self, now: datetime.datetime = None, limit: int | None = None) -> list[dict]:
        """"On this day" resurfacing: for each PRIOR year that has plays on
        today's local month/day, the track played most that day. Returns
        [{year, yearsAgo, trackId, trackName, artistName, playCount}], newest
        year first, capped at `limit` (None = uncapped). The repo over-selects
        a ±1-day UTC window; the exact local month/day match is applied here so
        it's correct across timezone offsets and DST."""
        nowLocal = now.astimezone(self.tz) if now is not None else datetime.datetime.now(tz=self.tz)
        today = nowLocal.date()
        monthDays = sorted({
            (today + datetime.timedelta(days=offset)).strftime("%m-%d")
            for offset in (-1, 0, 1)
        })
        rows = self.repo.getPlaysForMonthDays(self.user, monthDays)

        perYearTrack: dict = {}
        for r in rows:
            localDt = convertToDatetime(r["played_at"], tz=self.tz)
            if (localDt.month, localDt.day) != (today.month, today.day):
                continue
            if localDt.year == today.year:
                continue
            key = (localDt.year, r["track_id"])
            agg = perYearTrack.get(key)
            if agg is None:
                perYearTrack[key] = {"count": 1, "trackName": r["track_name"],
                                     "artistName": r["artist_name"]}
            else:
                agg["count"] += 1

        def sortKey(entry: dict) -> tuple:
            # Highest play count wins; track name (then id) breaks ties so the
            # pick is deterministic.
            return (-entry["playCount"], entry["trackName"] or "", entry["trackId"])

        bestByYear: dict = {}
        for (year, trackId), agg in perYearTrack.items():
            entry = {"year": year, "trackId": trackId, "trackName": agg["trackName"],
                     "artistName": agg["artistName"], "playCount": agg["count"]}
            current = bestByYear.get(year)
            if current is None or sortKey(entry) < sortKey(current):
                bestByYear[year] = entry

        result = sorted(bestByYear.values(), key=lambda e: e["year"], reverse=True)
        if limit is not None:
            result = result[:limit]
        for entry in result:
            entry["yearsAgo"] = today.year - entry["year"]
        return result

    def getLongestStreak(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> int:
        """Longest consecutive days of plays in range. See _getPlayDateSet for
        why SQL-side buckets give the same distinct-dates set as a per-play
        scan."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        play_dates = sorted(self._getPlayDateSet(startTs, endTs))
        if not play_dates:
            return 0

        max_streak = 1
        current_streak = 1
        prev_date = None

        for current_date in play_dates:
            if prev_date:
                # Check if dates are consecutive (1 day apart)
                prev_obj = datetime.datetime.strptime(prev_date, "%Y-%m-%d")
                curr_obj = datetime.datetime.strptime(current_date, "%Y-%m-%d")
                if (curr_obj - prev_obj).days == 1:
                    current_streak += 1
                else:
                    max_streak = max(max_streak, current_streak)
                    current_streak = 1
            prev_date = current_date

        max_streak = max(max_streak, current_streak)
        return max_streak

    def getPeakListeningTime(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> tuple[str, int] | None:
        """(day_of_week_name, play_count) for the day with most plays, or None.
        Counting runs in SQL (getBucketedPlayTotals); Python maps each bucket
        to its local weekday."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        rows = self.repo.getBucketedPlayTotals(self.user, startTs, endTs)
        if not rows:
            return None

        # Map Python's locale-independent weekday index to English names
        WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        counts = {}
        for row in rows:
            dt = convertToDatetime(row["bucketStartTs"], tz=self.tz)
            day_name = WEEKDAYS[dt.weekday()]
            counts[day_name] = counts.get(day_name, 0) + row["plays"]

        peak_day = max(counts, key=counts.get)
        return peak_day, counts[peak_day]

    def getDiscoveredSongsCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> int:
        """Count of distinct songs first played within the date range."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getDiscoveredSongsCount(self.user, startTs, endTs)

    def getDiscoveredArtistsCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> int:
        """Count of distinct artists first played within the date range."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getDiscoveredArtistsCount(self.user, startTs, endTs)

    def getAlbumsStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                        sortBy: str = "plays", limit: int | None = None, offset: int = 0,
                        albumId: str | None = None, searchQuery: str | None = None) -> list:
        """Return albums sorted by `sortBy` with aggregated listen totals - sorted/
        paged in SQL via a single batched query (see Repository.getAlbumsPage),
        mirroring getSongsStats(). `albumId` narrows this to a single album's
        stats. `searchQuery` narrows to albums whose name or artist(s) match."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getAlbumsPage(self.user, startTs, endTs, sortBy=sortBy, limit=limit, offset=offset,
                                        albumId=albumId, searchQuery=searchQuery)

    def getAlbum(self, albumId: str) -> dict | None:
        """A single album's aggregate stats - the album-detail page's lookup."""
        results = self.getAlbumsStats(sortBy="plays", limit=1, albumId=albumId)
        return results[0] if results else None

    def getAlbumBio(self, albumId: str) -> str | None:
        """This album's stored biography (see lazyFetchAlbumBio), or None if
        it's never been fetched or Last.fm has nothing usable - mirrors
        getArtistBio."""
        return self.repo.getAlbumBioState(albumId)["bio"]

    def getPlayedAlbumIds(self, albumIds: list[str]) -> set[str]:
        """The subset of `albumIds` this user has at least one play from - see
        Repository.getPlayedAlbumIds."""
        return self.repo.getPlayedAlbumIds(self.user, albumIds)

    def getAlbumsCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                        searchQuery: str | None = None) -> int:
        """Number of distinct albums played in range - the paging counterpart to
        getAlbumsStats(), for computing total page count without fetching every
        album's metadata."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getAlbumsCount(self.user, startTs, endTs, searchQuery=searchQuery)

    def getTopAlbums(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, by: str = "plays",
                      limit: int | None = None, offset: int = 0, searchQuery: str | None = None) -> list:
        # Albums are sorted/paged in SQL (see getAlbumsStats -> Repository.getAlbumsPage)
        # rather than re-sorted here in Python, for the same reason getTopSongs is.
        return self.getAlbumsStats(startDate, endDate, sortBy=by, limit=limit, offset=offset, searchQuery=searchQuery)

    def getArtistsStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                         artistId: str | None = None, sortBy: str = "plays", limit: int | None = None,
                         offset: int = 0, searchQuery: str | None = None) -> list:
        """Return artists sorted by `sortBy` with aggregated data and listen
        totals - sorted/paged in SQL via a single batched query (see
        Repository.getArtistAggregates) rather than fetching every artist and
        sorting/paging in Python, mirroring getSongsStats()/getAlbumsStats().
        `artistId` narrows this to a single artist's stats; `searchQuery`
        narrows to artists whose name matches."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getArtistAggregates(self.user, startTs, endTs, artistId=artistId, sortBy=sortBy,
                                              limit=limit, offset=offset, searchQuery=searchQuery)

    def getArtist(self, artistId: str, startDate: datetime.datetime = None,
                  endDate: datetime.datetime = None) -> dict | None:
        """A single artist's aggregate stats - the artist-detail page's lookup."""
        results = self.getArtistsStats(startDate, endDate, artistId=artistId, limit=1)
        return results[0] if results else None

    def getArtistBio(self, artistId: str) -> str | None:
        """This artist's stored biography (see lazyFetchArtistBio), or None
        if it's never been fetched or Last.fm has nothing usable. Kept
        separate from getArtist()/getArtistsStats() rather than added to
        that aggregate query - bio text has no place on list pages (Top
        Artists), only the detail page needs it."""
        return self.repo.getArtistBioState(artistId)["bio"]

    def getPlayedArtistIds(self, artistIds: list[str]) -> set[str]:
        """The subset of `artistIds` this user has at least one play of a
        track crediting - see Repository.getPlayedArtistIds."""
        return self.repo.getPlayedArtistIds(self.user, artistIds)

    def getArtistsCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                         searchQuery: str | None = None) -> int:
        """Number of distinct artists played in range - the paging counterpart
        to getArtistsStats(), for computing total page count without fetching
        every artist's metadata."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getArtistsCount(self.user, startTs, endTs, searchQuery=searchQuery)

    def getArtistTotals(self, startDate: datetime.datetime = None,
                         endDate: datetime.datetime = None) -> tuple[int, int, int]:
        """(total plays, total unique songs, total time listened) summed across
        every artist in range - the Top Artists page's "(top list)" totals,
        computed directly in SQL instead of fetching every artist and summing
        in Python."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getArtistTotals(self.user, startTs, endTs)

    def getOverallStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> list:
        """Return songs sorted by play count with full song metadata and listen totals."""
        previousSongsPlayed, previousDurationMs = 0, 0
        if startDate and endDate:
            duration = endDate - startDate
            previousStart = startDate - duration
            previousEnd = startDate
            prevStartTs, prevEndTs = self._dateRangeToTimestamps(previousStart, previousEnd)
            previousSongsPlayed, previousDurationMs = self.repo.getPlayTotals(self.user, prevStartTs, prevEndTs)

        # totalSongsPlayed/totalDurationMs are computed via a dedicated COUNT/SUM
        # query rather than by summing every song's stats: each play belongs to
        # exactly one song, so sum(plays-per-song) == total play count over the
        # same range - identical math, without hydrating every song just to add
        # its numbers up. currentTopSongs only needs the single top row.
        totalSongsPlayed, totalDurationMs = self.getPlayTotals(startDate, endDate)
        currentTopSongs = self.getTopSongs(startDate=startDate, endDate=endDate, by="plays", limit=1)
        currentTopArtists = self.getTopArtists(startDate=startDate, endDate=endDate, by="totalTimeListened", limit=1)

        return {"currentTopSongs": currentTopSongs,
                "currentTopArtists": currentTopArtists,
                "totalSongsPlayed": totalSongsPlayed,
                "totalDurationMs": totalDurationMs,
                "previousSongsPlayed": previousSongsPlayed,
                "previousDurationMs": previousDurationMs
                }

    def getTopSongs(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, by: str = "plays",
                     limit: int | None = None, offset: int = 0, searchQuery: str | None = None) -> list:
        # Songs are sorted/paged in SQL (see getSongsStats -> Repository.getSongsPage)
        # rather than re-sorted here in Python: once pagination is pushed down to
        # the database, re-sorting an already-LIMIT-ed page can't reconstruct
        # global rank, so SQL ordering must be the single source of truth.
        return self.getSongsStats(startDate, endDate, sortBy=by, limit=limit, offset=offset, searchQuery=searchQuery)

    def getTopArtists(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, by: str = "plays",
                       limit: int | None = None, offset: int = 0, searchQuery: str | None = None) -> list:
        # Artists are sorted/paged in SQL (see getArtistsStats -> Repository.getArtistAggregates)
        # rather than re-sorted here in Python, for the same reason getTopSongs is.
        return self.getArtistsStats(startDate, endDate, sortBy=by, limit=limit, offset=offset, searchQuery=searchQuery)

    def _bucketKey(self, date: datetime.datetime, groupBy: str) -> str:
        if groupBy == "week":
            return dateToString(startOfWeek(date))
        elif groupBy == "hour":
            return date.strftime("%Y-%m-%d %H:00")
        elif groupBy == "month":
            return date.strftime("%Y-%m")
        else:
            return dateToString(startOfDay(date))

    def getListeningTimeSeries(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                                groupBy: str = "day", trackId: str | None = None, artistId: str | None = None,
                                albumId: str | None = None) -> list:
        """Total listening time and play count per day or week, gap-filled with
        zero-value buckets so a bar chart shows a continuous timeline.
        `trackId`/`artistId`/`albumId` narrow this to one item's plays only -
        reused as-is by the song/artist/album detail pages' play-history chart
        (same output shape, so the frontend's existing renderTimeSeriesChart
        needs no changes).

        The per-play summing happens in SQL (see getBucketedPlayTotals);
        Python only re-buckets the pre-aggregated 15-minute rows into the
        app's configurable IANA timezone, which SQLite can't express."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        rows = self.repo.getBucketedPlayTotals(self.user, startTs, endTs, trackId=trackId, artistId=artistId,
                                                albumId=albumId)

        buckets = {}
        for row in rows:
            date = convertToDatetime(row["bucketStartTs"], tz=self.tz)
            key = self._bucketKey(date, groupBy)
            bucket = buckets.setdefault(key, {"label": key, "totalTimeListened": 0, "plays": 0})
            bucket["totalTimeListened"] += row["totalTimeListened"]
            bucket["plays"] += row["plays"]

        if startDate is not None and endDate is not None:
            rangeStart, rangeEnd = startDate, endDate
        elif rows:
            # rows are ordered by bucket start; the first/last bucket start in
            # local time bounds the same chart buckets the raw plays would.
            rangeStart = convertToDatetime(rows[0]["bucketStartTs"], tz=self.tz)
            rangeEnd = convertToDatetime(rows[-1]["bucketStartTs"], tz=self.tz) + datetime.timedelta(seconds=1)
        else:
            return []

        if groupBy == "week":
            cursor = startOfWeek(rangeStart)
            advance = lambda d: d + datetime.timedelta(days=7)
        elif groupBy == "hour":
            cursor = rangeStart.replace(minute=0, second=0, microsecond=0)
            advance = lambda d: d + datetime.timedelta(hours=1)
        elif groupBy == "month":
            # A fixed timedelta step doesn't work here since months vary in
            # length - advance to the 1st of the next calendar month instead.
            cursor = startOfMonth(rangeStart)
            advance = lambda d: d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
        else:
            cursor = startOfDay(rangeStart)
            advance = lambda d: d + datetime.timedelta(days=1)

        result = []
        while cursor < rangeEnd:
            key = self._bucketKey(cursor, groupBy)
            result.append(buckets.get(key, {"label": key, "totalTimeListened": 0, "plays": 0}))
            cursor = advance(cursor)
        return result

    def getHourOfDayHeatmap(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                             trackId: str | None = None, artistId: str | None = None,
                             albumId: str | None = None) -> list:
        """7x24 grid (rows Monday=0..Sunday=6, columns hour-of-day 0-23) of total
        listening time and play count, for a 'when do I listen' heatmap.
        `trackId`/`artistId`/`albumId` narrow this to one item's plays only -
        reused by the song detail page's 'when you listen to this song' heatmap,
        same as getListeningTimeSeries's item filters. Summing runs in SQL
        (getBucketedPlayTotals); Python maps each 15-minute bucket to its
        local weekday/hour cell."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        rows = self.repo.getBucketedPlayTotals(self.user, startTs, endTs, trackId=trackId, artistId=artistId,
                                                albumId=albumId)
        grid = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        for row in rows:
            date = convertToDatetime(row["bucketStartTs"], tz=self.tz)
            cell = grid[date.weekday()][date.hour]
            cell["totalTimeListened"] += row["totalTimeListened"]
            cell["plays"] += row["plays"]

        return grid

    def getArtistTrend(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, topN: int = 5, groupBy: str = "week") -> dict:
        """Per-bucket play counts for the topN most-played artists in the range, for
        an 'artist trend over time' line chart. Buckets are only the ones that have
        any activity - unlike getListeningTimeSeries, a trend line doesn't need a
        gap-filled timeline the way a bar chart's x-axis does."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        # Per-(bucket, artist) counts pre-summed in SQL; Python only re-maps
        # the 15-minute buckets into the chart's local-timezone buckets.
        rows = self.repo.getBucketedArtistPlayCounts(self.user, startTs, endTs)

        totalPlaysByArtist = {}
        idPlaysByArtist = {}   #< {name: {artistId: totalPlays}} - picks a click-through target below
        bucketedCounts = []
        # Many rows share the same 15-minute bucketStartTs (one per artist
        # active in it), so the local-timezone conversion + bucket-key mapping
        # is memoized per distinct bucket rather than recomputed per row -
        # ~77k rows collapse to ~21k conversions on a large library.
        bucketKeyCache: dict = {}
        for row in rows:
            bucketStartTs = row["bucketStartTs"]
            key = bucketKeyCache.get(bucketStartTs)
            if key is None:
                key = self._bucketKey(convertToDatetime(bucketStartTs, tz=self.tz), groupBy)
                bucketKeyCache[bucketStartTs] = key
            name = row["artistName"]
            bucketedCounts.append((key, name, row["plays"]))
            totalPlaysByArtist[name] = totalPlaysByArtist.get(name, 0) + row["plays"]
            idCounts = idPlaysByArtist.setdefault(name, {})
            idCounts[row["artistId"]] = idCounts.get(row["artistId"], 0) + row["plays"]

        if not totalPlaysByArtist:
            return {"buckets": [], "series": []}

        topNames = [name for name, _ in sorted(totalPlaysByArtist.items(), key=lambda kv: kv[1], reverse=True)[:topN]]

        bucketKeys = sorted({key for key, _, _ in bucketedCounts})
        seriesData = {name: {key: 0 for key in bucketKeys} for name in topNames}
        for key, name, plays in bucketedCounts:
            if name in seriesData:
                seriesData[name][key] += plays

        # Two different artist ids sharing a display name still merge into
        # one line (by design - see getBucketedArtistPlayCounts): the id
        # that contributed the most plays under that name represents the
        # whole line for click-through, ties broken by id so the pick is
        # deterministic rather than depending on incidental row order.
        series = []
        for name in topNames:
            representativeId = min(idPlaysByArtist[name].items(), key=lambda kv: (-kv[1], kv[0]))[0]
            series.append({
                "name": name,
                "id": representativeId,
                "data": [seriesData[name][key] for key in bucketKeys],
            })
        return {"buckets": bucketKeys, "series": series}

    def getUserSpotifyCredentials(self) -> dict | None:
        return self.repo.getUserSpotifyCredentials(self.user)

    def updateUserSpotifyCredentials(self, clientId: str | None, clientSecret: str | None, refreshToken: str | None) -> None:
        self.repo.updateUserSpotifyCredentials(self.user, clientId, clientSecret, refreshToken)

    def setSpotifyNeedsReauth(self, needsReauth: bool) -> None:
        self.repo.setSpotifyNeedsReauth(self.user, needsReauth)

    def getUserLastfmApiKey(self) -> str | None:
        return self.repo.getUserLastfmApiKey(self.user)

    def updateUserLastfmApiKey(self, apiKey: str | None) -> None:
        self.repo.updateUserLastfmApiKey(self.user, apiKey)
