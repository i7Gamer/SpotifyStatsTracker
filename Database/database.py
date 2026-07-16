from __future__ import annotations
import datetime
import logging
import os
import re
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
    from Database.utils import parseError, convertToDatetime, dateToString, startOfDay, startOfWeek, startOfMonth, timeToInt
except ModuleNotFoundError:
    from Formatters.spotifyClient import Client
    from Importers.StreamingHistoryImporter import Importer
    from Importers.AutoImporter import AutoImporter
    from Listeners.spotifyListener import Listener
    from repository import Repository, IMAGE_KIND_TRACK, IMAGE_KIND_ARTIST, IMAGE_STATUS_OK, IMAGE_STATUS_FAILED
    from utils import parseError, convertToDatetime, dateToString, startOfDay, startOfWeek, startOfMonth, timeToInt

logger = logging.getLogger(__name__)

TRUTHY_DEBUG_VALUES = {"1", "true"}

IMAGE_DOWNLOAD_WORKERS = 5   #< bounds total concurrent image downloads for the whole process, not per user

# getCompletionStats' play classification thresholds: under 30s counts as a
# skip (Spotify's own royalty threshold), at/over 80% of the track's duration
# counts as a completed listen, anything between is a partial.
COMPLETION_SKIP_THRESHOLD_MS = 30_000
COMPLETION_COMPLETE_RATIO = 0.8
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Images are shared across every user (album art / artist photos are the same
# bytes for everyone), so they live in one directory tree instead of under each
# user's own folder. Inside Data/ (see Database/db.py's DEFAULT_DB_PATH) so the
# Docker volume mount that persists the database also covers it.
MEDIA_DIR = Path(__file__).resolve().parent / "Data" / "Media"


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

    def isOwnWrite(self, trackId: str, play: dict) -> bool:
        return play["id"] in self.claimedRowIds or (trackId, play["played_at"]) in self.insertedPlayKeys


class Database:
    PROGRESS_UPDATE_INTERVAL = 10   #< Write import progress to disk every N entries instead of every entry
    RECONNECT_MAX_RETRIES = 10  #< max reconnection attempts before giving up (~30 min window with backoff)
    RECONNECT_INITIAL_DELAY = 1  #< initial backoff in seconds
    RECONNECT_MAX_DELAY = 300  #< cap backoff at 5 minutes
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

    WRAPPED_WORKER_MIN_START_DELAY = 60        #< minimum initial random startup delay in seconds
    WRAPPED_WORKER_MAX_START_DELAY = 300       #< maximum initial random startup delay in seconds
    WRAPPED_WORKER_LOOP_INTERVAL = 900         #< interval between consecutive checks in seconds (15 minutes)
    WRAPPED_YEAR_DELAY_SECONDS = 5             #< breathing room delay in seconds between recalculating years

    BACKFILLER_ALBUM_QUEUE_SIZE = 80           #< number of albums queued from DB for backfilling

    # Shared across every Database instance (every user) in this process. Image
    # download de-duplication is enforced by the `images` table (atomic across
    # threads *and* users), so a single bounded pool for the whole process is
    # enough - there's no need for one per user, and no need for the old
    # per-user in-memory id sets / metadata.json files this replaces.
    imgDir_tracks = MEDIA_DIR / "tracks"
    imgDir_artists = MEDIA_DIR / "artists"
    _imageDownloadExecutor = concurrent.futures.ThreadPoolExecutor(max_workers=IMAGE_DOWNLOAD_WORKERS)
    _active_backfills = set()
    _backfill_lock = threading.Lock()

    def __init__(self, user: str, cookiesFile: str | None = None, email: str | None = None, dbPath=None):
        if not user:
            raise ValueError("Database user must be specified and cannot be empty.")
        self.user = user
        self.cookiesFile = cookiesFile
        self.email = email
        self.listener = None
        self.baseDir = Path(__file__).resolve().parent

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

        filterKeyword = os.environ.get("IMPORT_KEYWORD", None)
        logger.info("auto import filtering by %s", filterKeyword)
        # importHistoryBatch (not importHistory): files dropped together share
        # one import run state, so a skip/replay pair straddling a file
        # boundary isn't collapsed - and a bad file doesn't abort the rest.
        self.autoImporter = AutoImporter(folderPath=self.autoImportFolderPath,
                                         importCallback=self.importHistoryBatch,
                                         pollInterval=5,
                                         keyword=filterKeyword)

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

    def refreshSettings(self) -> None:
        from zoneinfo import ZoneInfo
        import Database.utils as utils
        try:
            self.settings = self.repo.getUserSettings(self.user)
            tz_name = self.settings.get("timezone")
            self.tz = ZoneInfo(tz_name) if tz_name else utils.getTimezone()
        except Exception:
            self.tz = utils.getTimezone()

    def _addToDatabaseFromListener(self, data) -> None:
        """Record plays from the listener. Includes validation to detect cross-user
        data contamination (a bug that previously caused plays from one user to be
        recorded under another user's account)."""
        if not data:
            return
        if os.environ.get("FLASK_DEBUG"):
            source = data[0].get("_source", "unknown") if data else "unknown"
            logger.debug("_addToDatabaseFromListener called for user=%s with %d items, source=%s",
                        self.user, len(data), source)
        had_errors = False
        for item in data:
            track = item.get("track")
            timestamp = item.get("played_at")
            msPlayed = item.get("ms_played", 0)
            source = item.get("_source", "listener")



            # Reject completely unparseable or corrupt timestamps
            numeric_ts = timeToInt(timestamp)
            if numeric_ts <= 0:
                logger.warning(
                    "Skipping track %s: timestamp %s is invalid or could not be parsed.",
                    track.get("id") if track else "unknown",
                    timestamp
                )
                had_errors = True
                continue

            # Sanity check: verify the timestamp makes sense (not in far future)
            import time as time_module
            current_time = time_module.time()
            if numeric_ts > current_time + 86400:  # More than 1 day in future
                logger.error(
                    "CONTAMINATION CHECK FAILED: Track %s has timestamp %s (%.0f seconds in future). "
                    "This suggests cross-user data contamination. Skipping this play.",
                    track.get("id") if track else "unknown",
                    timestamp,
                    numeric_ts - current_time
                )
                had_errors = True
                continue

            # Sanity check: validate play duration is reasonable for a track
            # (SpotipyFree sometimes returns insane values like 7062895ms for a 171s track)
            track_duration = track.get("duration_ms", 0) if track else 0
            if track_duration > 0 and msPlayed > track_duration * 10:
                logger.warning(
                    "Skipping track %s: recorded duration %dms is %dx the track's actual duration (%dms). "
                    "Likely SpotipyFree data corruption.",
                    track.get("id") if track else "unknown",
                    msPlayed, msPlayed // max(track_duration, 1), track_duration
                )
                had_errors = True
                continue

            # Only record tracks played for at least 1 second (filter out skips/scrubs)
            if msPlayed < 1000:
                logger.debug("Skipping track %s: played only %dms (< 1s)", track.get("id") if track else "unknown", msPlayed)
                continue
            if track:
                # Per-item isolation: if the callback raised, the listener would
                # retry the whole batch forever and record nothing new until the
                # bad item aged out of the recently-played feed.
                try:
                    self.appendTrackData(timestamp, track, msPlayed, context=item.get("context", None), source=source)
                except Exception as e:
                    logger.error("Error adding track %s from listener: %s", track.get("id"), parseError(e))
                    had_errors = True
        # Mark successful poll (only if no errors occurred during processing)
        with self._health_lock:
            self.listener_last_poll_time = time.monotonic()
            if had_errors:
                self.listener_error_count += 1
                self.listener_last_error = "One or more tracks failed to add from listener"
                if self.listener_error_count > 5:
                    self.listener_health = "DEGRADED"
                    logger.warning("Listener error count exceeded threshold, marking as DEGRADED")
            else:
                self.listener_error_count = 0
                self.listener_last_error = None
                if self.listener_health != "HEALTHY":
                    self.listener_health = "HEALTHY"
                    logger.info("Listener recovered to HEALTHY state")

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

    # ---- catalog / track metadata --------------------------------------------------

    def _fetchTrackFromListener(self, trackId: str) -> dict | None:
        """Fetch and cache full metadata for a track we don't have yet, via the
        live listener client. Returns None (and logs) if the fetch fails - a play
        for an unknown track can't be recorded without its metadata, since plays
        has a foreign key to tracks."""
        if self.listener is None:
            return None
        try:
            track = Client.formatTrack(self.listener.track(trackId), embedPlaybackInfo=False)
            self.repo.upsertTrack(track, created_reason=f"listener_fetch (user: {self.user})")
            self.repo.commit()
            logger.info("Created track %s (%s) via listener fetch", trackId, track.get("name", "unknown"))
            return track
        except Exception:
            logger.error("Failed to download track %s", trackId)
            return None

    def _ensureTrackMetadata(self, trackId: str) -> dict | None:
        track = self.repo.getTrack(trackId)
        if track is not None:
            return track
        logger.info("Missing track metadata for %s, downloading it", trackId)
        return self._fetchTrackFromListener(trackId)

    @staticmethod
    def _splitEntryAndTrack(metadata: dict) -> tuple[dict, dict]:
        entry = {
            "id": metadata["id"],
            "playedAt": metadata["playedAt"],
            "timePlayed": metadata["timePlayed"],
            "playedFrom": metadata.get("playedFrom"),
        }
        track = {k: v for k, v in metadata.items() if k not in ("playedAt", "timePlayed", "playedFrom")}
        return entry, track

    @staticmethod
    def _mergeEntryWithTrack(entry: dict, track: dict) -> dict:
        meta = track.copy()
        meta["playedAt"] = entry["playedAt"]
        meta["timePlayed"] = entry["timePlayed"]
        meta["playedFrom"] = entry.get("playedFrom")
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

    def playlistName(self, playlistUri: str | None) -> str | None:
        """Return the playlist name for a Spotify playlist URI or id, caching it on first lookup."""
        if not playlistUri:
            return None
        contextType, playlistId = playlistUri.split(":", 1)
        return self.repo.getPlaylistName(playlistId, contextType)

    def updatePlaylists(self, playlist: str | None) -> None:
        if playlist is None:
            return
        contextType, playlistId = playlist.split(":", 1)
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

    def getEntriesCount(self) -> int:
        """Return total number of entries in the database."""
        return self.repo.getPlaysCount(self.user)

    def getEntriesFromNew(self, count: int | None = None, startIndex: int = 0, fullPagination: bool = True) -> list:
        """ Return the latest `count` entries from history, sorted from newest to oldest. If count is None, return all entries. """
        entries = self.repo.getPlaysNewestFirst(self.user, count=count, startIndex=startIndex)
        return self._paginateEntries(entries) if fullPagination else entries

    def getEntriesFromOld(self, count: int | None = None, startIndex: int = 0, fullPagination: bool = True) -> list:
        """ Return the oldest `count` entries from history, sorted from oldest to newest. If count is None, return all entries. """
        entries = self.repo.getPlaysOldestFirst(self.user, count=count, startIndex=startIndex)
        return self._paginateEntries(entries) if fullPagination else entries

    def searchEntries(self, query: str, count: int | None = None, startIndex: int = 0) -> list:
        """Entries (newest first) whose track/artist/album/playlist matches
        `query`, paginated in SQL (Repository.searchPlays) rather than
        filtering the whole history in Python."""
        entries = self.repo.searchPlays(self.user, query, limit=count, offset=startIndex)
        return self._paginateEntries(entries)

    def searchEntriesCount(self, query: str) -> int:
        """The paging counterpart to searchEntries() - total matching entries,
        for computing total page count without fetching every match."""
        return self.repo.searchPlaysCount(self.user, query)

    def writeProgress(self, status: str, current: int = 0, total: int = 0, message: str = "", error: bool = False):
        self.repo.writeProgress(self.user, status, current, total, message, error)

    def readProgress(self) -> dict:
        progress = self.repo.readProgress(self.user)
        if progress is None:
            return {"status": "idle", "current": 0, "total": 0, "percentage": 0, "message": "", "error": False}
        return progress

    def resetProgress(self):
        self.writeProgress("idle", 0, 0, "", False)

    def _downloadImageTask(self, path: Path, url: str, imgId: str, kind: str):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            img = Image.open(BytesIO(response.content))
            # Always store as JPEG: the templates hardcode `<imgId>.jpeg`, so an
            # image saved under its source format (e.g. .png) would 404 forever.
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")   #< JPEG can't store alpha/palette modes
            path.mkdir(parents=True, exist_ok=True)
            img.save(path / f"{imgId}.jpeg", format="JPEG")
            self.repo.markImageStatus(imgId, kind, IMAGE_STATUS_OK)
        except Exception as e:
            self.repo.markImageStatus(imgId, kind, IMAGE_STATUS_FAILED)
            if isinstance(e, requests.exceptions.RequestException):
                logger.error("Error fetching image from %s (id=%s): %s", url, imgId, parseError(e))
            else:
                logger.error("Error saving image (id=%s): %s", imgId, parseError(e))

    def _saveImg(self, path: Path, url: str, imgId: str, kind: str):
        if not url:
            return  #< Spotify occasionally returns tracks with no album images; skip silently
        # Atomically claim the download: returns False if this image is already
        # downloaded or another thread/user already claimed it - shared across the
        # whole process (and would even be safe across separate processes, unlike
        # the old per-instance in-memory id sets).
        if not self.repo.tryClaimImageDownload(imgId, kind):
            return
        self._imageDownloadExecutor.submit(self._downloadImageTask, path, url, imgId, kind)

    def saveTrackImg(self, url: str, imgId: str):
        self._saveImg(self.imgDir_tracks, url, imgId, kind=IMAGE_KIND_TRACK)

    def _lazyFetchArtistImageTask(self, artistId: str, imagePath: Path) -> bool:
        try:
            headers = {"User-Agent": USER_AGENT}
            res = requests.get(f"https://open.spotify.com/artist/{artistId}", headers=headers, timeout=5)
            match = re.search(r'<meta property="og:image" content="([^"]+)"', res.text)
            if not match:
                self.repo.markImageStatus(artistId, IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED)
                return False
            imgData = requests.get(match.group(1), headers=headers, timeout=5).content
            imagePath.parent.mkdir(parents=True, exist_ok=True)
            imagePath.write_bytes(imgData)
            self.repo.markImageStatus(artistId, IMAGE_KIND_ARTIST, IMAGE_STATUS_OK)
            return True
        except Exception as e:
            logger.error("Failed to lazy load artist image for %s: %s", artistId, parseError(e))
            self.repo.markImageStatus(artistId, IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED)
            return False

    def lazyFetchArtistImage(self, artistId: str, imagePath: Path):
        """Best-effort fetch of an artist's image scraped from their public
        Spotify page, used as a fallback for artists we never received image
        metadata for from the API. Deduplicated per artist id via the database's
        image status table so failed fetches persist across app restarts.

        The actual fetch runs on the shared image-download executor (like
        saveTrackImg()/saveArtistImg()) instead of inline, so a request for a
        still-missing image doesn't block the request thread on up to two
        sequential network calls. Returns True if the image is already on
        disk (nothing to do); otherwise returns the submitted Future for a
        freshly kicked-off fetch (the HTTP route that calls this doesn't wait
        on it - it just serves whatever's on disk right now, same as the
        other image types - callers that do need to wait, e.g. tests, can
        call .result() on it), or False if there's nothing to fetch (no
        artistId, or a fetch for this id already succeeded/failed)."""
        if imagePath.exists():
            return True
        if not artistId:
            return False

        status = self.repo.imageStatus(artistId, IMAGE_KIND_ARTIST)
        if status == IMAGE_STATUS_OK:
            return imagePath.exists()
        if status == IMAGE_STATUS_FAILED:
            return False

        if self.repo.tryClaimImageDownload(artistId, IMAGE_KIND_ARTIST):
            return self._imageDownloadExecutor.submit(self._lazyFetchArtistImageTask, artistId, imagePath)
        return False

    def saveImagesFromTrack(self, track: dict):
        self.saveTrackImg(track["imageUrl"], track["imageId"])

    # ---- writing plays ---------------------------------------------------------------

    def appendEntries(self, entry: dict):
        """Record a single play. Named for compatibility with the previous
        JSON-backed API (it always took one entry despite the plural name)."""
        if not entry:
            return
        self.repo.insertPlay(self.user, entry["id"], entry["playedAt"], entry["timePlayed"], entry.get("playedFrom"),
                              created_reason=f"manual_entry (user: {self.user})")
        self.repo.commit()

    def appendMetadata(self, meta: dict, created_reason: str | None = None) -> bool:
        self.saveImagesFromTrack(meta)
        entry, track = self._splitEntryAndTrack(meta)
        self.repo.upsertTrack(track, created_reason=created_reason)
        was_inserted = self.repo.insertPlay(self.user, entry["id"], entry["playedAt"], entry["timePlayed"], entry.get("playedFrom"),
                              created_reason=created_reason)
        self.repo.commit()
        self.updatePlaylists(entry.get("playedFrom"))
        return was_inserted

    def appendTrackData(self, timestamp, track, timePlayed, context=None, source="listener"):
        formatted_track = Client.formatTrack(track, timestamp, timePlayed, context=context)
        track_id = track.get("id", "unknown")
        track_name = track.get("name", "unknown")

        if source == self.WEB_API_BACKFILL_SOURCE:
            # Wide, defense-in-depth guard: skip if this exact track already has a
            # play within (duration + 60s) of this one. Deliberately NOT applied to
            # the live listener's own inserts (source == "listener") - the listener
            # is the primary, trusted source, and a genuine short-track replay
            # within this window is normal listening behavior that must not be
            # silently dropped. Backfill is a catch-up mechanism and should be
            # conservative about re-adding something a trusted source may already
            # have captured - this window is symmetric so it catches a duplicate
            # regardless of whether Spotify reported this entry's played_at as a
            # start or end time (see _checkWebApiBackfill for why that can't be
            # assumed one way or the other).
            durationSeconds = (track.get("duration_ms", 0) or 0) // 1000
            tolerance = durationSeconds + self.BACKFILL_INSERT_GUARD_EXTRA_SECONDS
            if self.repo.hasPlayNearTime(self.user, track_id, formatted_track["playedAt"], tolerance):
                if os.environ.get("FLASK_DEBUG", "").lower() in TRUTHY_DEBUG_VALUES:
                    logger.info(
                        "Skipping backfilled play for track %s (%s): an existing play already exists "
                        "within %ds (duration+60s) of played_at=%s",
                        track_id, track_name, tolerance, formatted_track["playedAt"],
                    )
                return False

        created_reason = f"{source}_play (user: {self.user})"
        was_inserted = self.appendMetadata(formatted_track, created_reason=created_reason)
        if was_inserted:
            logger.info(
                "Recording play for user %s: track=%s (%s), timestamp=%s, duration=%dms, source=%s",
                self.user, track_id, track_name, timestamp, timePlayed, source
            )
        return was_inserted

    def importHistory(self, exportedHistory, progressPrefix: str = "", isFinalFile: bool = True, hasPriorError: bool = False, track_file_hash: bool = False,
                      runState: _ImportRunState | None = None):
        importer = self._withCookiesFile(lambda cookiesFile: Importer(cookiesFile=cookiesFile, email=self.email))
        if runState is None:
            runState = _ImportRunState()

        parsedHistory, exportType = importer._convertToList(exportedHistory)
        if not parsedHistory:
            return

        total = len(parsedHistory)
        self.writeProgress("running", 0, total, f"{progressPrefix}Starting import", error=hasPriorError)

        def progressCallback(status, current, totalSteps, message):
            self.writeProgress(status, current, totalSteps, f"{progressPrefix}{message}", error=hasPriorError)

        # Imported tracks/plays are staged locally and only written to the database
        # once the whole import has succeeded. SQLite only allows one writer
        # transaction at a time, so committing incrementally here would either
        # block progress-polling reads for the whole import, or (worse) let a
        # failure partway through leave a half-imported batch committed.
        #
        # INVARIANT: repo methods that self-commit ("with conn:" - writeProgress,
        # image-status writes, playlist upserts) run on this same thread-local
        # connection, and "with conn:" commits WHATEVER is pending on it. They
        # are therefore only safe while no import rows are staged in the
        # transaction: during the staging loop below (which writes nothing to
        # the tracks/plays tables) or after the final commit()/rollback().
        # Never call one between the first upsertTrack and the commit, or it
        # silently commits a partial import.
        stagedTracks: dict[str, dict] = {}
        stagedPlays: list[dict] = []
        index = 0
        # Rolled-back writes must not stay claimed in a batch-shared run state
        claimedRowIdsBefore = set(runState.claimedRowIds)
        insertedPlayKeysBefore = set(runState.insertedPlayKeys)
        try:
            knownTracks = self.repo.getAllTracks()
            for index, meta in enumerate(
                importer.importHistory(parsedHistory, knownTracks, exportType, progressCallback=progressCallback),
                start=1,
            ):
                entry, track = self._splitEntryAndTrack(meta)
                stagedTracks[track["id"]] = track
                stagedPlays.append(entry)
                self.saveImagesFromTrack(track)

                if index % self.PROGRESS_UPDATE_INTERVAL == 0 or index == total:
                    self.writeProgress("running", index, total, f"{progressPrefix}Imported {index} of {total}")

            for track in stagedTracks.values():
                self.repo.upsertTrack(track, created_reason=f"history_import (user: {self.user})")

            insertedCount = 0
            updatedCount = 0
            for entry in stagedPlays:
                track_id = entry["id"]
                played_at = entry["playedAt"]
                time_played = entry["timePlayed"]
                played_from = entry.get("playedFrom")

                # Check if a play for this track already exists within (duration + 60s) tolerance,
                # same logic as API backfill to handle potential overlap with backfilled data
                # where Spotify's played_at can be ambiguous (start or end time).
                track = stagedTracks.get(track_id)
                #< staged tracks carry Client.formatTrack's "duration" key (ms)
                durationSeconds = (track.get("duration", 0) or 0) // 1000 if track else 0
                tolerance = durationSeconds + self.BACKFILL_INSERT_GUARD_EXTRA_SECONDS
                raw_matches = self.repo.getPlaysNearTime(self.user, track_id, played_at, tolerance)
                matches = []
                for m in raw_matches:
                    # Rows this run already wrote belong to other import entries and
                    # are never candidates - otherwise a replay would "correct" the
                    # skip play inserted moments earlier instead of being recorded
                    # itself (see _ImportRunState).
                    if runState.isOwnWrite(track_id, m):
                        continue
                    db_played_at = m["played_at"]
                    diff_start = abs(db_played_at - played_at)
                    diff_end = abs(db_played_at - (played_at + durationSeconds))
                    if diff_start <= self.IMPORT_MATCH_START_WINDOW_SECONDS or diff_end <= self.IMPORT_MATCH_END_WINDOW_SECONDS:
                        matches.append(m)

                if matches:
                    if len(matches) == 1:
                        # Exactly one match - safe to update if data differs
                        existing_play = matches[0]
                        runState.claimedRowIds.add(existing_play["id"])
                        data_differs = (
                            existing_play["time_played"] != time_played or
                            existing_play["played_at"] != played_at
                        )

                        if data_differs:
                            # Update both fields with imported data (more accurate source)
                            conn = self.repo._conn()
                            conn.execute(
                                "UPDATE plays SET played_at = ?, time_played = ? WHERE id = ?",
                                (played_at, time_played, existing_play["id"])
                            )
                            changes = []
                            if int(existing_play["played_at"]) != int(played_at):
                                changes.append(f"played_at corrected from {int(existing_play['played_at'])} to {int(played_at)}")
                            if existing_play["time_played"] != time_played:
                                changes.append(f"time_played corrected from {existing_play['time_played']}ms to {time_played}ms")

                            logger.info(
                                "Updated import play for track %s: %s",
                                track_id, ", ".join(changes)
                            )
                            updatedCount += 1
                            continue
                        else:
                            # Data matches - skip, no update needed
                            if os.environ.get("FLASK_DEBUG", "").lower() in TRUTHY_DEBUG_VALUES:
                                logger.info(
                                    "Skipping import play for track %s: duplicate found with identical data",
                                    track_id,
                                )
                            continue
                    else:
                        # Multiple matches - ambiguous, skip to avoid wrong update
                        if os.environ.get("FLASK_DEBUG", "").lower() in TRUTHY_DEBUG_VALUES:
                            logger.info(
                                "Skipping import play for track %s: %d plays found within tolerance - ambiguous, "
                                "not updating to avoid wrong match",
                                track_id, len(matches),
                            )
                        continue

                # If no matches, proceed to insert as usual
                if self.repo.insertPlay(self.user, track_id, played_at, time_played, played_from,
                                        created_reason=f"history_import (user: {self.user})"):
                    insertedCount += 1
                runState.insertedPlayKeys.add((track_id, played_at))

            if track_file_hash:
                import hashlib
                content_bytes = exportedHistory.encode("utf-8") if isinstance(exportedHistory, str) else str(exportedHistory).encode("utf-8")
                file_hash = hashlib.sha256(content_bytes).hexdigest()
                self.repo.markFileImported(self.user, file_hash)

            self.repo.commit()
            logger.info("Imported %d tracks; %d new plays, %d plays corrected for user %s", len(stagedTracks), insertedCount, updatedCount, self.user)

            status = "complete" if isFinalFile else "running"
            self.writeProgress(status, total, total, f"{progressPrefix}Import complete", error=hasPriorError)
        except Exception as e:
            self.repo.rollback()
            runState.claimedRowIds = claimedRowIdsBefore
            runState.insertedPlayKeys = insertedPlayKeysBefore
            self.writeProgress("failed", index, total, f"{progressPrefix}Import failed: {parseError(e)}", error=True)
            raise

    def importHistoryBatch(self, fileContents: list[str]) -> None:
        """Import multiple export files sequentially - cached up front by the
        caller (app.py reads every upload before starting this thread) and then
        processed one after another, mirroring AutoImporter's existing
        one-file-at-a-time folder-watching behavior. A failure in one file is
        logged and skipped rather than aborting the whole batch, so a single bad
        upload doesn't block the rest."""
        if not fileContents:
            return

        import hashlib
        total = len(fileContents)
        failedCount = 0
        skippedCount = 0
        # One run state for the whole batch: files commit separately, so a
        # skip/replay pair straddling a file boundary would otherwise collapse
        # (the replay in file N+1 matching the skip committed by file N).
        runState = _ImportRunState()
        for index, content in enumerate(fileContents, start=1):
            try:
                isFinalFile = (index == total)
                content_bytes = content.encode("utf-8") if isinstance(content, str) else str(content).encode("utf-8")
                file_hash = hashlib.sha256(content_bytes).hexdigest()

                if self.repo.isFileImported(self.user, file_hash):
                    logger.info("File %s/%s already imported (hash: %s). Skipping.", index, total, file_hash)
                    skippedCount += 1
                    status = "complete" if isFinalFile else "running"
                    self.writeProgress(status, index, total, f"File {index}/{total}: Skipping already imported file", error=(failedCount > 0))
                    continue

                self.importHistory(
                    content,
                    progressPrefix=f"File {index}/{total}: ",
                    isFinalFile=isFinalFile,
                    hasPriorError=(failedCount > 0),
                    track_file_hash=True,
                    runState=runState
                )
            except Exception as e:
                failedCount += 1
                logger.error("Import failed for file %s/%s: %s", index, total, parseError(e))

        succeededCount = total - failedCount - skippedCount
        if failedCount == 0:
            if skippedCount == total:
                self.writeProgress("complete", total, total, "All files were already imported")
            else:
                self.writeProgress("complete", total, total, f"Imported {succeededCount}/{total} files ({skippedCount} skipped)")
        elif succeededCount == 0 and skippedCount == 0:
            self.writeProgress("failed", total, total, f"Imported 0/{total} files (all failed)", error=True)
        else:
            self.writeProgress("complete", total, total,
                                f"Imported {succeededCount}/{total} files ({skippedCount} skipped, {failedCount} failed)", error=True)

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
            WHERE p.username = ?{rangeClause}
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
            WHERE p.username = ?{rangeClause}
              AND al.release_date IS NOT NULL
              AND al.release_date != 0
            GROUP BY decade
            HAVING decade IS NOT NULL
            ORDER BY decade
        """
        rows = conn.execute(query, params).fetchall()
        return {f"{row['decade']}s": row["count"] for row in rows}

    def getCompletionStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> dict:
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        conn = self.repo._conn()
        # Fully classified in SQL - one aggregate row instead of a row per
        # distinct (time_played, duration) pair. Unknown (<=0) durations
        # can't distinguish partial from complete, so anything past the skip
        # threshold counts as complete for them.
        params = [
            COMPLETION_SKIP_THRESHOLD_MS,
            COMPLETION_SKIP_THRESHOLD_MS, COMPLETION_COMPLETE_RATIO,
            COMPLETION_SKIP_THRESHOLD_MS, COMPLETION_COMPLETE_RATIO,
            self.user,
        ]
        rangeClause = self.repo._dateRangeClause(params, startTs, endTs, column="p.played_at")
        query = f"""
            SELECT
                COALESCE(SUM(CASE WHEN p.time_played < ? THEN 1 ELSE 0 END), 0) AS skips,
                COALESCE(SUM(CASE WHEN p.time_played >= ?
                                   AND (t.duration_ms <= 0 OR p.time_played >= t.duration_ms * ?)
                                  THEN 1 ELSE 0 END), 0) AS completes,
                COALESCE(SUM(CASE WHEN p.time_played >= ?
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

    def getLongestStreak(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> int:
        """Longest consecutive days of plays in range. Works off SQL-side
        15-minute buckets (getBucketedPlayTotals) - a bucket's start shares
        its local date with every play inside it, so the distinct-dates set
        is identical to a per-play scan's."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        rows = self.repo.getBucketedPlayTotals(self.user, startTs, endTs)
        if not rows:
            return 0

        play_dates = sorted({
            convertToDatetime(r["bucketStartTs"], tz=self.tz).strftime("%Y-%m-%d")
            for r in rows
        })

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
        bucketedCounts = []
        for row in rows:
            date = convertToDatetime(row["bucketStartTs"], tz=self.tz)
            key = self._bucketKey(date, groupBy)
            name = row["artistName"]
            bucketedCounts.append((key, name, row["plays"]))
            totalPlaysByArtist[name] = totalPlaysByArtist.get(name, 0) + row["plays"]

        if not totalPlaysByArtist:
            return {"buckets": [], "series": []}

        topNames = [name for name, _ in sorted(totalPlaysByArtist.items(), key=lambda kv: kv[1], reverse=True)[:topN]]

        bucketKeys = sorted({key for key, _, _ in bucketedCounts})
        seriesData = {name: {key: 0 for key in bucketKeys} for name in topNames}
        for key, name, plays in bucketedCounts:
            if name in seriesData:
                seriesData[name][key] += plays

        series = [{"name": name, "data": [seriesData[name][key] for key in bucketKeys]} for name in topNames]
        return {"buckets": bucketKeys, "series": series}

    def _makeOnStaleCallback(self) -> callable:
        """Create an onStale callback that retries with exponential backoff.
        Called when the listener detects a stale feed or auth error and needs
        to reconnect with fresh cookies/session."""
        def onStaleWithBackoff():
            with self._health_lock:
                self.listener_health = "DEGRADED"
                self.listener_error_count += 1

            for attempt in range(self.RECONNECT_MAX_RETRIES):
                if attempt > 0:
                    backoff_delay = min(
                        self.RECONNECT_INITIAL_DELAY * (2 ** attempt),
                        self.RECONNECT_MAX_DELAY
                    )
                    logger.warning(
                        "Reconnection attempt %d/%d, waiting %ds before retry",
                        attempt, self.RECONNECT_MAX_RETRIES, backoff_delay
                    )
                    time.sleep(backoff_delay)

                try:
                    logger.info("Attempting to reconnect (attempt %d/%d)", attempt + 1, self.RECONNECT_MAX_RETRIES)
                    self.startListener(email=self.email)
                    logger.info("Reconnection succeeded on attempt %d", attempt + 1)
                    return
                except Exception as e:
                    logger.warning("Reconnection attempt %d failed: %s", attempt + 1, parseError(e))
                    with self._health_lock:
                        self.listener_last_error = parseError(e)
                    if attempt == self.RECONNECT_MAX_RETRIES - 1:
                        logger.error(
                            "Reconnection failed after %d attempts, tracking paused for this user",
                            self.RECONNECT_MAX_RETRIES
                        )
                        with self._health_lock:
                            self.listener_health = "DEAD"

        return onStaleWithBackoff

    def startListener(self, cookiesFile=None, email=None):
        if cookiesFile:
            self.cookiesFile = cookiesFile
        if email:
            if self.email and email != self.email:
                logger.warning(
                    "Email mismatch in startListener for user %s: was %s, now %s. "
                    "This could indicate confused session state.",
                    self.user, self.email, email
                )
            self.email = email
        if self.listener is not None:
            logger.info("Stopping existing listener for user %s before re-starting", self.user)
            try:
                self.listener.stop()
            except Exception as e:
                logger.error("Failed to stop existing listener for user %s: %s", self.user, parseError(e))
        self.listener = self._withCookiesFile(lambda cf: Listener(cf, email=self.email, get_credentials=self.getUserSpotifyCredentials))
        if self.listener.contaminationDetected:
            # The cookies authenticate as a different Spotify account (see
            # Listener.__init__'s contamination check). The listener itself
            # refuses to record; reflect that as DEAD so the UI shows the user
            # something actionable instead of a listener that looks healthy
            # while recording nothing.
            with self._health_lock:
                self.listener_health = "DEAD"
                self.listener_last_error = (
                    "Stored cookies belong to a different Spotify account - "
                    "re-login with matching cookies to resume tracking"
                )
            return
        with self._health_lock:
            self.listener_health = "HEALTHY"
            self.listener_error_count = 0
        self.listener.startListener_thread(
            callback=self._addToDatabaseFromListener,
            onStale=self._makeOnStaleCallback(),
            onWebApiSnapshot=self._reconcileWithWebApiHistory,
        )

    def getUserSpotifyCredentials(self) -> dict | None:
        return self.repo.getUserSpotifyCredentials(self.user)

    def updateUserSpotifyCredentials(self, clientId: str | None, clientSecret: str | None, refreshToken: str | None) -> None:
        self.repo.updateUserSpotifyCredentials(self.user, clientId, clientSecret, refreshToken)

    def _reconcileWithWebApiHistory(self, apiItems: list[dict]) -> None:
        """Remove PROVABLE duplicate local plays: Web API backfill copies of a
        play another source already recorded. Both the live listener and the
        backfill can capture the same instant with different timestamps
        (Spotify's played_at field is documented as inconsistent about whether
        it reports a track's start or end time, per spotify/web-api#1083 - see
        _checkWebApiBackfill for how that ambiguity is handled on the ingest
        side), leaving two rows for the same track seconds apart.

        Deletion requires BOTH proofs:
        - proximity: a same-track sibling row within
          DUPLICATE_RECORDING_TOLERANCE_SECONDS, AND
        - mixed sources: the cluster holds a backfill row plus at least one
          row from another source (listener / import / legacy-NULL).
        Only the backfill copies are deleted - backfill is the only secondary
        recorder, so rows from primary sources are never deleted. Proximity
        alone proves nothing: real exports genuinely contain a short skip
        followed by a restart of the same track seconds later, and such
        same-source clusters must survive untouched.

        Deliberately never deletes a play just because it's absent from the
        Web API response: Spotify's recently-played endpoint isn't a complete
        log (limited item count, its own internal play-duration threshold,
        track relinking can return a different ID for the same song), so a
        lone play with no same-track sibling is always left alone - only a
        genuine nearby cross-source duplicate counts as proof.

        Only runs for users with working Spotify Developer API credentials
        configured (invoked from Listener._checkWebApiBackfill's
        onWebApiSnapshot callback).

        Bounded to the exact [oldest, newest] played_at span the API response
        covers - never reaches past that window, so it can't touch older
        history."""
        if not apiItems:
            return

        apiTimes = [
            timeToInt(item["played_at"])
            for item in apiItems
            if item.get("track", {}).get("id") and item.get("played_at")
        ]
        if not apiTimes:
            logger.debug("Reconciliation skipped: no API items with both track id and played_at")
            return

        windowStart = min(apiTimes)
        windowEnd = max(apiTimes)

        localPlays = self.repo.getPlaysWithSourceInRange(self.user, windowStart, windowEnd)
        if not localPlays:
            return

        playsByTrack: dict[str, list[dict]] = {}
        for play in localPlays:
            playsByTrack.setdefault(play["id"], []).append(play)

        deletedCount = 0
        for trackId, group in playsByTrack.items():
            if len(group) < 2:
                continue  # no sibling for this track - nothing proves duplication, never delete

            # Cluster same-track plays that are within tolerance of a shared
            # anchor - each cluster of 2+ might be the same real listen
            # recorded more than once. Sorted chronologically first (the DB
            # query has no ORDER BY) so the anchor - and therefore which
            # plays end up in which cluster - is deterministic and doesn't
            # depend on the arbitrary order SQLite happens to return rows in.
            remaining = sorted(group, key=lambda play: play["playedAt"])
            while remaining:
                anchor = remaining.pop(0)
                cluster = [anchor]
                stillRemaining = []
                for other in remaining:
                    if abs(anchor["playedAt"] - other["playedAt"]) <= self.DUPLICATE_RECORDING_TOLERANCE_SECONDS:
                        cluster.append(other)
                    else:
                        stillRemaining.append(other)
                remaining = stillRemaining

                if len(cluster) < 2:
                    continue  # no close-in-time sibling for this one either

                backfillCopies = [
                    play for play in cluster
                    if (play.get("createdReason") or "").startswith(self.WEB_API_BACKFILL_SOURCE)
                ]
                if not backfillCopies or len(backfillCopies) == len(cluster):
                    # Same-source cluster: without a second source there is no
                    # proof of double-recording (could be a genuine skip-then-
                    # restart) - never guess, never delete.
                    continue

                for play in backfillCopies:
                    if self.repo.deletePlay(self.user, play["id"], play["playedAt"]):
                        deletedCount += 1
                        logger.debug(
                            "Reconciliation deleted duplicate play: user=%s track=%s time=%d",
                            self.user, play["id"], play["playedAt"]
                        )

        if deletedCount:
            self.repo.commit()
            logger.info(
                "Web API reconciliation: removed %d duplicate play(s) for user %s",
                deletedCount, self.user,
            )

    def startAutoImporter(self):
        self.autoImporter.start()

    def isListenerLoggedIn(self):
        if self.listener == None:
            return False
        return self.listener.isLoggedIn()

    def getListenerHealth(self) -> dict:
        """Get current listener health status for displaying to user."""
        with self._health_lock:
            seconds_since_last_poll = None
            if self.listener_last_poll_time is not None:
                seconds_since_last_poll = time.monotonic() - self.listener_last_poll_time
            return {
                "status": self.listener_health,
                "error_count": self.listener_error_count,
                "last_error": self.listener_last_error,
                "seconds_since_last_poll": seconds_since_last_poll,
            }

    def stop(self):
        if self.listener is not None:
            self.listener.stop()
        self.autoImporter.wd.stop()
        self.stopMetadataBackfiller()
        self.stopWrappedCalculationsWorker()

    def startWrappedCalculationsWorker(self) -> None:
        """Start the background thread to precalculate wrapped data."""
        if not hasattr(self, "wrapped_thread") or not hasattr(self, "wrapped_stop_event"):
            return
        if self.wrapped_thread is not None and self.wrapped_thread.is_alive():
            return
        self.wrapped_stop_event.clear()
        self.wrapped_thread = threading.Thread(
            target=self._wrappedCalculationsLoop,
            name=f"wrapped-worker-{self.user}",
            daemon=True
        )
        self.wrapped_thread.start()

    def stopWrappedCalculationsWorker(self) -> None:
        """Signal and wait for the background wrapped worker thread to stop."""
        if not hasattr(self, "wrapped_thread") or not hasattr(self, "wrapped_stop_event"):
            return
        if self.wrapped_thread is None:
            return
        self.wrapped_stop_event.set()
        self.wrapped_thread.join(timeout=3)
        self.wrapped_thread = None

    def _wrappedCalculationsLoop(self) -> None:
        """Periodically checks if plays have changed and recalculates wrapped stats."""
        import random
        try:
            # 1. Random startup delay to distribute CPU load if multiple users are loaded
            startup_delay = random.randint(self.WRAPPED_WORKER_MIN_START_DELAY, self.WRAPPED_WORKER_MAX_START_DELAY)
            logger.info("[WrappedWorker-%s] Starting with initial delay of %d seconds", self.user, startup_delay)
            if self.wrapped_stop_event.wait(startup_delay):
                return

            while not self.wrapped_stop_event.is_set():
                try:
                    self._checkAndRecalculateWrapped()
                except Exception as e:
                    logger.error("[WrappedWorker-%s] Error checking wrapped: %s", self.user, parseError(e))

                # Check loop interval
                if self.wrapped_stop_event.wait(self.WRAPPED_WORKER_LOOP_INTERVAL):
                    break
        except Exception as e:
            logger.error("[WrappedWorker-%s] Worker loop crashed: %s", self.user, parseError(e))

    def _getWrappedRecalcLock(self, year: int) -> threading.Lock:
        """Per-(user instance, year) lock so the periodic worker and an
        on-demand /wrapped recalculation never run _calculateAndSaveWrapped
        for the same year at the same time."""
        with self._wrapped_recalc_locks_guard:
            lock = self._wrapped_recalc_locks.get(year)
            if lock is None:
                lock = threading.Lock()
                self._wrapped_recalc_locks[year] = lock
            return lock

    def _wrappedCacheNeedsRecalc(self, year: int, yearStart: datetime.datetime, yearEnd: datetime.datetime, max_played_at: float):
        """Compares the cached (max_played_at, play_count) snapshot for a year
        against live values. Returns (isStale, cached_max, cached_total, current_total)."""
        current_total = self.repo.getPlayCountInPeriod(self.user, yearStart.timestamp(), yearEnd.timestamp())
        cached_max = self.repo.getCachedWrappedMaxPlayedAt(self.user, year)
        cached_total = self.repo.getCachedWrappedTotalPlays(self.user, year)
        isStale = cached_max is None or cached_total is None or cached_max < max_played_at or cached_total != current_total
        return isStale, cached_max, cached_total, current_total

    def _checkAndRecalculateWrapped(self) -> None:
        """Checks for each year if there is new data and triggers recalculation if needed."""
        nowLocal = datetime.datetime.now(tz=self.tz)
        currentYear = nowLocal.year

        oldestEntries = self.getEntriesFromOld(count=1, fullPagination=False)
        earliestYear = convertToDatetime(oldestEntries[0]["playedAt"], tz=self.tz).year if oldestEntries else currentYear
        availableYears = list(range(currentYear, earliestYear - 1, -1))

        for year in availableYears:
            if self.wrapped_stop_event.is_set():
                break

            yearStart = nowLocal.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            yearEnd = nowLocal.replace(year=year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

            # Query max played_at for this year
            max_played_at = self.repo.getMaxPlayedAtInPeriod(self.user, yearStart.timestamp(), yearEnd.timestamp())
            if max_played_at is None:
                # No plays for this year. If there is cached data, delete it.
                self.repo.deleteUserWrapped(self.user, year)
                continue

            isStale, cached_max, cached_total, current_total = self._wrappedCacheNeedsRecalc(year, yearStart, yearEnd, max_played_at)
            if not isStale:
                continue

            lock = self._getWrappedRecalcLock(year)
            if not lock.acquire(blocking=False):
                # An on-demand /wrapped recalculation is already handling this
                # year; don't duplicate the work or block the periodic loop -
                # the next cycle will notice if anything is still stale.
                logger.info("[WrappedWorker-%s] Year %d recalculation already in progress elsewhere, skipping this cycle", self.user, year)
                continue
            try:
                cachedMaxDisplay = convertToDatetime(cached_max, tz=self.tz).isoformat() if cached_max is not None else "none"
                actualMaxDisplay = convertToDatetime(max_played_at, tz=self.tz).isoformat()
                logger.info("[WrappedWorker-%s] Recalculating wrapped for year %d (cached max: %s, actual max: %s, cached plays: %s, actual plays: %s)",
                            self.user, year, cachedMaxDisplay, actualMaxDisplay, str(cached_total), str(current_total))
                self._calculateAndSaveWrapped(year, yearStart, yearEnd, max_played_at)
            finally:
                lock.release()
            # Sleep briefly between years to distribute database load
            if self.wrapped_stop_event.wait(self.WRAPPED_YEAR_DELAY_SECONDS):
                break

    def _calculateAndSaveWrapped(self, year: int, yearStart: datetime.datetime, yearEnd: datetime.datetime, max_played_at: float) -> None:
        """Runs all queries to precalculate the Spotify Wrapped stats and caches them in user_wrapped table."""
        # 1. Total plays and milliseconds
        totalPlays, totalMs = self.getPlayTotals(yearStart, yearEnd)

        # 2. Longest streak
        longestStreak = self.getLongestStreak(yearStart, yearEnd)

        # 3. Peak listening time
        peakListeningTime = self.getPeakListeningTime(yearStart, yearEnd)
        peak_day = peakListeningTime[0] if peakListeningTime else None
        peak_plays = peakListeningTime[1] if peakListeningTime else None

        # 4. Unique counts
        uniqueSongs = self.getSongsCount(yearStart, yearEnd)
        uniqueArtists = self.getArtistsCount(yearStart, yearEnd)
        discoveredSongsCount = self.getDiscoveredSongsCount(yearStart, yearEnd)
        discoveredArtistsCount = self.getDiscoveredArtistsCount(yearStart, yearEnd)

        # 5. Timeseries
        timeSeriesDay = self.getListeningTimeSeries(startDate=yearStart, endDate=yearEnd, groupBy="day")
        timeSeriesWeek = self.getListeningTimeSeries(startDate=yearStart, endDate=yearEnd, groupBy="week")
        timeSeriesMonth = self.getListeningTimeSeries(startDate=yearStart, endDate=yearEnd, groupBy="month")

        # 6. Top 100 lists
        topSongs = self.getTopSongs(startDate=yearStart, endDate=yearEnd, by="plays", limit=100)
        topArtists = self.getTopArtists(startDate=yearStart, endDate=yearEnd, by="plays", limit=100)
        topAlbums = self.getTopAlbums(startDate=yearStart, endDate=yearEnd, by="plays", limit=100)

        # 7. Discoveries lists (unbounded query filtered by firstListenedAt)
        songsStats = self.getSongsStats(sortBy="plays")
        artistsStats = self.getArtistsStats()
        albumsStats = self.getAlbumsStats(sortBy="plays")

        yearStartTs, yearEndTs = yearStart.timestamp(), yearEnd.timestamp()

        discoveredSongsList = [
            item for item in songsStats
            if item.get("firstListenedAt") is not None and yearStartTs <= item["firstListenedAt"] < yearEndTs
        ]
        discoveredSongsList.sort(key=lambda item: item.get("plays", 0), reverse=True)
        discoveredSongsList = discoveredSongsList[:100]

        discoveredArtistsList = [
            item for item in artistsStats
            if item.get("firstListenedAt") is not None and yearStartTs <= item["firstListenedAt"] < yearEndTs
        ]
        discoveredArtistsList.sort(key=lambda item: item.get("plays", 0), reverse=True)
        discoveredArtistsList = discoveredArtistsList[:100]

        discoveredAlbumsList = [
            item for item in albumsStats
            if item.get("firstListenedAt") is not None and yearStartTs <= item["firstListenedAt"] < yearEndTs
        ]
        discoveredAlbumsList.sort(key=lambda item: item.get("plays", 0), reverse=True)
        discoveredAlbumsList = discoveredAlbumsList[:100]

        data = {
            "calculated_at": time.time(),
            "max_played_at": max_played_at,
            "total_plays": totalPlays,
            "total_ms": totalMs,
            "longest_streak": longestStreak,
            "peak_day": peak_day,
            "peak_plays": peak_plays,
            "unique_songs": uniqueSongs,
            "unique_artists": uniqueArtists,
            "discovered_songs": discoveredSongsCount,
            "discovered_artists": discoveredArtistsCount,
            "time_series_day": json.dumps(timeSeriesDay),
            "time_series_week": json.dumps(timeSeriesWeek),
            "time_series_month": json.dumps(timeSeriesMonth),
            "top_songs": json.dumps(topSongs),
            "top_artists": json.dumps(topArtists),
            "top_albums": json.dumps(topAlbums),
            "discovered_songs_list": json.dumps(discoveredSongsList),
            "discovered_artists_list": json.dumps(discoveredArtistsList),
            "discovered_albums_list": json.dumps(discoveredAlbumsList),
        }
        self.repo.saveCachedWrapped(self.user, year, data)

    def recalculateWrappedForYear(self, year: int) -> None:
        """Calculate and cache wrapped stats for a year immediately (synchronously).

        Waits on this year's recalc lock rather than racing the periodic
        worker: if the worker is already recalculating this exact year, this
        blocks until it's done instead of duplicating the (expensive) work,
        then re-checks whether the cache is still stale before doing anything -
        the worker may have already brought it up to date while we waited.
        """
        nowLocal = datetime.datetime.now(tz=self.tz)
        yearStart = nowLocal.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        yearEnd = nowLocal.replace(year=year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        max_played_at = self.repo.getMaxPlayedAtInPeriod(self.user, yearStart.timestamp(), yearEnd.timestamp())
        if max_played_at is None:
            return

        with self._getWrappedRecalcLock(year):
            isStale, _, _, _ = self._wrappedCacheNeedsRecalc(year, yearStart, yearEnd, max_played_at)
            if isStale:
                self._calculateAndSaveWrapped(year, yearStart, yearEnd, max_played_at)

    def startMetadataBackfiller(self) -> None:
        """Start the background thread to fill in missing album metadata."""
        if not hasattr(self, "backfiller_thread") or not hasattr(self, "backfiller_stop_event"):
            return
        if self.backfiller_thread is not None and self.backfiller_thread.is_alive():
            return
        self.backfiller_stop_event.clear()
        self.backfiller_thread = threading.Thread(
            target=self._metadataBackfillLoop,
            name=f"metadata-backfiller-{self.user}",
            daemon=True
        )
        self.backfiller_thread.start()

    def stopMetadataBackfiller(self) -> None:
        """Signal and wait for the background backfiller thread to stop."""
        if not hasattr(self, "backfiller_thread") or not hasattr(self, "backfiller_stop_event"):
            return
        if self.backfiller_thread is None:
            return
        self.backfiller_stop_event.set()
        self.backfiller_thread.join(timeout=3)
        self.backfiller_thread = None

    def _metadataBackfillLoop(self) -> None:
        """Periodically queries Spotify for missing album release dates and tracks."""
        import random
        try:
            # 1. Random startup offset to prevent multiple user threads from starting at the same moment
            startup_delay = random.randint(30, 90)
            logger.info("[Backfiller-%s] Starting with initial delay of %d seconds", self.user, startup_delay)
            if self.backfiller_stop_event.wait(startup_delay):
                logger.info("[Backfiller-%s] Stopped during startup delay", self.user)
                return

            while not self.backfiller_stop_event.is_set():
                target_ids = []
                try:
                    # 2. Get Spotify API credentials if configured
                    creds = self.getUserSpotifyCredentials()

                    # 3. Query up to N missing album IDs
                    missing_ids = self.repo.getAlbumsMissingMetadata(limit=self.BACKFILLER_ALBUM_QUEUE_SIZE)
                    if not missing_ids:
                        if self.backfiller_stop_event.wait(300):
                            break
                        continue

                    # 4. Process-wide deduplication: filter out already active backfills
                    with Database._backfill_lock:
                        for album_id in missing_ids:
                            if album_id not in Database._active_backfills:
                                target_ids.append(album_id)
                                Database._active_backfills.add(album_id)
                                if len(target_ids) >= 20:  # Spotify bulk limit is 20
                                    break

                    # 5. If nothing eligible remains, wait and try next iteration
                    if not target_ids:
                        if self.backfiller_stop_event.wait(300):
                            break
                        continue

                    # 6. Fetch detailed metadata
                    logger.info("[Backfiller-%s] Fetching metadata for %d albums", self.user, len(target_ids))
                    fetched_albums = []
                    attempted_ids = []  #< albums that got a definitive response (incl. "gone") - rate-limits their next retry
                    use_fallback = True

                    if creds and creds.get("client_id") and creds.get("refresh_token"):
                        from Database.Listeners.spotifyListener import _refresh_spotify_access_token
                        import requests

                        access_token = _refresh_spotify_access_token(
                            creds["client_id"], creds["client_secret"], creds["refresh_token"]
                        )
                        if access_token:
                            headers = {"Authorization": f"Bearer {access_token}"}
                            ids_str = ",".join(target_ids)
                            url = f"https://api.spotify.com/v1/albums?ids={ids_str}"
                            resp = requests.get(url, headers=headers, timeout=10)
                            if resp.status_code == 200:
                                albums_data = resp.json().get("albums") or []
                                for album_raw in albums_data:
                                    if album_raw:
                                        fetched_albums.append(album_raw)
                                # Null entries are albums Spotify has no data for -
                                # count those as attempted too, or they'd be re-queued
                                # every cycle forever.
                                attempted_ids = list(target_ids)
                                use_fallback = False
                            else:
                                if os.environ.get("FLASK_DEBUG", "").lower() in TRUTHY_DEBUG_VALUES:
                                    logger.warning(
                                        "[Backfiller-%s] Spotify Web API returned status %d. Falling back to SpotipyFree.",
                                        self.user, resp.status_code
                                    )
                        else:
                            logger.warning("[Backfiller-%s] Failed to refresh access token. Falling back to SpotipyFree.", self.user)

                    if use_fallback:
                        import SpotipyFree
                        import time
                        try:
                            cookiesFile = self._materializeCookiesFile()
                            sp = SpotipyFree.Spotify(cookiesFile=str(cookiesFile))
                            for album_id in target_ids:
                                if self.backfiller_stop_event.is_set():
                                    break
                                try:
                                    album_raw = sp.album(album_id)
                                    if album_raw:
                                        fetched_albums.append(album_raw)
                                    attempted_ids.append(album_id)  #< a clean "no data" reply is definitive; exceptions stay unmarked for a next-cycle retry
                                except Exception as fe:
                                    logger.warning("[Backfiller-%s] SpotipyFree failed for album %s: %s", self.user, album_id, fe)
                                self.backfiller_stop_event.wait(1.0)
                        finally:
                            cookiesFile.unlink(missing_ok=True)

                        if fetched_albums:
                            logger.info("[Backfiller-%s] SpotipyFree fetched %d album(s)", self.user, len(fetched_albums))
                        else:
                            logger.warning("[Backfiller-%s] SpotipyFree fallback failed to fetch any albums", self.user)

                    from Database.utils import convertToDatetime
                    updated_count = 0
                    for album_raw in fetched_albums:
                        album_id = album_raw.get("id")
                        release_date_str = album_raw.get("release_date")
                        total_tracks = album_raw.get("total_tracks", 0)
                        album_name = album_raw.get("name")

                        if release_date_str == "0000-00-00" or not release_date_str:
                            release_date = 0.0
                        else:
                            try:
                                dt = convertToDatetime(release_date_str)
                                release_date = dt.timestamp() if dt else 0.0
                            except Exception:
                                release_date = 0.0

                        # A blank name isn't data - passing None skips the name update
                        # so a blanked response can't overwrite a name the importer
                        # already filled from the user's export.
                        self.repo.updateAlbumMetadata(album_id, release_date, total_tracks,
                                                      name=album_name if album_name else None)

                        # Update names (and durations, when provided) for the tracks
                        # in this album if returned - the album response is the only
                        # duration source for tracks whose own lookup came back blanked.
                        tracks_data = album_raw.get("tracks", {}).get("items") or []
                        for track_raw in tracks_data:
                            track_id = track_raw.get("id") or track_raw.get("track_id")
                            track_name = track_raw.get("name")
                            if track_id and track_name:
                                duration_ms = track_raw.get("duration_ms") or 0
                                self.repo.updateTrackName(track_id, track_name,
                                                          duration_ms=duration_ms if duration_ms > 0 else None)

                        updated_count += 1

                    if attempted_ids:
                        self.repo.markAlbumsBackfillAttempted(attempted_ids)

                    if updated_count > 0:
                        logger.info(
                            "[Backfiller-%s] Updated metadata for %d album(s)",
                            self.user, updated_count
                        )

                    # 7. Release lock on the processed IDs
                    with Database._backfill_lock:
                        for album_id in target_ids:
                            Database._active_backfills.discard(album_id)

                except Exception as e:
                    logger.error("[Backfiller-%s] Error in metadata backfiller loop: %s", self.user, e)
                    # Cleanup registry if error occurred mid-process
                    try:
                        with Database._backfill_lock:
                            for album_id in target_ids:
                                Database._active_backfills.discard(album_id)
                    except Exception:
                        pass

                if self.backfiller_stop_event.wait(300):
                    break

        finally:
            logger.info("[Backfiller-%s] Exited gracefully", self.user)


if __name__ == "__main__":

    manager = Database(user="Tzur")
    manager.startListener("cookies.json")
    manager.startAutoImporter()
    import code
    print("Starting interactive shell. Access 'manager' object directly.")
    code.interact(local=dict(globals(), **locals()))

    # import SpotipyFree
    # sp = SpotipyFree.Spotify()
    # sp.login()

    # importFile = Path("importMe.json")
    # if importFile.exists():
    #     with importFile.open("r", encoding="utf-8") as f:
    #         historyPayload = json.load(f)
    #     manager.importSpotifyHistory(historyPayload)
