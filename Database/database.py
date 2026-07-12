from __future__ import annotations
import datetime
import logging
import os
import re
import tempfile
import threading
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
    from Database.utils import parseError, convertToDatetime, dateToString, startOfDay, startOfWeek, startOfMonth
except ModuleNotFoundError:
    from Formatters.spotifyClient import Client
    from Importers.StreamingHistoryImporter import Importer
    from Importers.AutoImporter import AutoImporter
    from Listeners.spotifyListener import Listener
    from repository import Repository, IMAGE_KIND_TRACK, IMAGE_KIND_ARTIST, IMAGE_STATUS_OK, IMAGE_STATUS_FAILED
    from utils import parseError, convertToDatetime, dateToString, startOfDay, startOfWeek, startOfMonth

logger = logging.getLogger(__name__)

IMAGE_DOWNLOAD_WORKERS = 5   #< bounds total concurrent image downloads for the whole process, not per user

# Images are shared across every user (album art / artist photos are the same
# bytes for everyone), so they live in one directory tree instead of under each
# user's own folder. Inside Data/ (see Database/db.py's DEFAULT_DB_PATH) so the
# Docker volume mount that persists the database also covers it.
MEDIA_DIR = Path(__file__).resolve().parent / "Data" / "Media"


class Database:
    PROGRESS_UPDATE_INTERVAL = 10   #< Write import progress to disk every N entries instead of every entry

    # Shared across every Database instance (every user) in this process. Image
    # download de-duplication is enforced by the `images` table (atomic across
    # threads *and* users), so a single bounded pool for the whole process is
    # enough - there's no need for one per user, and no need for the old
    # per-user in-memory id sets / metadata.json files this replaces.
    imgDir_tracks = MEDIA_DIR / "tracks"
    imgDir_artists = MEDIA_DIR / "artists"
    _imageDownloadExecutor = concurrent.futures.ThreadPoolExecutor(max_workers=IMAGE_DOWNLOAD_WORKERS)
    _imageIdsLock = threading.RLock()
    _artistImageLazyFetchAttempted = set()

    def __init__(self, user: str, cookiesFile: str | None = None, email: str | None = None, dbPath=None):
        if not user:
            raise ValueError("Database user must be specified and cannot be empty.")
        self.user = user
        self.cookiesFile = cookiesFile
        self.email = email
        self.listener = None
        self.baseDir = Path(__file__).resolve().parent

        # All Database instances (one per user) share the same underlying SQLite
        # file - catalog data (tracks/artists/albums/images) is global, so it's
        # stored once regardless of how many users have played a given track.
        self.repo = Repository(dbPath) if dbPath is not None else Repository()
        self.repo.upsertUser(user, email)

        self.autoImportFolderPath = self.baseDir / ".." / "autoImport" / self.user

        filterKeyword = os.environ.get("IMPORT_KEYWORD", None)
        logger.info("auto import filtering by %s", filterKeyword)
        self.autoImporter = AutoImporter(folderPath=self.autoImportFolderPath,
                                         importCallback=self.importHistory,
                                         pollInterval=5,
                                         keyword=filterKeyword)

    def _addToDatabaseFromListener(self, data) -> None:
        if not data:
            return
        for item in data:
            track = item.get("track")
            timestamp = item.get("played_at")
            msPlayed = item.get("ms_played", 0)
            if track:
                # Per-item isolation: if the callback raised, the listener would
                # retry the whole batch forever and record nothing new until the
                # bad item aged out of the recently-played feed.
                try:
                    self.appendTrackData(timestamp, track, msPlayed, context=item.get("context", None))
                except Exception as e:
                    logger.error("Error adding track from listener: %s", parseError(e))

    def _materializeCookiesFile(self) -> Path:
        """SpotipyFree/spotapi only know how to read a Spotify session from a file
        path (spotapi.saver.JSONSaver), not from a dict - write this user's
        cookies (the database is the source of truth) to a short-lived temp file
        in the same [{"identifier", "cookies"}, ...] shape SpotipyFree.saveSession
        produces. The caller is responsible for deleting it once the client
        holding it has been constructed - it's only read at construction time."""
        cookies = self.repo.getUserCookies(self.user) or {}
        tmpFd, tmpPath = tempfile.mkstemp(prefix=f"cookies_{self.user}_", suffix=".json")
        os.close(tmpFd)
        tmpPath = Path(tmpPath)
        tmpPath.write_text(json.dumps([{"identifier": self.email, "cookies": cookies}]), encoding="utf-8")
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
            self.repo.upsertTrack(track)
            self.repo.commit()
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

    def _entryWithTrackMetadata(self, entry: dict) -> dict | None:
        track = self._ensureTrackMetadata(entry["id"])
        if track is None:
            return None
        return self._mergeEntryWithTrack(entry, track)

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

    def getHistory(self) -> list:
        return self._paginateEntries(self.repo.getPlaysOldestFirst(self.user))

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

    def saveArtistImg(self, url: str, imgId: str):
        self._saveImg(self.imgDir_artists, url, imgId, kind=IMAGE_KIND_ARTIST)

    def _lazyFetchArtistImageTask(self, artistId: str, imagePath: Path) -> bool:
        try:
            res = requests.get(f"https://open.spotify.com/artist/{artistId}", timeout=5)
            match = re.search(r'<meta property="og:image" content="([^"]+)"', res.text)
            if not match:
                return False
            imgData = requests.get(match.group(1), timeout=5).content
            imagePath.parent.mkdir(parents=True, exist_ok=True)
            imagePath.write_bytes(imgData)
            return True
        except Exception as e:
            logger.error("Failed to lazy load artist image for %s: %s", artistId, parseError(e))
            return False

    def lazyFetchArtistImage(self, artistId: str, imagePath: Path):
        """Best-effort fetch of an artist's image scraped from their public
        Spotify page, used as a fallback for artists we never received image
        metadata for from the API. Deduplicated per artist id (via the same
        lock used by the rest of the image pipeline) so repeated requests for
        a still-missing image don't keep re-hitting Spotify.

        The actual fetch runs on the shared image-download executor (like
        saveTrackImg()/saveArtistImg()) instead of inline, so a request for a
        still-missing image doesn't block the request thread on up to two
        sequential network calls. Returns True if the image is already on
        disk (nothing to do); otherwise returns the submitted Future for a
        freshly kicked-off fetch (the HTTP route that calls this doesn't wait
        on it - it just serves whatever's on disk right now, same as the
        other image types - callers that do need to wait, e.g. tests, can
        call .result() on it), or False if there's nothing to fetch (no
        artistId, or a fetch for this id was already attempted)."""
        if imagePath.exists():
            return True
        if not artistId:
            return False

        with self._imageIdsLock:
            if artistId in self._artistImageLazyFetchAttempted:
                return imagePath.exists()
            self._artistImageLazyFetchAttempted.add(artistId)

        return self._imageDownloadExecutor.submit(self._lazyFetchArtistImageTask, artistId, imagePath)

    def saveImagesFromTrack(self, track: dict):
        self.saveTrackImg(track["imageUrl"], track["imageId"])

    # ---- writing plays ---------------------------------------------------------------

    def appendEntries(self, entry: dict):
        """Record a single play. Named for compatibility with the previous
        JSON-backed API (it always took one entry despite the plural name)."""
        if not entry:
            return
        self.repo.insertPlay(self.user, entry["id"], entry["playedAt"], entry["timePlayed"], entry.get("playedFrom"))
        self.repo.commit()

    def appendMetadata(self, meta: dict) -> None:
        self.saveImagesFromTrack(meta)
        entry, track = self._splitEntryAndTrack(meta)
        self.repo.upsertTrack(track)
        self.repo.insertPlay(self.user, entry["id"], entry["playedAt"], entry["timePlayed"], entry.get("playedFrom"))
        self.repo.commit()
        self.updatePlaylists(entry.get("playedFrom"))

    def appendTrackData(self, timestamp, track, timePlayed, context=None):
        self.appendMetadata(Client.formatTrack(track, timestamp, timePlayed, context=context))

    def resortDatabase(self):
        """No-op: plays are always returned in played_at order via SQL ORDER BY,
        so there's no persisted ordering left to fix."""
        logger.info("Resorted Database")

    def deduplicate(self) -> int:
        """No-op: plays.UNIQUE(username, track_id, played_at) makes it impossible
        to insert a duplicate in the first place. Kept for API compatibility with
        callers (app.py's startup path)."""
        return 0

    def importHistory(self, exportedHistory, progressPrefix: str = ""):
        importer = self._withCookiesFile(lambda cookiesFile: Importer(cookiesFile=cookiesFile, email=self.email))

        parsedHistory, exportType = importer._convertToList(exportedHistory)
        if not parsedHistory:
            return

        total = len(parsedHistory)
        self.writeProgress("running", 0, total, f"{progressPrefix}Starting import")

        def progressCallback(status, current, totalSteps, message):
            self.writeProgress(status, current, totalSteps, f"{progressPrefix}{message}")

        # Imported tracks/plays are staged locally and only written to the database
        # once the whole import has succeeded. SQLite only allows one writer
        # transaction at a time, so committing incrementally here would either
        # block progress-polling reads for the whole import, or (worse) let a
        # failure partway through leave a half-imported batch committed. Progress
        # writes go through their own connection/commit (Repository.writeProgress),
        # so they stay live throughout regardless.
        stagedTracks: dict[str, dict] = {}
        stagedPlays: list[dict] = []
        index = 0
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
                self.repo.upsertTrack(track)
            for entry in stagedPlays:
                self.repo.insertPlay(self.user, entry["id"], entry["playedAt"], entry["timePlayed"], entry.get("playedFrom"))
            self.repo.commit()

            self.writeProgress("complete", total, total, f"{progressPrefix}Import complete")
        except Exception as e:
            self.repo.rollback()
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

        total = len(fileContents)
        failedCount = 0
        for index, content in enumerate(fileContents, start=1):
            try:
                self.importHistory(content, progressPrefix=f"File {index}/{total}: ")
            except Exception as e:
                failedCount += 1
                logger.error("Import failed for file %s/%s: %s", index, total, parseError(e))

        succeededCount = total - failedCount
        if failedCount == 0:
            self.writeProgress("complete", total, total, f"Imported {succeededCount}/{total} files")
        elif succeededCount == 0:
            self.writeProgress("failed", total, total, f"Imported 0/{total} files (all failed)", error=True)
        else:
            self.writeProgress("complete", total, total,
                                f"Imported {succeededCount}/{total} files ({failedCount} failed)")

    # ---- stats -------------------------------------------------------------------------

    @staticmethod
    def _dateRangeToTimestamps(startDate: datetime.datetime | None, endDate: datetime.datetime | None):
        startTs = startDate.timestamp() if startDate else None
        endTs = endDate.timestamp() if endDate else None
        return startTs, endTs

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
        needs no changes)."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        plays = self.repo.getPlaysInRange(self.user, startTs, endTs, trackId=trackId, artistId=artistId,
                                           albumId=albumId)

        buckets = {}
        for play in plays:
            date = convertToDatetime(play["playedAt"])
            key = self._bucketKey(date, groupBy)
            bucket = buckets.setdefault(key, {"label": key, "totalTimeListened": 0, "plays": 0})
            bucket["totalTimeListened"] += play["timePlayed"]
            bucket["plays"] += 1

        if startDate is not None and endDate is not None:
            rangeStart, rangeEnd = startDate, endDate
        elif plays:
            playedDates = [convertToDatetime(p["playedAt"]) for p in plays]
            rangeStart = min(playedDates)
            rangeEnd = max(playedDates) + datetime.timedelta(seconds=1)
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
        same as getListeningTimeSeries's item filters."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        plays = self.repo.getPlaysInRange(self.user, startTs, endTs, trackId=trackId, artistId=artistId,
                                           albumId=albumId)
        grid = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        for play in plays:
            date = convertToDatetime(play["playedAt"])
            cell = grid[date.weekday()][date.hour]
            cell["totalTimeListened"] += play["timePlayed"]
            cell["plays"] += 1

        return grid

    def getArtistTrend(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, topN: int = 5, groupBy: str = "week") -> dict:
        """Per-bucket play counts for the topN most-played artists in the range, for
        an 'artist trend over time' line chart. Buckets are only the ones that have
        any activity - unlike getListeningTimeSeries, a trend line doesn't need a
        gap-filled timeline the way a bar chart's x-axis does."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        pairs = self.repo.getPlayArtistPairsInRange(self.user, startTs, endTs)

        totalPlaysByArtist = {}
        bucketedPairs = []
        for pair in pairs:
            date = convertToDatetime(pair["playedAt"])
            key = self._bucketKey(date, groupBy)
            name = pair["artistName"]
            bucketedPairs.append((key, name))
            totalPlaysByArtist[name] = totalPlaysByArtist.get(name, 0) + 1

        if not totalPlaysByArtist:
            return {"buckets": [], "series": []}

        topNames = [name for name, _ in sorted(totalPlaysByArtist.items(), key=lambda kv: kv[1], reverse=True)[:topN]]

        bucketKeys = sorted({key for key, _ in bucketedPairs})
        seriesData = {name: {key: 0 for key in bucketKeys} for name in topNames}
        for key, name in bucketedPairs:
            if name in seriesData:
                seriesData[name][key] += 1

        series = [{"name": name, "data": [seriesData[name][key] for key in bucketKeys]} for name in topNames]
        return {"buckets": bucketKeys, "series": series}

    def startListener(self, cookiesFile=None, email=None):
        if cookiesFile:
            self.cookiesFile = cookiesFile
        if email:
            self.email = email
        self.listener = self._withCookiesFile(lambda cf: Listener(cf, email=self.email))
        # onStale: the listener's own feed can go silently stale (see
        # spotifyListener.LISTENER_STALE_TIMEOUT_SECONDS) - rebuilding the whole
        # session here (fresh cookies file, fresh Listener, fresh websocket) is
        # the same recovery startListener() already does for an explicit
        # re-login (see app.py's _refresh_user_session), just triggered
        # automatically instead of by a user hitting /login again.
        self.listener.startListener_thread(
            callback=self._addToDatabaseFromListener,
            onStale=lambda: self.startListener(email=self.email),
        )

    def startAutoImporter(self):
        self.autoImporter.start()

    def isListenerLoggedIn(self):
        if self.listener == None:
            return False
        return self.listener.isLoggedIn()

    def stop(self):
        if self.listener is not None:
            self.listener.stop()
        self.autoImporter.wd.stop()


if __name__ == "__main__":

    manager = Database(user="Tzur")
    manager.startListener("cookies.json")
    manager.startAutoImporter()
    import pysole
    pysole.probe()

    # import SpotipyFree
    # sp = SpotipyFree.Spotify()
    # sp.login()

    # importFile = Path("importMe.json")
    # if importFile.exists():
    #     with importFile.open("r", encoding="utf-8") as f:
    #         historyPayload = json.load(f)
    #     manager.importSpotifyHistory(historyPayload)
