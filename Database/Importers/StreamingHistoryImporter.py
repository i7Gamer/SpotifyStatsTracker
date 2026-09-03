# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import csv
import json
import datetime
import hashlib
import logging
import concurrent.futures

logger = logging.getLogger(__name__)

try:
    from Database.Formatters.spotifyClient import Client
    from Database.db import (SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON, SKIP_THRESHOLD_MS,
                             looksLikeSpotifyTrackId)
    from Database.utils import timeToInt, timeToIntUTC, parseError, convertToDatetime, getTimezone
    from Database.Spotify import Spotify
except ModuleNotFoundError:
    from Formatters.spotifyClient import Client
    from db import (SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON, SKIP_THRESHOLD_MS,
                    looksLikeSpotifyTrackId)
    from utils import timeToInt, timeToIntUTC, parseError, convertToDatetime, getTimezone
    from Spotify import Spotify


def _knownNameKey(name: str, artist: str) -> str:
    """Cache key for the name+artist lookup index. The separator matters: bare
    concatenation made ("Al", "Green") and ("A", "lGreen") the same key, so one
    track could be silently matched to the other's catalog row. Mirrors
    _createSyntheticTrack's own "name::artist" pairing."""
    return f"{name}::{artist}"


class MusicoletExpansionTooLargeError(Exception):
    """A Musicolet CSV describes more plays than the importer will expand.

    Its own type, not ValueError: _expandMusicoletRows' per-row handler catches
    (IndexError, ValueError) and turns them into a dropped row, which is exactly
    the wrong answer here - this is a statement about the FILE, and it has to
    reach the caller rather than be counted as one bad line."""


