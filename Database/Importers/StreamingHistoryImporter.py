import csv
import json
import datetime
import hashlib
import logging
import SpotipyFree
import concurrent.futures
import threading

logger = logging.getLogger(__name__)

try:
    from Database.Formatters.spotifyClient import Client
    from Database.db import SYNTHETIC_FALLBACK_REASON
    from Database.utils import timeToInt, timeToIntUTC, parseError
except ModuleNotFoundError:
    from Formatters.spotifyClient import Client
    from db import SYNTHETIC_FALLBACK_REASON
    from utils import timeToInt, timeToIntUTC, parseError


class Importer:
    # 1000 allows for frequent progress bar updates in the UI and batches API pre-fetches
    # to avoid rate limits/network blocking without long delays.
    CHUNK_SIZE = 1000
    MAX_PREFETCH_WORKERS = 14
    # Spotify's exported history includes skips recorded with only a fraction of
    # a second played - below this threshold it's not a real listen and must not
    # be imported as one.
    MIN_TIME_PLAYED_MS = 1000

    # Error-text markers for lookup failures that are likely temporary (network,
    # auth/session, rate limiting). Synthesizing a fallback record for these would
    # freeze bad data into the shared catalog permanently, so the play is skipped
    # instead - a later re-import retries cleanly, and plays already imported are
    # deduped by the plays UNIQUE constraint. Everything else (no search results,
    # 404s) is treated as the track genuinely being gone from Spotify.
    TRANSIENT_LOOKUP_ERROR_MARKERS = (
        "429", "rate limit", "timeout", "timed out", "connection",
        "session", "unauthorized", "forbidden", "temporarily",
        "500", "502", "503", "504",
    )

    def __init__(self, cookiesFile=None, email=None):
        self.sp = SpotipyFree.Spotify(cookiesFile=cookiesFile, email=email)

    def _searchForSong(self, name, artist):
        query = f"track:{name} artist:{artist}"
        track = self.sp.search(query, type="track", limit=1)["tracks"]["items"][0]
        # track = self.sp.track(track["external_urls"]["spotify"])
        return track

    def _fetchTrackMeta(self, name, artist, trackUri):
        """ Fetch raw track metadata by URI, falling back to a name/artist search. """
        if trackUri:
            try:
                return self.sp.track(trackUri)
            except Exception:
                return self._searchForSong(name=name, artist=artist)
        return self._searchForSong(name=name, artist=artist)

    def _convertToList(self, export):
        export = export.lstrip("\ufeff")
        if export.lstrip().startswith("FILE_PATH,"):
            return export.splitlines()[1:], "musicoletPremium"
        try:
            export = json.loads(export)
            if "msPlayed" in export[0]:   #< Acount export
                return export, "spotifyAcountExport"
            if "ts" in export[0]:         #< Extended export
                return export, "spotifyExtendedExport"
        except:
            pass
        return [], "None"
    
    def importHistory(self, parsedHistory, known, exportType, progressCallback=None):
        if len(parsedHistory) == 0:
            return []
        if exportType == "spotifyAcountExport":
            return self.importAcountHistory(parsedHistory, known=known, progressCallback=progressCallback)
        if exportType == "spotifyExtendedExport":
                return self.importExtendedHistory(parsedHistory, known=known, progressCallback=progressCallback)
        if exportType == "musicoletPremium":
            return self.importMusicoletCSVExport(parsedHistory, known=known, progressCallback=progressCallback)
        return []

    def buildKnownIndex(self, knownTrack):
        index = {}
        for item in knownTrack:
            index[item["id"]] = item
            if len(item["artists"]) == 0:
                continue
            index[item["name"]+item["artists"][0]["name"]] = item
        return index

    def _parseHistory(self, dataFunction, history):
        parsedItems = []
        for item in history:
            try:
                name, artist, startTimestamp, timePlayed, trackUri = dataFunction(item)
                if timePlayed < self.MIN_TIME_PLAYED_MS:
                    continue
                parsedItems.append((name, artist, startTimestamp, timePlayed, trackUri))
            except Exception:
                continue
        return parsedItems

    def _resolveKnownKey(self, trackUri, name, artist, known):
        """ Return whichever of trackUri or the name+artist key is already cached in
        `known`, preferring trackUri. A trackUri missing from the cache still falls
        back to the name+artist key (e.g. a reissue/remaster URI for a song already
        cached under its name+artist) rather than being treated as unmatched. """
        if trackUri and trackUri in known:
            return trackUri
        idKey = name + artist if name and artist else None
        if idKey and idKey in known:
            return idKey
        return None

    def _identifyMissingTracks(self, chunk, known):
        missingTracks = {}
        for name, artist, startTimestamp, timePlayed, trackUri in chunk:
            if self._resolveKnownKey(trackUri, name, artist, known) is not None:
                continue

            if trackUri:
                missingTracks[trackUri] = (name, artist, trackUri)
            elif name and artist:
                missingTracks[name + artist] = (name, artist, None)
        return missingTracks

    def _prefetchMissingTracks(self, missingTracks, chunkStart, totalItems, known, progressCallback):
        totalMissing = len(missingTracks)
        fetchedCount = 0
        
        def fetchOne(key, info):
            name, artist, trackUri = info
            meta = None
            try:
                meta = self._fetchTrackMeta(name, artist, trackUri)
            except Exception as e:
                logger.warning("Error fetching %s by %s: %s", name, artist, parseError(e))
            return key, meta

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.MAX_PREFETCH_WORKERS) as executor:
            futures = {executor.submit(fetchOne, k, val): k for k, val in missingTracks.items()}
            for future in concurrent.futures.as_completed(futures):
                fetchedCount += 1
                if progressCallback:
                    progressCallback(
                        "running", 
                        chunkStart + fetchedCount, 
                        totalItems, 
                        f"Pre-fetching batch metadata ({fetchedCount}/{totalMissing})..."
                    )
                
                try:
                    key, meta = future.result()
                    if meta:
                        formatted = Client.formatTrack(meta, embedPlaybackInfo=False)
                        known[formatted["id"]] = formatted
                        if key != formatted["id"]:
                            known[key] = formatted
                        if len(formatted["artists"]) > 0:
                            nameArtistKey = formatted["name"] + formatted["artists"][0]["name"]
                            known[nameArtistKey] = formatted
                except Exception as e:
                    logger.error("Error saving pre-fetched track: %s", parseError(e))

    def _createSyntheticTrack(self, name: str, artist: str, trackUri: str | None, timePlayed: int) -> dict:
        # Determine track, album, and artist IDs
        if trackUri:
            track_id = trackUri
        else:
            # Generate deterministic unique ID based on name and artist
            track_id = hashlib.md5(f"{name}::{artist}".encode("utf-8")).hexdigest()

        album_id = f"album_{track_id}"
        artist_id = f"artist_{track_id}"

        # urls stay empty (like imageUrl) - these entities don't exist on Spotify,
        # and every template guards its "Open in Spotify" link on a truthy url.
        artists = [
            {
                "name": artist,
                "url": "",
                "imageUrl": "",
                "imageId": artist_id,
                "id": artist_id,
            }
        ]

        album = {
            "name": name,  # Fallback: use track name as album name
            "url": "",
            "id": album_id,
            "imageId": album_id,
            "imageUrl": "",
            "totalTracks": 1,
            "releaseDate": 0.0,
        }

        return {
            "name": name,
            "releaseDate": 0.0,
            "id": track_id,
            "url": "",
            "artists": artists,
            "album": album,
            "imageUrl": "",
            "imageId": album_id,
            "duration": timePlayed,  # Use play time as default duration
            "explicit": False,
            "isrc": "",
            "discNumber": 1,
            "trackNumber": 1,
            "created_reason": SYNTHETIC_FALLBACK_REASON,
        }

    def _isTransientLookupError(self, e: Exception) -> bool:
        if isinstance(e, (ConnectionError, TimeoutError)):
            return True
        errorText = str(e).lower()
        return any(marker in errorText for marker in self.TRANSIENT_LOOKUP_ERROR_MARKERS)

    def _processPlay(self, item, known):
        name, artist, startTimestamp, timePlayed, trackUri = item
        try:
            matchedId = self._resolveKnownKey(trackUri, name, artist, known)

            if matchedId:
                meta = Client.embedPlayInfo(known[matchedId].copy(), startTimestamp, timePlayed)
            else:
                if not name or not artist:
                    return None

                try:
                    meta = self._fetchTrackMeta(name, artist, trackUri)
                    base = Client.formatTrack(meta, embedPlaybackInfo=False)
                except Exception as e:
                    if self._isTransientLookupError(e):
                        # Don't freeze a synthetic record into the catalog over what's
                        # likely a temporary failure - skip the play; a re-import
                        # after the outage retries it (existing plays dedup).
                        logger.warning("Transient Spotify lookup failure for %s by %s (URI: %s) - skipping play, re-import to retry: %s", name, artist, trackUri, parseError(e))
                        return None
                    # Fallback to synthetic track
                    logger.info("Spotify lookup failed for %s by %s (URI: %s), using synthetic record: %s", name, artist, trackUri, parseError(e))
                    base = self._createSyntheticTrack(name, artist, trackUri, timePlayed)

                known[base["id"]] = base
                if trackUri:
                    known[trackUri] = base
                known[name + artist] = base
                meta = Client.embedPlayInfo(base.copy(), startTimestamp, timePlayed)

            return meta
        except Exception as e:
            logger.error("Error processing item: %s", parseError(e))
            return None
        
    def _import(self, dataFunction, history, known=[], progressCallback=None):
        known = self.buildKnownIndex(known)
        
        parsedItems = self._parseHistory(dataFunction, history)
        totalItems = len(parsedItems)
        if totalItems == 0:
            return
            
        for chunkStart in range(0, totalItems, self.CHUNK_SIZE):
            chunk = parsedItems[chunkStart : chunkStart + self.CHUNK_SIZE]
            
            missingTracks = self._identifyMissingTracks(chunk, known)
            
            # Fetch missing tracks in this chunk concurrently
            if missingTracks:
                self._prefetchMissingTracks(
                    missingTracks, 
                    chunkStart, 
                    totalItems, 
                    known, 
                    progressCallback
                )
            
            # Yield items from the current chunk (fully in-memory now)
            for item in chunk:
                meta = self._processPlay(item, known)
                if meta:
                    yield meta

    def importAcountHistory(self, history, known=[], progressCallback=None):
        def dataFunction(item):
            # endTime is documented by Spotify as UTC with no timezone marker on
            # the wire - timeToInt would otherwise interpret it as local time.
            endTimestamp = timeToIntUTC(item["endTime"])
            timePlayed = item["msPlayed"]

            startTimestamp = endTimestamp-timePlayed//1000
            name=item["trackName"]
            artist=item["artistName"]
            return name, artist, startTimestamp, timePlayed, None
        
        yield from self._import(dataFunction, history, known, progressCallback)

    def importExtendedHistory(self, history, known=[], progressCallback=None):
        def dataFunction(item):
            ts = item["ts"]
            endTimestamp = timeToInt(ts)
            timePlayed = item.get("ms_played", 0)
            startTimestamp = endTimestamp - timePlayed // 1000

            name = item["master_metadata_track_name"]
            artist = item["master_metadata_album_artist_name"]
            uri = item.get("spotify_track_uri")
            trackUri = uri.split(":")[-1] if uri else None
            return name, artist, startTimestamp, timePlayed, trackUri
        
        yield from self._import(dataFunction, history, known, progressCallback)

    # Musicolet's CSV only carries an aggregate play count per track, not
    # individual play timestamps. Synthetic per-play timestamps are anchored
    # here (a fixed epoch) rather than at now() - re-importing the same file
    # then reproduces the exact same (track, played_at) pairs and is silently
    # deduped by the plays.UNIQUE constraint instead of creating a fresh batch
    # of fake plays every time. An updated file with a higher play count for a
    # track only adds the new tail of plays.
    MUSICOLET_SYNTHETIC_TIME_ANCHOR = datetime.datetime(2000, 1, 1)

    def importMusicoletCSVExport(self, rows, known=[], progressCallback=None):
        def expand(rows):
            ### Data formatted in: FILE_PATH,TITLE,ARTIST,ALBUM,ALBUM_ARTIST,COMPOSER,GENRE,YEAR,DURATION_MS,PLAY_COUNT
            NAME = 1
            ARTISTS = 2
            DURATION_MS = 8
            PLAYCOUNT = 9

            formatedData = []
            reader = csv.reader(rows)

            for song in reader:
                if not song:
                    continue

                try:
                    name = song[NAME]
                    mainArtist = song[ARTISTS].split("/")[0]
                    timePlayed = int(song[DURATION_MS])
                    playCount = int(song[PLAYCOUNT])

                    trackTime = self.MUSICOLET_SYNTHETIC_TIME_ANCHOR
                    for _ in range(playCount):
                        startTimestamp = trackTime.strftime("%Y-%m-%d %H:%M:%S")
                        formatedData.append((
                            name,
                            mainArtist,
                            startTimestamp,
                            timePlayed
                        ))
                        trackTime += datetime.timedelta(milliseconds=timePlayed)

                except (IndexError, ValueError) as e:
                    continue

            return formatedData

        def dataFunction(item):
            name, mainArtist, startTimestamp, timePlayed = item
            return name, mainArtist, startTimestamp, timePlayed, None

        yield from self._import(dataFunction, expand(rows), known, progressCallback)