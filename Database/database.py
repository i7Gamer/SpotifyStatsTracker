from __future__ import annotations
import datetime
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
    from Database.utils import parseError, convertToDatetime, dateToString, startOfDay, startOfWeek
except ModuleNotFoundError:
    from Formatters.spotifyClient import Client
    from Importers.StreamingHistoryImporter import Importer
    from Importers.AutoImporter import AutoImporter
    from Listeners.spotifyListener import Listener
    from repository import Repository, IMAGE_KIND_TRACK, IMAGE_KIND_ARTIST, IMAGE_STATUS_OK, IMAGE_STATUS_FAILED
    from utils import parseError, convertToDatetime, dateToString, startOfDay, startOfWeek

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
        print(f"auto import filtering by {filterKeyword}")
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
                    print(f"Error adding track from listener: {parseError(e)}")

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
            print(f"Failed to download track {trackId}")
            return None

    def _ensureTrackMetadata(self, trackId: str) -> dict | None:
        track = self.repo.getTrack(trackId)
        if track is not None:
            return track
        print(f"Missing track metadata for {trackId}, downloading it")
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

    def _entryWithTrackMetadata(self, entry: dict) -> dict | None:
        track = self._ensureTrackMetadata(entry["id"])
        if track is None:
            return None
        meta = track.copy()
        meta["playedAt"] = entry["playedAt"]
        meta["timePlayed"] = entry["timePlayed"]
        meta["playedFrom"] = entry.get("playedFrom")
        return meta

    def _paginateEntries(self, entries: list) -> list:
        result = []
        for entry in entries:
            meta = self._entryWithTrackMetadata(entry)
            if meta is not None:
                result.append(meta)
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
            print(f"Error occurred while fetching playlist name for {playlistId} (probably due to playlist being private): {e}")
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
                print(f"Error fetching image from {url} (id={imgId}): {parseError(e)}")
            else:
                print(f"Error saving image (id={imgId}): {parseError(e)}")

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

    def lazyFetchArtistImage(self, artistId: str, imagePath: Path) -> bool:
        """Best-effort synchronous fetch of an artist's image scraped from their public
        Spotify page, used as a fallback for artists we never received image metadata
        for from the API. Deduplicated per artist id (via the same lock used by the
        rest of the image pipeline) so repeated requests for a still-missing image
        don't keep re-hitting Spotify. Returns True if the image exists on disk after
        this call, whether freshly fetched or already cached.
        """
        if imagePath.exists():
            return True
        if not artistId:
            return False

        with self._imageIdsLock:
            if artistId in self._artistImageLazyFetchAttempted:
                return imagePath.exists()
            self._artistImageLazyFetchAttempted.add(artistId)

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
            print(f"Failed to lazy load artist image for {artistId}: {parseError(e)}")
            return False

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
        print("Resorted Database")

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
                print(f"Import failed for file {index}/{total}: {parseError(e)}")

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
                       albumId: str | None = None) -> list:
        """Return songs sorted by `sortBy` with full song metadata and listen
        totals - sorted/paged in SQL via a single batched query (see
        Repository.getSongsPage) rather than hydrating every song ever played
        just to discard all but the requested page. `trackId`/`artistId`/
        `albumId` narrow this to a single song's stats, an artist's songs, or an
        album's songs (see Repository.getSongsPage)."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getSongsPage(self.user, startTs, endTs, sortBy=sortBy, limit=limit, offset=offset,
                                       trackId=trackId, artistId=artistId, albumId=albumId)

    def getSong(self, trackId: str) -> dict | None:
        """A single song's full metadata plus all-time listen totals - the
        song-detail page's lookup."""
        results = self.getSongsStats(sortBy="plays", limit=1, trackId=trackId)
        return results[0] if results else None

    def getSongsCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> int:
        """Number of distinct songs played in range - the paging counterpart to
        getSongsStats(), for computing total page count without fetching every
        song's metadata."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getSongsCount(self.user, startTs, endTs)

    def getPlayTotals(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> tuple[int, int]:
        """(play count, total time listened) across the whole range - cheap
        aggregate that doesn't require fetching per-song metadata."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getPlayTotals(self.user, startTs, endTs)

    def getAlbumsStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                        sortBy: str = "plays", limit: int | None = None, offset: int = 0,
                        albumId: str | None = None) -> list:
        """Return albums sorted by `sortBy` with aggregated listen totals - sorted/
        paged in SQL via a single batched query (see Repository.getAlbumsPage),
        mirroring getSongsStats(). `albumId` narrows this to a single album's
        stats."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getAlbumsPage(self.user, startTs, endTs, sortBy=sortBy, limit=limit, offset=offset,
                                        albumId=albumId)

    def getAlbum(self, albumId: str) -> dict | None:
        """A single album's aggregate stats - the album-detail page's lookup."""
        results = self.getAlbumsStats(sortBy="plays", limit=1, albumId=albumId)
        return results[0] if results else None

    def getAlbumsCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> int:
        """Number of distinct albums played in range - the paging counterpart to
        getAlbumsStats(), for computing total page count without fetching every
        album's metadata."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getAlbumsCount(self.user, startTs, endTs)

    def getTopAlbums(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, by: str = "plays",
                      limit: int | None = None, offset: int = 0) -> list:
        # Albums are sorted/paged in SQL (see getAlbumsStats -> Repository.getAlbumsPage)
        # rather than re-sorted here in Python, for the same reason getTopSongs is.
        return self.getAlbumsStats(startDate, endDate, sortBy=by, limit=limit, offset=offset)

    def getArtistsStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                         artistId: str | None = None) -> list:
        """Return artists sorted by total plays with aggregated data and listen
        totals. `artistId` narrows this to a single artist's stats."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getArtistAggregates(self.user, startTs, endTs, artistId=artistId)

    def getArtist(self, artistId: str, startDate: datetime.datetime = None,
                  endDate: datetime.datetime = None) -> dict | None:
        """A single artist's aggregate stats - the artist-detail page's lookup."""
        results = self.getArtistsStats(startDate, endDate, artistId=artistId)
        return results[0] if results else None

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
        currentTopArtists = self.getTopArtists(startDate=startDate, endDate=endDate, by="totalTimeListened")

        return {"currentTopSongs": currentTopSongs,
                "currentTopArtists": currentTopArtists,
                "totalSongsPlayed": totalSongsPlayed,
                "totalDurationMs": totalDurationMs,
                "previousSongsPlayed": previousSongsPlayed,
                "previousDurationMs": previousDurationMs
                }

    def _sortTopStats(self, items, compareKeys, by: str = "plays") -> list:
        """
        Sorts songs within a date range.
        'plays' and 'totalTimeListened' are sorted descending (highest first).
        'name' is sorted ascending (A to Z).
        """

        reverse=True
        if by == "name":
            reverse=False

        return sorted(
            items,
            key=lambda item: tuple(item[key] for key in compareKeys),     #< the tuple acts as a tie breaker, if two elements 'by' are the same, it compares the 'totalTimeListened', then 'name'
            reverse=reverse
        )

    def getTopSongs(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, by: str = "plays",
                     limit: int | None = None, offset: int = 0) -> list:
        # Songs are sorted/paged in SQL (see getSongsStats -> Repository.getSongsPage)
        # rather than re-sorted here in Python: once pagination is pushed down to
        # the database, re-sorting an already-LIMIT-ed page can't reconstruct
        # global rank, so SQL ordering must be the single source of truth.
        return self.getSongsStats(startDate, endDate, sortBy=by, limit=limit, offset=offset)

    def getTopArtists(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, by: str = "plays") -> list:
        artists = self.getArtistsStats(startDate, endDate)
        compKeys = (by, "totalTimeListened", "name")
        return self._sortTopStats(artists, compKeys, by)

    def _bucketKey(self, date: datetime.datetime, groupBy: str) -> str:
        if groupBy == "week":
            return dateToString(startOfWeek(date))
        elif groupBy == "hour":
            return date.strftime("%Y-%m-%d %H:00")
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
            step = datetime.timedelta(days=7)
            cursor = startOfWeek(rangeStart)
        elif groupBy == "hour":
            step = datetime.timedelta(hours=1)
            cursor = rangeStart.replace(minute=0, second=0, microsecond=0)
        else:
            step = datetime.timedelta(days=1)
            cursor = startOfDay(rangeStart)

        result = []
        while cursor < rangeEnd:
            key = self._bucketKey(cursor, groupBy)
            result.append(buckets.get(key, {"label": key, "totalTimeListened": 0, "plays": 0}))
            cursor += step
        return result

    def getHourOfDayHeatmap(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> list:
        """7x24 grid (rows Monday=0..Sunday=6, columns hour-of-day 0-23) of total
        listening time and play count, for a 'when do I listen' heatmap."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        plays = self.repo.getPlaysInRange(self.user, startTs, endTs)
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
        self.listener.startListener_thread(callback=self._addToDatabaseFromListener)

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