class Importer:  #< one export file -> plays + track metadata, via cache, URI lookup, or name search
    # 1000 allows for frequent progress bar updates in the UI and batches API pre-fetches
    # to avoid rate limits/network blocking without long delays.
    CHUNK_SIZE = 1000
    MAX_PREFETCH_WORKERS = 14
    # A file the format sniffer typed wrongly makes EVERY entry unreadable, so
    # the per-entry warning is capped and the droppedMalformed counter carries
    # the total (see _parseHistory).
    MAX_MALFORMED_ENTRY_LOG_LINES = 5
    # Entries shorter than this fixed floor are yielded tagged isSkip=True - the
    # DB writer records them into plays as is_skip=1 and has them bypass near-time
    # play matching. This is the fixed import-dedup floor, not the admin-tunable
    # stats threshold (see Database/db.py). Only negative durations are dropped.
    SKIP_THRESHOLD_MS = SKIP_THRESHOLD_MS

    # offline_timestamp is seconds in some export eras and milliseconds in
    # others - values above this cutoff (~5138 CE as seconds) can only be ms.
    OFFLINE_TIMESTAMP_MS_CUTOFF = 1e11
    # Sanity floor for offline_timestamp: before 2006 (Spotify's founding era)
    # it can't be a real play time - fall back to the ts-derived start. Applies
    # to that field only, not to a play's start time in general - see
    # _plausibleStart for why the general bound has to be lower.
    MIN_PLAUSIBLE_PLAY_TIMESTAMP = int(datetime.datetime(2006, 1, 1, tzinfo=datetime.timezone.utc).timestamp())

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
        self.sp = Spotify(cookiesFile=cookiesFile, email=email)

    def _searchForSong(self, name, artist) -> dict:
        query = f"track:{name} artist:{artist}"   #< Spotify's fielded-search syntax
        items = self.sp.search(query, type="track", limit=1)["tracks"]["items"]
        if not items:
            # Static message on purpose: name/artist are user data and a track
            # literally named "Connection Timeout" would otherwise match
            # TRANSIENT_LOOKUP_ERROR_MARKERS and drop the play instead of
            # synthesizing a fallback record. Callers log name/artist themselves.
            raise ValueError("no search results")
        return items[0]

    def _fetchTrackMeta(self, name, artist, trackUri):
        """ Fetch raw track metadata by URI, falling back to a name/artist search. """
        if trackUri:
            try:
                return self.sp.track(trackUri)
            except Exception:
                return self._searchForSong(name=name, artist=artist)
        return self._searchForSong(name=name, artist=artist)

    def _convertToList(self, export):
        """(parsed entries, export type). "emptyExport" marks a file that IS a
        recognized export with nothing in it (a valid but empty JSON list) -
        distinct from "None", which means the content matched no known export
        format at all (corrupt, truncated mid-copy, or the wrong file) and
        which Database.importHistory treats as a failed import rather than
        silently succeeding."""
        export = export.lstrip("\ufeff")
        # ONE normalized string for both the sniff and the slice. They used to
        # disagree - the sniff stripped, the slice did not - so a leading
        # newline had splitlines()[1:] drop the BLANK line and keep the HEADER
        # as row 0. That row fails int(DURATION_MS) and books a
        # droppedMalformed, which is an UNREADABLE_DROP_STAT_KEY, so the
        # overwrite import refused the whole batch and told the user to
        # re-export a file that was fine. See _expandMusicoletRows' docstring,
        # which states this cannot happen.
        csvText = export.lstrip()
        if csvText.startswith("FILE_PATH,"):   #< Musicolet's CSV header
            return csvText.splitlines()[1:], "musicoletPremium"   #< CSV: drop the header row

        try:
            entries = json.loads(export)
        except Exception:  # noqa: S110 - the "None" classification below IS the error report;
            entries = None  # the caller surfaces it to the user as an unreadable-export message
        if isinstance(entries, list):
            if not entries:
                return [], "emptyExport"
            first = entries[0]
            if isinstance(first, dict):   #< both Spotify exports are lists of objects
                if "msPlayed" in first:
                    return entries, "spotifyAcountExport"   #< the Account export's field naming
                if "ts" in first:
                    return entries, "spotifyExtendedExport"
        return [], "None"   #< unrecognized: importHistory surfaces this as an unreadable export

    def importHistory(self, parsedHistory, known, exportType, progressCallback=None, stats=None):
        importers = {
            "spotifyAcountExport": self.importAcountHistory,
            "spotifyExtendedExport": self.importExtendedHistory,
            "musicoletPremium": self.importMusicoletCSVExport,
        }
        importerForType = importers.get(exportType)
        if not parsedHistory or importerForType is None:
            return []
        return importerForType(parsedHistory, known=known, progressCallback=progressCallback, stats=stats)

    def buildKnownIndex(self, knownTrack) -> dict:
        """Cached tracks indexed twice over: by id, and by name+first-artist
        (for entries whose export row carries no URI)."""
        knownIndex = {}
        for item in knownTrack:
            if not item.get("name"):
                # Stored from a blanked (region-restricted) lookup before the export
                # overlay existed - leave it out of the cache so a re-import
                # re-fetches it and heals the record.
                continue
            knownIndex[item["id"]] = item
            if item["artists"]:
                knownIndex[_knownNameKey(item["name"], item["artists"][0]["name"])] = item
        return knownIndex

    def _parseHistory(self, dataFunction, history, stats=None):
        """Turn raw export entries into the tuples the rest of the import works
        on, counting the ones that can't be turned into anything.

        Every drop below this layer is counted (droppedNoTrack /
        droppedTransient / droppedUnexpected); this one used to `continue` with
        no counter, no log and no `stats` parameter to bump. That is the
        uncounted drop this codebase treats as data loss: the overwrite import
        decides whether it may delete a covered range by asking whether every
        parsed play survived staging, and an entry that never parsed is
        invisible to that question - so its play could be deleted and never
        rebuilt, under a "complete" message.

        The two failure kinds are counted apart because they mean different
        things to that decision:

        droppedMalformed - the entry raised. A key the format guarantees is
            absent, a null where a number belongs. Rare and exceptional: a
            podcast row PARSES (Spotify sends master_metadata_track_name as
            null, not missing) and is dropped later as droppedNoTrack, so
            treating malformed as a reason to abort does not penalise the
            episodes most real exports contain.
        droppedNegativeTime - a pre-existing, deliberate sanity filter. Counted
            for visibility only; folding it into the above would newly abort
            overwrite imports for exports that have always been importable.
        droppedImplausibleTime - the same kind of filter, for a start time that
            cannot be a real play (see _plausibleStart), and counted apart from
            droppedMalformed for exactly the reason above: it is reachable from
            ordinary user files, and one such row must not abort an overwrite
            import of an otherwise readable 100k-entry export."""
        parsedItems = []
        loggedMalformed = 0
        for index, item in enumerate(history):
            # Counted here rather than derived from len(history) by the caller:
            # the Musicolet path expands its CSV rows into many entries before
            # this loop, so the caller's row count is not the entry count, and
            # "was NOTHING readable?" needs the two compared exactly.
            self._bumpStat(stats, "entriesSeen")
            try:
                parsed = dataFunction(item)
                # albumName (6th) and extras (7th) are optional - account
                # exports and older callers produce 5/6-tuples.
                name, artist, startTimestamp, timePlayed, trackUri = parsed[:5]
                albumName = parsed[5] if len(parsed) > 5 else None
                extras = parsed[6] if len(parsed) > 6 else None
                playedFrom = parsed[7] if len(parsed) > 7 else None   #< extended export only
                if timePlayed < 0:
                    self._bumpStat(stats, "droppedNegativeTime")
                    continue
                if not self._plausibleStart(startTimestamp):
                    self._bumpStat(stats, "droppedImplausibleTime")
                    continue
                parsedItems.append((name, artist, startTimestamp, timePlayed, trackUri, albumName, extras, playedFrom))
            except Exception as e:
                self._bumpStat(stats, "droppedMalformed")
                # Capped: a misclassified file makes EVERY entry fail, and one
                # log line per entry would bury the rest of the import. The
                # counter carries the total. Position and error only - export
                # rows are the user's listening history.
                if loggedMalformed < self.MAX_MALFORMED_ENTRY_LOG_LINES:
                    loggedMalformed += 1
                    logger.warning("Skipping unreadable export entry at position %d: %s",
                                   index, parseError(e))
        return parsedItems

    def _plausibleStart(self, startTimestamp) -> bool:
        """Could this be the start of a real play?

        Nothing else on the way in says no: timeToInt answers 0 for a stamp it
        cannot read (a null `ts`, an empty endTime, a hand-edited file), the
        entry function then subtracts the play's own length from it, and the
        result - a negative number - was written to plays.played_at as a listen
        dated 1969, where it sits in the streak calendar and the year list
        forever.

        Read through timeToInt for the same reason coverage() does: a start is
        not always a number here. The Musicolet path formats its synthetic
        stamps as strings, and a caller may pass a datetime.

        The floor is the epoch, NOT MIN_PLAUSIBLE_PLAY_TIMESTAMP, even though
        that constant is right there and reads like the obvious bound: those
        Musicolet stamps are anchored at MUSICOLET_SYNTHETIC_TIME_ANCHOR
        (2000-01-01, deliberately fixed so re-importing the same file is a
        no-op), so a 2006 floor would drop every play from that format. What
        this rejects is the unreadable stamp and what derives from it, which is
        the whole of the observed defect - a real-looking but wrong year is the
        user's own data and none of a sanity filter's business.

        A floor only: a stamp in the future is the LISTENER's contamination
        signal, and an export is a record of the past that a wrong timezone can
        legitimately push a few hours ahead of the clock reading it here."""
        return timeToInt(startTimestamp) > 0

    def _resolveKnownKey(self, trackUri, name, artist, known):
        """ Return whichever of trackUri or the name+artist key is already cached in
        `known`, preferring trackUri. A trackUri missing from the cache still falls
        back to the name+artist key (e.g. a reissue/remaster URI for a song already
        cached under its name+artist) rather than being treated as unmatched. """
        if trackUri and trackUri in known:
            return trackUri
        idKey = _knownNameKey(name, artist) if name and artist else None
        if idKey and idKey in known:
            return idKey
        return None

    def _identifyMissingTracks(self, chunk, known):
        missingTracks = {}
        for entry in chunk:
            name, artist, _, _, trackUri, albumName = entry[:6]
            if self._resolveKnownKey(trackUri, name, artist, known) is not None:
                continue

            if trackUri:
                missingTracks[trackUri] = (name, artist, trackUri, albumName)
            elif name and artist:
                # _knownNameKey, not concatenation: this key is what the
                # prefetch stores its result under, and _resolveKnownKey is what
                # looks it up - built two different ways, nothing was ever found
                # and every URI-less entry got fetched twice.
                missingTracks[_knownNameKey(name, artist)] = (name, artist, None, albumName)
        return missingTracks

    def _prefetchMissingTracks(self, missingTracks, chunkStart, totalItems, known, progressCallback):
        totalMissing = len(missingTracks)
        fetchedCount = 0

        def fetchOne(key, info):
            name, artist, trackUri, albumName = info
            try:
                return key, self._fetchTrackMeta(name, artist, trackUri), None
            except Exception as e:
                logger.warning("Error fetching %s by %s: %s", name, artist, parseError(e))
                # The error travels with the result: whether this track is
                # UNRESOLVABLE or merely unreachable right now decides whether
                # the answer can be cached (below), and only the exception says
                # which - the same question _processPlay asks one layer down.
                return key, None, e

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
                    key, meta, error = future.result()
                    name, artist, trackUri, albumName = missingTracks[key]
                    if meta:
                        formatted = Client.formatTrack(meta, embedPlaybackInfo=False)
                        formatted = self._overlayExportMetadata(formatted, name, artist, albumName)
                        known[formatted["id"]] = formatted
                        if key != formatted["id"]:
                            known[key] = formatted
                        if len(formatted["artists"]) > 0:
                            known[_knownNameKey(formatted["name"], formatted["artists"][0]["name"])] = formatted
                    elif (name and artist and error is not None
                          and not self._isTransientLookupError(error)):
                        # Spotify does not have this track, and asking again in a
                        # second will not change that - so answer it HERE, the
                        # way _processPlay would have. Without this the failure
                        # was recorded nowhere, `known` still had no entry, and
                        # the same track was looked up a second time, serially,
                        # once per unresolvable track - double the volume against
                        # a catalog quota measured in hundreds per day.
                        #
                        # duration 0, not the play's length: the play is not in
                        # scope here, and _processPlay's synthetic branch already
                        # raises a synthetic record's duration to the longest
                        # play it sees, which is the same value it would have
                        # started from.
                        #
                        # `name and artist` is _processPlay's own precondition
                        # for inventing a record, and it has to hold here too: a
                        # row with a uri but no name (a podcast row, an edited
                        # file) belongs in droppedNoTrack, and a synthetic built
                        # for it carries name=None into a NOT NULL column - one
                        # counted drop turning into a failed import.
                        synthetic = self._createSyntheticTrack(name, artist, trackUri, 0,
                                                               albumName=albumName)
                        known[synthetic["id"]] = synthetic
                        if key != synthetic["id"]:
                            known[key] = synthetic
                        known[_knownNameKey(name, artist)] = synthetic
                    #< a TRANSIENT error is deliberately left out of `known`: it
                    #  says nothing about whether the track exists, so the
                    #  per-play path must be free to try again (and a
                    #  still-failing one is counted as droppedTransient there)
                except Exception as e:
                    logger.error("Error saving pre-fetched track: %s", parseError(e))

    def _createSyntheticTrack(self, name: str, artist: str, trackUri: str | None, timePlayed: int,
                               albumName: str | None = None) -> dict:
        # Determine track, album, and artist IDs
        if trackUri:
            track_id = trackUri
        else:
            # Generate deterministic unique ID based on name and artist.
            # usedforsecurity=False: this is a surrogate key, never a security
            # primitive, and the flag keeps md5 callable on FIPS-mode hosts
            # (where the default constructor raises) without altering the
            # digest - the ids below are already persisted in user databases.
            #
            # Stripped for the same reason _resolveExportArtist strips: padding
            # is not an identity, and hashing it verbatim fabricated a second
            # track for the same song. The "::" separator still does its own
            # job (see _knownNameKey) - stripping the ends cannot merge
            # ("Al", "Green") into ("A", "lGreen").
            track_id = hashlib.md5(
                f"{name.strip()}::{artist.strip()}".encode("utf-8"), usedforsecurity=False).hexdigest()

        album_id = f"album_{track_id}"

        # With a real URI the track's Spotify page still exists (just unplayable),
        # so the link stays useful and is kept. Without one, the md5-based id points
        # at nothing - the url stays empty (like imageUrl), and every template guards
        # its "Open in Spotify" link on a truthy url. The fabricated album_ id never
        # existed on Spotify, so its url always stays empty.
        track_url = f"https://open.spotify.com/track/{track_id}" if trackUri else ""

        artists = [self._resolveExportArtist(artist)]

        album = {
            "name": albumName or name,  #< prefer the export's album name, fall back to the track name
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
            "url": track_url,
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

    def _buildCatalogArtistsByName(self, knownIndex: dict) -> dict:
        """Real (non-fabricated) artists from the seeded catalog, keyed by lowercase
        name - lets fallback records reuse the true artist id/link when the same
        artist is already known from other tracks, instead of fabricating one."""
        artistsByName = {}
        for track in knownIndex.values():
            for artistEntry in track.get("artists") or []:
                artistId = artistEntry.get("id") or ""
                artistName = (artistEntry.get("name") or "").strip().lower()
                if not artistName or not artistId or artistId.startswith("artist_"):
                    continue
                artistsByName.setdefault(artistName, {
                    "id": artistId,
                    "name": artistEntry["name"],
                    "url": artistEntry.get("url", ""),
                    "imageUrl": artistEntry.get("imageUrl", ""),
                    "imageId": artistEntry.get("imageId"),
                })
        return artistsByName

    def _resolveExportArtist(self, artist: str) -> dict:
        """Prefer the real catalog artist with this name (keeps stats grouped and
        the real Spotify link); fabricate a deterministic name-keyed entry otherwise.

        Stripped BEFORE hashing, not just before the lookup. The two disagreed:
        the catalog was searched for "queen" while the fallback id hashed
        "Queen " verbatim, so a padded spelling both missed the real artist and
        fabricated a second row - one artist's plays split across two ids that
        no page adds back together. The name is stored stripped for the same
        reason it is hashed stripped: it is the label every artist page shows."""
        normalized = artist.strip()
        catalogArtist = getattr(self, "_catalogArtistsByName", {}).get(normalized.lower())
        if catalogArtist:
            return dict(catalogArtist)
        # usedforsecurity=False for the same reason as _createSyntheticTrack's
        # id: a deterministic surrogate key, not a security primitive.
        artist_id = f"artist_{hashlib.md5(normalized.encode('utf-8'), usedforsecurity=False).hexdigest()}"
        return {
            "name": normalized,
            "url": "",
            "imageUrl": "",
            "imageId": artist_id,
            "id": artist_id,
        }

    def _overlayExportMetadata(self, base: dict, name: str, artist: str, albumName: str | None) -> dict:
        """Spotify blanks name/duration and reports the generic "Various Artists"
        profile for region-restricted tracks (playability COUNTRY_RESTRICTED) while
        still returning the real track and album ids. Keep the real ids/links but
        fill the blanked fields from the export's own data, and tag the record so
        the UI shows a "May be unavailable" badge."""
        if base.get("name") or not name:
            return base

        base["name"] = name
        base["created_reason"] = RESTRICTED_FALLBACK_REASON

        # The returned artist is untrustworthy on blanked tracks (Spotify reports
        # the generic Various Artists profile) - replace it with the export's
        # artist unless it already matches, reusing the real catalog artist id
        # when the same artist is known from other tracks.
        returnedArtists = base.get("artists") or []
        returnedArtistName = (returnedArtists[0].get("name") or "").strip().lower() if returnedArtists else ""
        if artist and returnedArtistName != artist.strip().lower():
            base["artists"] = [self._resolveExportArtist(artist)]

        album = base.get("album")
        if album is not None and not album.get("name"):
            album["name"] = albumName or name

        return base

    @staticmethod
    def _bumpStat(stats, key):
        if stats is not None:
            stats[key] = stats.get(key, 0) + 1

    def _processPlay(self, item, known, stats=None):
        name, artist, startTimestamp, timePlayed, trackUri, albumName = item[:6]
        extras = item[6] if len(item) > 6 else None
        playedFrom = item[7] if len(item) > 7 else None
        try:
            matchedId = self._resolveKnownKey(trackUri, name, artist, known)

            if matchedId:
                base = known[matchedId]
                if base.get("album") is None:
                    repaired = False
                    track_id = base.get("id")
                    # Shape, not a prefix: this guard used to test
                    # startswith("synth_"), which no TRACK id carries -
                    # _createSyntheticTrack emits a bare md5 digest - so it
                    # never fired, and a surrogate id was asked of Spotify
                    # for every play of that track, failing every time.
                    if looksLikeSpotifyTrackId(track_id):
                        try:
                            logger.info("Track %s (%s) is missing its album in DB. Querying Spotify API...", base.get("name"), track_id)
                            track_meta = self.sp.track(track_id)
                            formatted = Client.formatTrack(track_meta, embedPlaybackInfo=False)
                            if formatted.get("album"):
                                base["album"] = formatted["album"]
                                base["releaseDate"] = formatted["releaseDate"]
                                base["imageUrl"] = formatted["imageUrl"]
                                base["imageId"] = formatted["imageId"]
                                repaired = True
                                logger.info("Successfully refetched and repaired album for track %s from Spotify API", base.get("name"))
                        except Exception as e:
                            logger.warning("Failed to query Spotify API for track %s to repair album: %s", base.get("name"), e)
                    
                    if not repaired and albumName:
                        album_id = f"album_{base['id']}"
                        base["album"] = {
                            "id": album_id,
                            "name": albumName,
                            "url": "",
                            "imageId": album_id,
                            "imageUrl": "",
                            "totalTracks": 1,
                            "releaseDate": 0.0,
                        }
                        base["imageId"] = album_id
                        logger.info("Repaired missing album for track %s using import data: %s", base.get("name"), albumName)

                if base.get("created_reason") in (SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON):
                    if timePlayed > base.get("duration", 0):
                        base["duration"] = timePlayed
                # capAtDuration=False: the export's ms_played is authoritative, and the
                # matched catalog track may be a different (shorter) version of the song
                # (name+artist match / Spotify relinking) - capping at its duration would
                # discard real listening time.
                meta = Client.embedPlayInfo(base.copy(), startTimestamp, timePlayed, capAtDuration=False)
            else:
                if not name or not artist:
                    # Podcast/audiobook rows (null track name) and other
                    # unresolvable entries - counted so the drop isn't silent.
                    self._bumpStat(stats, "droppedNoTrack")
                    return None

                try:
                    meta = self._fetchTrackMeta(name, artist, trackUri)
                    base = Client.formatTrack(meta, embedPlaybackInfo=False)
                    base = self._overlayExportMetadata(base, name, artist, albumName)
                except Exception as e:
                    if self._isTransientLookupError(e):
                        # Don't freeze a synthetic record into the catalog over what's
                        # likely a temporary failure - skip the play; a re-import
                        # after the outage retries it (existing plays dedup).
                        logger.warning("Transient Spotify lookup failure for %s by %s (URI: %s) - skipping play, re-import to retry: %s", name, artist, trackUri, parseError(e))
                        self._bumpStat(stats, "droppedTransient")
                        return None
                    # Fallback to synthetic track
                    logger.info("Spotify lookup failed for %s by %s (URI: %s), using synthetic record: %s", name, artist, trackUri, parseError(e))
                    base = self._createSyntheticTrack(name, artist, trackUri, timePlayed, albumName=albumName)

                known[base["id"]] = base
                if trackUri:
                    known[trackUri] = base
                known[_knownNameKey(name, artist)] = base
                # capAtDuration=False: see the known-track branch above - export
                # ms_played is authoritative, fetched track may be a relinked version.
                meta = Client.embedPlayInfo(base.copy(), startTimestamp, timePlayed, capAtDuration=False)

            meta["isSkip"] = timePlayed < SKIP_THRESHOLD_MS
            # The play's context (playlist/album). The DB writer already reads
            # entry["playedFrom"] - it was simply never populated from an
            # export, so re-importing your own history dropped the column that
            # the listening-source breakdown is built on.
            meta["playedFrom"] = playedFrom
            if extras:
                meta["importExtras"] = extras
            return meta
        except Exception as e:
            # Counted like the other drops: an overwrite import decides whether
            # it may delete the covered range based on every parsed play having
            # made it through staging, and an uncounted drop there is silent
            # data loss.
            logger.error("Error processing item: %s", parseError(e))
            self._bumpStat(stats, "droppedUnexpected")
            return None
        
    def _import(self, dataFunction, history, known=None, progressCallback=None, stats=None):
        known = self.buildKnownIndex(known or [])
        self._catalogArtistsByName = self._buildCatalogArtistsByName(known)

        parsedItems = self._parseHistory(dataFunction, history, stats=stats)
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
                meta = self._processPlay(item, known, stats=stats)
                if meta:
                    yield meta

    def _accountEntryTuple(self, item):
        # endTime is documented by Spotify as UTC with no timezone marker on
        # the wire - timeToInt would otherwise interpret it as local time.
        endTimestamp = timeToIntUTC(item["endTime"])
        timePlayed = item["msPlayed"]

        startTimestamp = endTimestamp-timePlayed//1000
        name=item["trackName"]
        artist=item["artistName"]
        return name, artist, startTimestamp, timePlayed, None, None  #< account export carries no album name

    def importAcountHistory(self, history, known=None, progressCallback=None, stats=None):
        yield from self._import(self._accountEntryTuple, history, known, progressCallback, stats=stats)

    def _boolToInt(self, value):
        return None if value is None else int(bool(value))

    def _extractExtras(self, item):
        """The extended export's behavioral fields, keyed by their plays-table
        column names (incognito_mode -> incognito, booleans as 0/1). ip_addr
        is deliberately never extracted. None when the entry carries nothing -
        e.g. an instance's own re-imported export without these fields."""
        extras = {
            "platform": item.get("platform"),
            "conn_country": item.get("conn_country"),
            "reason_start": item.get("reason_start"),
            "reason_end": item.get("reason_end"),
            "shuffle": self._boolToInt(item.get("shuffle")),
            "skipped": self._boolToInt(item.get("skipped")),
            "offline": self._boolToInt(item.get("offline")),
            "incognito": self._boolToInt(item.get("incognito_mode")),
        }
        return extras if any(value is not None for value in extras.values()) else None

    def _extendedEntryTuple(self, item):
        ts = item["ts"]
        endTimestamp = timeToInt(ts)
        timePlayed = item.get("ms_played", 0)
        startTimestamp = endTimestamp - timePlayed // 1000

        # For offline plays ts is the SYNC time - whole sessions share one
        # stamp, sometimes days late. offline_timestamp is the true start,
        # sanity-guarded (plausible era, not after the sync stamp) because
        # real exports mix seconds and milliseconds and occasional garbage.
        if item.get("offline") and item.get("offline_timestamp"):
            try:
                raw = float(item["offline_timestamp"])
                normalized = raw / 1000 if raw > self.OFFLINE_TIMESTAMP_MS_CUTOFF else raw
                if self.MIN_PLAUSIBLE_PLAY_TIMESTAMP <= normalized <= endTimestamp:
                    startTimestamp = int(normalized)
            except (TypeError, ValueError):
                pass  #< unparsable offline_timestamp - keep the ts-derived start

        name = item["master_metadata_track_name"]
        artist = item["master_metadata_album_artist_name"]
        albumName = item.get("master_metadata_album_album_name")
        uri = item.get("spotify_track_uri")
        trackUri = uri.split(":")[-1] if uri else None
        # played_from is this app's own addition to the format (Spotify's exports
        # have no such field), so a genuine Spotify export simply yields None.
        # Only a string is accepted: the value is later split on ':' to resolve a
        # playlist name, and an edited file could carry anything.
        playedFrom = item.get("played_from")
        if not isinstance(playedFrom, str):
            playedFrom = None
        return (name, artist, startTimestamp, timePlayed, trackUri, albumName,
                self._extractExtras(item), playedFrom)

    def importExtendedHistory(self, history, known=None, progressCallback=None, stats=None):
        yield from self._import(self._extendedEntryTuple, history, known, progressCallback, stats=stats)

    def expectedEntryCount(self, parsedHistory, exportType) -> int:
        """How many PLAYS importHistory will yield at most, which is not the
        parsed file's length for every format.

        One Musicolet CSV row carries an aggregate play count and expands to
        that many plays (_expandMusicoletRows), so a caller using
        len(parsedHistory) as a progress denominator counted plays against rows:
        the import bar read "Fetched 20000 of 800". Every other format expands
        1:1, which is why the mismatch went unnoticed.

        An upper bound, not an exact figure - _parseHistory and _processPlay
        drop entries below this - which is the same relationship len() already
        had with the two Spotify formats, and the direction a progress
        denominator needs.

        No stats: this runs the same expansion staging will run, and passing the
        import's dict would count every dropped row twice - the rule coverage()
        below already follows for the same reason."""
        if exportType != "musicoletPremium":
            return len(parsedHistory)
        return len(self._expandMusicoletRows(parsedHistory))

    def coverage(self, parsedHistory, exportType):
        """(minStart, maxEnd, coveredYears) across every entry (skip-length
        included) - the overwrite import deletes covered-year segments within
        the span before re-importing. Years come from START timestamps so a
        play straddling New Year doesn't spuriously cover the next year; the
        offline_timestamp correction applies, widening the span to where
        offline plays actually happened. None when the export is empty or
        unrecognized - the caller must abort instead of deleting anything."""
        entryFunctions = {
            "spotifyAcountExport": (self._accountEntryTuple, lambda rows: rows),
            "spotifyExtendedExport": (self._extendedEntryTuple, lambda rows: rows),
            # No stats here on purpose: coverage() runs the same expansion over
            # the same rows as staging does, separately, and passing the import's
            # dict would count every dropped row twice.
            "musicoletPremium": (self._musicoletEntryTuple, self._expandMusicoletRows),
        }
        if exportType not in entryFunctions:
            return None
        entryTuple, prepare = entryFunctions[exportType]
        parsedItems = self._parseHistory(entryTuple, prepare(parsedHistory))
        if not parsedItems:
            return None

        minStart = None
        maxEnd = None
        coveredYears = set()
        for name, artist, startTimestamp, timePlayed, *_ in parsedItems:
            startTs = timeToInt(startTimestamp)
            endTs = startTs + timePlayed / 1000
            minStart = startTs if minStart is None else min(minStart, startTs)
            maxEnd = endTs if maxEnd is None else max(maxEnd, endTs)
            # App timezone, matching how the dashboard buckets days/years -
            # convertToDatetime's own default is UTC.
            coveredYears.add(convertToDatetime(startTs, getTimezone()).year)
        return minStart, maxEnd, coveredYears

    # Musicolet's CSV only carries an aggregate play count per track, not
    # individual play timestamps. Synthetic per-play timestamps are anchored
    # here (a fixed epoch) rather than at now() - re-importing the same file
    # then reproduces the exact same (track, played_at) pairs and is silently
    # deduped by the plays.UNIQUE constraint instead of creating a fresh batch
    # of fake plays every time. An updated file with a higher play count for a
    # track only adds the new tail of plays.
    MUSICOLET_SYNTHETIC_TIME_ANCHOR = datetime.datetime(2000, 1, 1)
    # Ceiling on how many synthetic plays ONE Musicolet file may expand into.
    # PLAY_COUNT is an amplifier - one tuple is materialised per play - so a
    # ~60-byte row claiming 50 million plays costs ~4GB, in the process every
    # user's listener and the backup/email workers share. Sized far above any
    # real library: the heaviest accounts on this instance carry tens of
    # thousands of plays in TOTAL, so a legitimate import cannot reach this.
    # A total rather than a per-row cap, because a per-row one leaves the same
    # hole open to a file of many rows each sitting just under it.
    MUSICOLET_MAX_EXPANDED_PLAYS = 2_000_000

    def _expandMusicoletRows(self, rows, stats=None):
        """One CSV row per track, expanded into `PLAY_COUNT` synthetic plays.

        This runs BEFORE _parseHistory, so it owns the same counting duty that
        docstring describes: a row lost here produces no entry at all, and
        _parseHistory's per-entry `entriesSeen` therefore never sees it. Both
        consumers of these counters go blind to it - the all-unreadable guard
        (entriesSeen == droppedMalformed) and, worse, the overwrite import's
        refusal to delete a range it cannot rebuild. That second one has teeth
        here specifically: every Musicolet row is anchored at the same
        MUSICOLET_SYNTHETIC_TIME_ANCHOR, so the SURVIVING rows' covered range
        spans the dropped row's timestamps too, and its plays are deleted with
        nothing left to re-insert them.

        The header row is NOT our problem: _convertToList strips it
        (splitlines()[1:]) and coverage() is handed the same stripped list, so
        `int("DURATION_MS")` never reaches the counter below. Were it to, every
        well-formed Musicolet overwrite import would abort."""
        ### Data formatted in: FILE_PATH,TITLE,ARTIST,ALBUM,ALBUM_ARTIST,COMPOSER,GENRE,YEAR,DURATION_MS,PLAY_COUNT
        NAME = 1
        ARTISTS = 2
        ALBUM = 3
        DURATION_MS = 8
        PLAYCOUNT = 9

        formatedData = []
        reader = csv.reader(rows)
        loggedMalformed = 0

        for index, song in enumerate(reader):
            if not song or not any(field.strip() for field in song):
                continue   #< blank line: formatting, not a lost play - counting it
                           #  would abort an overwrite import over a stray newline

            try:
                name = song[NAME]
                mainArtist = song[ARTISTS].split("/")[0]
                albumName = song[ALBUM]
                timePlayed = int(song[DURATION_MS])
                playCount = int(song[PLAYCOUNT])

                if playCount < 1:
                    # A library row nobody has played expands to nothing, so
                    # _parseHistory - which counts entriesSeen per expanded PLAY
                    # - would never see it, while a malformed row below counts 1.
                    # A file of never-played rows plus one bad row would then
                    # read as "nothing could be read" and be rejected whole.
                    # Counted, but NOT as a drop: the row was perfectly readable.
                    self._bumpStat(stats, "entriesSeen")
                    continue

                # BEFORE the loop, and against the RUNNING total: checked
                # afterwards it would report the problem having already spent
                # the memory, which is the whole failure.
                #
                # Refused outright rather than clamped. Clamping silently drops
                # real plays, and every Musicolet row is anchored at the same
                # MUSICOLET_SYNTHETIC_TIME_ANCHOR - so the surviving rows'
                # covered range still spans the discarded ones, and an overwrite
                # import would delete plays nothing re-inserts (the hazard this
                # function's docstring describes). A file this importer cannot
                # represent should say so, not import a subset of itself.
                if len(formatedData) + playCount > self.MUSICOLET_MAX_EXPANDED_PLAYS:
                    raise MusicoletExpansionTooLargeError(
                        f"this file expands to more than {self.MUSICOLET_MAX_EXPANDED_PLAYS} "
                        f"plays, which is more than one import can hold - check the "
                        f"PLAY_COUNT column for an implausible value"
                    )

                trackTime = self.MUSICOLET_SYNTHETIC_TIME_ANCHOR
                for _ in range(playCount):
                    startTimestamp = trackTime.strftime("%Y-%m-%d %H:%M:%S")
                    formatedData.append((
                        name,
                        mainArtist,
                        startTimestamp,
                        timePlayed,
                        albumName
                    ))
                    trackTime += datetime.timedelta(milliseconds=timePlayed)

            except (IndexError, ValueError) as e:
                # entriesSeen too, not just the drop: the all-unreadable guard
                # compares the two, and a row that never expanded is a row
                # _parseHistory will never count.
                self._bumpStat(stats, "entriesSeen")
                self._bumpStat(stats, "droppedMalformed")
                # Capped exactly as _parseHistory caps its own: a misclassified
                # file makes every row fail.
                #
                # The exception TYPE and the position only - NOT parseError(e),
                # unlike the JSON path above. There the failures are KeyErrors
                # naming Spotify's own field names; here they come from
                # int(song[DURATION_MS]), and ValueError puts the offending
                # VALUE in its message - which on a column-shifted CSV is a
                # track title, an album or a file path. That is the user's
                # listening history, and it would land in the app log.
                if loggedMalformed < self.MAX_MALFORMED_ENTRY_LOG_LINES:
                    loggedMalformed += 1
                    logger.warning("Skipping unreadable Musicolet CSV row at position %d: %s",
                                   index, type(e).__name__)

        return formatedData

    def _musicoletEntryTuple(self, item):
        name, mainArtist, startTimestamp, timePlayed, albumName = item
        return name, mainArtist, startTimestamp, timePlayed, None, albumName

    def importMusicoletCSVExport(self, rows, known=None, progressCallback=None, stats=None):
        yield from self._import(self._musicoletEntryTuple, self._expandMusicoletRows(rows, stats=stats),
                                known, progressCallback, stats=stats)