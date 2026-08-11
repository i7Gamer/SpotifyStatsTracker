# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# Imported for the type hints below. Everything used at RUNTIME still goes
# through _dbmod, so the suite's patch("Database.database.X") targets are
# unaffected - these names were simply never defined here, leaving the hints
# unresolvable for tooling and for typing.get_type_hints(). All leaf modules
# (stdlib, or Database/lastfm.py, which imports nothing of ours), so a real
# import costs nothing and cannot cycle.
from pathlib import Path
from Database.lastfm import LastfmClient, LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS

# Module-global names (LastfmClient, requests, Importer, logger, time, Path, ...)
# are reached through the database module, so the suite's
# patch("Database.database.X") targets keep working here. Late-bound rather than
# imported: database.py imports this file's mixin, so importing it back by name
# made the cycle break whichever module was imported first (see Database/dbmodule.py).
from Database.dbmodule import dbmod as _dbmod


# Cover art from Spotify's CDN is tens of KB; this is a wide ceiling that only
# a misbehaving or redirected endpoint would reach. Without it the whole body is
# materialized in memory before PIL ever inspects it, so one bad response could
# balloon a worker thread's footprint.
MAX_IMAGE_BYTES = 12 * 1024 * 1024

# Bounds both outbound requests below - the CDN image download and the Web API
# artist lookup. Each runs on a background image thread and has a fallback
# behind it (the cookie client, or a later retry through the pending claim), so
# a stalled endpoint should cost one slow attempt rather than a wedged worker.
MEDIA_FETCH_HTTP_TIMEOUT_SECONDS = 10


def _isTransientFetchError(exc: Exception) -> bool:
    """Whether this failure left us knowing nothing about the image.

    The line is "did we get the bytes and find them unusable, or did we never
    get them". Anything that is not a request error got far enough to be a
    verdict about the image itself - PIL could not decode it, the cap says it
    is too big (a ValueError, deliberately not a RequestException), the disk
    refused it. Those stay 'failed'.

    A request error is transient EXCEPT a 4xx, which is the server telling us
    this URL is not going to work: a 404 cover stays 404, and re-asking is the
    load 'failed' exists to stop. 5xx, timeouts and connection resets carry no
    such answer - `response` is absent on those, which is what the status
    lookup falls through on."""
    if not isinstance(exc, _dbmod.requests.exceptions.RequestException):
        return False
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return not (status and 400 <= status < 500)


class MediaFetchMixin:
    """Album/artist image + Last.fm biography fetching and on-demand refresh, mixed into Database."""

    @staticmethod
    def _readCappedBody(response) -> bytes:
        """The response body, refusing anything over MAX_IMAGE_BYTES.

        Content-Length is only an early exit - a server may omit or understate
        it - so the body is also accumulated with a hard stop. Streaming keeps
        an oversized response from being materialized in full before PIL ever
        looks at it."""
        declared = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
        if declared is not None and str(declared).isdigit() and int(declared) > MAX_IMAGE_BYTES:
            raise ValueError(f"image too large: {declared} bytes (max {MAX_IMAGE_BYTES})")

        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def _downloadImageTask(self, path: Path, url: str, imgId: str, kind: str):
        try:
            # Held in a `with`: the body is streamed and _readCappedBody stops
            # draining it the moment it blows the cap, and an undrained response
            # never returns its connection to the pool - the next image download
            # then pays a fresh TCP+TLS handshake. Closed before the decode/save
            # work below, which no longer needs the socket.
            with _dbmod.requests.get(url, timeout=MEDIA_FETCH_HTTP_TIMEOUT_SECONDS, stream=True) as response:
                response.raise_for_status()
                content = self._readCappedBody(response)
            img = _dbmod.Image.open(_dbmod.BytesIO(content))
            # Always store as JPEG: the templates hardcode `<imgId>.jpeg`, so an
            # image saved under its source format (e.g. .png) would 404 forever.
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")   #< JPEG can't store alpha/palette modes
            path.mkdir(parents=True, exist_ok=True)
            # Staged and renamed, not written in place: a save that dies partway
            # (full disk, power loss) would otherwise leave a truncated JPEG at
            # the final path - and lazyFetchArtistImage returns True on
            # imagePath.exists() BEFORE it consults the download status, so that
            # broken file would be served forever and never re-fetched. Same
            # .partial + os.replace shape the database backup uses.
            finalPath = path / f"{imgId}.jpeg"
            partialPath = finalPath.with_suffix(".partial")
            try:
                img.save(partialPath, format="JPEG")
                _dbmod.os.replace(partialPath, finalPath)
            except Exception:
                partialPath.unlink(missing_ok=True)
                raise
            self.repo.markImageStatus(imgId, kind, _dbmod.IMAGE_STATUS_OK)
        except Exception as e:
            if _isTransientFetchError(e):
                #< see releaseImageClaim: 'failed' is a verdict about the IMAGE,
                #  and lazyFetchArtistImage refuses one for good. A network-layer
                #  failure never saw the image, so it is not entitled to that
                #  verdict - an artist whose lookup succeeded and whose download
                #  then hit a blip would otherwise lose their picture
                #  permanently. Track covers re-claim a 'failed' row anyway
                #  (tryClaimImageDownload allows it, and the listener re-saves on
                #  the next play), so for them this only avoids recording a
                #  verdict nobody reached.
                self.repo.releaseImageClaim(imgId, kind)
            else:
                self.repo.markImageStatus(imgId, kind, _dbmod.IMAGE_STATUS_FAILED)
            if isinstance(e, _dbmod.requests.exceptions.RequestException):
                _dbmod.logger.error("Error fetching image from %s (id=%s): %s", url, imgId, _dbmod.parseError(e))
            else:
                _dbmod.logger.error("Error saving image (id=%s): %s", imgId, _dbmod.parseError(e))

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
        self._saveImg(self.imgDir_tracks, url, imgId, kind=_dbmod.IMAGE_KIND_TRACK)

    def _fetchArtistImageUrl(self, artistId: str) -> str | None:
        """Looks up a real Spotify CDN image URL for an artist, mirroring the
        dual-path fetch _metadataBackfillLoop already uses for albums: the
        authenticated Web API first (if this user has API credentials configured),
        falling back to the cookie client otherwise. Returns None if neither source has
        an image (including a definitive "no images" response from the official
        API, which is trusted as-is rather than spending another request asking
        the cookie client the same question).

        Previously this scraped open.spotify.com's public artist page for an
        og:image meta tag - that stopped working once Spotify moved artist pages
        to a client-rendered SPA shell with no server-rendered metadata, for every
        artist, not just obscure ones."""
        creds = self.getUserSpotifyCredentials()
        #< all three, like the listener's own gate (spotifyListener.py): the
        #  refresh call reads client_secret unconditionally, so a two-of-three
        #  row raised KeyError here instead of falling back
        if creds and creds.get("client_id") and creds.get("client_secret") and creds.get("refresh_token"):
            from Database.Listeners.spotifyListener import _refresh_spotify_access_token
            access_token = _refresh_spotify_access_token(
                creds["client_id"], creds["client_secret"], creds["refresh_token"])
            if access_token:
                try:
                    headers = {"Authorization": f"Bearer {access_token}"}
                    resp = _dbmod.requests.get(f"https://api.spotify.com/v1/artists/{artistId}", headers=headers,
                                               timeout=MEDIA_FETCH_HTTP_TIMEOUT_SECONDS)
                    if resp.status_code == 200:
                        images = resp.json().get("images") or []
                        return images[0]["url"] if images else None
                except Exception as e:
                    _dbmod.logger.warning("Web API artist image fetch failed for %s, falling back to the cookie client: %s",
                                    artistId, _dbmod.parseError(e))
            else:
                _dbmod.logger.warning("Failed to refresh access token, falling back to the cookie client for artist image %s.", artistId)

        import Database.Spotify
        # No cookiesFile on purpose: artist() is a public lookup through
        # spotapi's pooled client and never touches the login. Constructing
        # with cookies ran a full login() whose TLSClient atexit-pinned one
        # live curl session PER ARTIST IMAGE for the life of the process -
        # the same leak _pooledPublicClient was built to close - and paid a
        # temp cookies-file write/delete for a session nothing used.
        sp = Database.Spotify.Spotify()
        images = (sp.artist(artistId) or {}).get("images") or []
        return images[0]["url"] if images else None

    def _lazyFetchArtistImageTask(self, artistId: str, imagePath: Path) -> bool:
        try:
            imageUrl = self._fetchArtistImageUrl(artistId)
        except Exception as e:
            _dbmod.logger.error("Failed to lazy load artist image for %s: %s", artistId, _dbmod.parseError(e))
            #< NOT 'failed'. That status means "asked, and this artist has no
            #  picture", and lazyFetchArtistImage refuses it for good - it
            #  returns False before reaching the reclaim tryClaimImageDownload
            #  would otherwise allow. An exception establishes nothing of the
            #  sort: a network blip or an open limiter backoff window (which
            #  any user's 429 opens) would deny that artist their picture
            #  permanently, fixable only by a migrator - which this project has
            #  now had to ship twice for stuck 'failed' rows. Release the claim
            #  and let a later render ask again.
            self.repo.releaseImageClaim(artistId, _dbmod.IMAGE_KIND_ARTIST)
            return False

        if not imageUrl:
            #< a real answer: the lookup came back and carried no images
            self.repo.markImageStatus(artistId, _dbmod.IMAGE_KIND_ARTIST, _dbmod.IMAGE_STATUS_FAILED)
            return False

        # Reuses the same download/normalize-to-JPEG/markImageStatus pipeline
        # tracks and albums already go through, run synchronously since this task
        # itself already runs on _imageDownloadExecutor (dispatched non-blocking
        # by lazyFetchArtistImage below) - submitting again would just queue a
        # redundant future.
        self._downloadImageTask(imagePath.parent, imageUrl, artistId, _dbmod.IMAGE_KIND_ARTIST)
        return imagePath.exists()

    def lazyFetchArtistImage(self, artistId: str, imagePath: Path):
        """Best-effort fetch of an artist's image via the Spotify Web API /
        the cookie client (see _fetchArtistImageUrl), used as a fallback for artists we
        never received image metadata for from the API. Deduplicated per artist id
        via the database's image status table so failed fetches persist across app
        restarts.

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

        status = self.repo.imageStatus(artistId, _dbmod.IMAGE_KIND_ARTIST)
        if status == _dbmod.IMAGE_STATUS_OK:
            return imagePath.exists()
        if status == _dbmod.IMAGE_STATUS_FAILED:
            return False

        if self.repo.tryClaimImageDownload(artistId, _dbmod.IMAGE_KIND_ARTIST):
            return self._imageDownloadExecutor.submit(self._lazyFetchArtistImageTask, artistId, imagePath)
        return False

    def _lazyFetchArtistBioTask(self, artistId: str, artistName: str) -> bool:
        """Returns whether a usable bio was actually stored - False for a
        definitive "nothing available" result, transient/invalid-key
        (unattempted, left for a later retry), or an unexpected error."""
        try:
            apiKey = self.repo.getUserLastfmApiKey(self.user)
            if not apiKey:
                return False   #< key removed between the claim and this task running; leave unattempted
            outcome = _dbmod.LastfmClient(apiKey).getArtistInfo(artistName)
            if outcome is None or outcome.status not in (_dbmod.OUTCOME_OK, _dbmod.OUTCOME_NOT_FOUND):
                return False   #< transient/invalid-key: stays unattempted, a later page view retries
            bio = outcome.bio if outcome.status == _dbmod.OUTCOME_OK else None
            self.repo.setArtistBio(artistId, bio)
            return bio is not None
        except Exception as e:
            _dbmod.logger.error("Failed to lazy fetch artist bio for %s: %s", artistId, _dbmod.parseError(e))
            return False
        finally:
            self._releaseLastfmEntities("bio", [{"id": artistId}])

    def lazyFetchArtistBio(self, artistId: str, artistName: str):
        """Best-effort, on-demand fetch of an artist's biography via Last.fm's
        artist.getinfo (see Database.lastfm.getArtistInfo for the HTML-
        stripping and "+"-name incorrect-tag-entity guard), triggered by a
        page visit rather than the background biography backfiller's queue.
        Permanent-once-tried like artist images: THIS call never retries an
        artist it's already attempted, even if the result was "no bio" - the
        background backfiller (_lastfmBiographyBackfillLoop) is the one that
        revisits a definitive-empty result later, on its own 30-day cycle
        (see getArtistsMissingBiographies), so a page view alone can't spin
        Last.fm on every visit to an artist with no bio.

        The admin's instance-wide toggle (isArtistBioEnabled) gates fetching
        itself, not just display - disabled means no lookups at all, same
        contract as the Last.fm genre backfill's kill switch.

        Runs on a small dedicated executor (like lazyFetchArtistImage) so an
        artist page render never blocks on Last.fm's response time; the
        route doesn't wait on it and just renders whatever's already stored
        (None on a first-ever visit - a later visit shows it once fetched).
        Returns True if already attempted (bio may still be None -
        definitively no bio available), False if there's nothing to fetch
        (no artistId/name, the feature is disabled, no stored Last.fm key for
        this user, or another thread already claimed this artist), or the
        submitted Future on a freshly kicked-off fetch."""
        if not artistId or not artistName:
            return False
        if not self.repo.isArtistBioEnabled():
            return False

        state = self.repo.getArtistBioState(artistId)
        if state["attempted_at"] is not None:
            return True
        if not self.repo.getUserLastfmApiKey(self.user):
            return False

        if self._claimLastfmEntities("bio", [{"id": artistId}]):
            return self._artistBioFetchExecutor.submit(self._lazyFetchArtistBioTask, artistId, artistName)
        return False

    def _lazyFetchAlbumBioTask(self, albumId: str, albumName: str, artistName: str) -> bool:
        """Returns whether a usable bio was actually stored - mirrors
        _lazyFetchArtistBioTask, but album.getinfo also needs the album's
        primary artist name."""
        try:
            apiKey = self.repo.getUserLastfmApiKey(self.user)
            if not apiKey:
                return False   #< key removed between the claim and this task running; leave unattempted
            client = _dbmod.LastfmClient(apiKey)
            outcome = self._lastfmLookupBioOutcome(
                lambda name: client.getAlbumInfo(artistName, name), albumName)
            if outcome is None or outcome.status not in (_dbmod.OUTCOME_OK, _dbmod.OUTCOME_NOT_FOUND):
                return False   #< transient/invalid-key: stays unattempted, a later page view retries
            bio = outcome.bio if outcome.status == _dbmod.OUTCOME_OK else None
            self.repo.setAlbumBio(albumId, bio)
            return bio is not None
        except Exception as e:
            _dbmod.logger.error("Failed to lazy fetch album bio for %s: %s", albumId, _dbmod.parseError(e))
            return False
        finally:
            self._releaseLastfmEntities("album_bio", [{"id": albumId}])

    def lazyFetchAlbumBio(self, albumId: str, albumName: str, artistName: str):
        """Best-effort, on-demand fetch of an album's biography via Last.fm's
        album.getinfo, mirroring lazyFetchArtistBio - same permanent-once-
        tried contract, same isAlbumBioEnabled() gate on fetching itself
        (not just display), dispatched on its own small executor
        (_albumBioFetchExecutor) so an album page render never blocks on
        Last.fm's response time. `artistName` is the album's primary
        artist - album.getinfo needs artist+album, unlike artist.getinfo.
        Returns True if already attempted, False if there's nothing to fetch
        (missing id/name/artist, the feature is disabled, no stored Last.fm
        key, or another thread already claimed this album), or the
        submitted Future on a freshly kicked-off fetch."""
        if not albumId or not albumName or not artistName:
            return False
        if not self.repo.isAlbumBioEnabled():
            return False

        state = self.repo.getAlbumBioState(albumId)
        if state["attempted_at"] is not None:
            return True
        if not self.repo.getUserLastfmApiKey(self.user):
            return False

        if self._claimLastfmEntities("album_bio", [{"id": albumId}]):
            return self._albumBioFetchExecutor.submit(self._lazyFetchAlbumBioTask, albumId, albumName, artistName)
        return False

    def saveImagesFromTrack(self, track: dict):
        self.saveTrackImg(track["imageUrl"], track["imageId"])

    def refreshLastfmEntity(self, kind: str, entityId: str) -> dict:
        """Force a fresh Last.fm lookup for exactly one artist/album/track,
        bypassing every "already attempted" gate (GENRE_BACKFILL_RETRY_SECONDS,
        the bio permanent-once-tried contract) - the admin-triggered "Refresh
        Last.fm Data" button's synchronous action, not a batch. Uses the
        calling admin's own stored Last.fm key, like lazyFetchArtistBio.

        Returns {"status": ..., "name": <entity name, when known>}. status is
        one of: "no_api_key", "not_found", "no_artist" (album with no
        derivable primary artist), "invalid_key", "transient" (no definitive
        genre result this attempt - try again), "ok".

        Reuses _storeLastfmGenresWithInheritance for albums/tracks, so a
        refresh behaves exactly like a first-time backfill including its
        inheritance fallback - see that method's docstring for the one known
        edge case where stale OWN genres aren't cleared (the entity, its
        album and its artist all now come back empty after previously having
        own tags)."""
        apiKey = self.repo.getUserLastfmApiKey(self.user)
        if not apiKey:
            return {"status": "no_api_key"}
        client = _dbmod.LastfmClient(apiKey)

        try:
            if kind == "artist":
                return self._refreshArtistLastfmData(client, entityId)
            if kind == "album":
                return self._refreshAlbumLastfmData(client, entityId)
            if kind == "track":
                return self._refreshTrackLastfmData(client, entityId)
            raise ValueError(f"Unknown Last.fm refresh kind: {kind!r}")
        except _dbmod._LastfmInvalidKeyError:
            return {"status": "invalid_key"}

    def _refreshArtistLastfmData(self, client: LastfmClient, artistId: str) -> dict:
        row = self.repo.getArtistLastfmLookupRow(artistId)
        if row is None:
            return {"status": "not_found"}

        # timeout on every lookup here (and in the album/track twins below):
        # this runs synchronously on an admin request thread, and an unbounded
        # limiter acquire sat out whole backoff windows (60s on a Last.fm 429)
        # holding a Waitress thread - the same rule validateApiKey follows.
        # A timed-out lookup (None) already maps to "transient": try again.
        outcome = client.getArtistTopTags(row["name"], stop_event=self.lastfm_stop_event,
                                          timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS)
        if outcome is None:
            return {"status": "transient"}
        definitive, genres = self._lastfmOutcomeGenres(outcome)
        if not definitive:
            return {"status": "transient"}
        self.repo.replaceArtistGenres(artistId, genres)
        self.repo.markArtistsLastfmAttempted([artistId])

        bioOutcome = client.getArtistInfo(row["name"], timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS)
        if bioOutcome is not None and bioOutcome.status in (_dbmod.OUTCOME_OK, _dbmod.OUTCOME_NOT_FOUND):
            self.repo.setArtistBio(artistId, bioOutcome.bio if bioOutcome.status == _dbmod.OUTCOME_OK else None)

        return {"status": "ok", "name": row["name"]}

    def _refreshAlbumLastfmData(self, client: LastfmClient, albumId: str) -> dict:
        row = self.repo.getAlbumLastfmLookupRow(albumId)
        if row is None:
            return {"status": "not_found"}
        primary = self.repo.getAlbumPrimaryArtists([albumId]).get(albumId)
        if primary is None:
            return {"status": "no_artist"}

        definitive, genres, aborted = self._lastfmLookupOwnGenres(
            lambda name: client.getAlbumTopTags(primary["artist_name"], name,
                                                stop_event=self.lastfm_stop_event,
                                                timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS),
            row["name"])
        if aborted or not definitive:
            return {"status": "transient"}
        if not self._storeLastfmGenresWithInheritance(
                client, "album", albumId, genres, primary["artist_id"], primary["artist_name"],
                timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS):
            return {"status": "transient"}

        bioOutcome = self._lastfmLookupBioOutcome(
            lambda name: client.getAlbumInfo(primary["artist_name"], name,
                                             timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS),
            row["name"])
        if bioOutcome is not None and bioOutcome.status in (_dbmod.OUTCOME_OK, _dbmod.OUTCOME_NOT_FOUND):
            self.repo.setAlbumBio(albumId, bioOutcome.bio if bioOutcome.status == _dbmod.OUTCOME_OK else None)

        return {"status": "ok", "name": row["name"]}

    def _refreshTrackLastfmData(self, client: LastfmClient, trackId: str) -> dict:
        row = self.repo.getTrackLastfmLookupRow(trackId)
        if row is None:
            return {"status": "not_found"}

        definitive, genres, aborted = self._lastfmLookupOwnGenres(
            lambda name: client.getTrackTopTags(row["artist_name"], name,
                                                stop_event=self.lastfm_stop_event,
                                                timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS),
            row["name"])
        if aborted or not definitive:
            return {"status": "transient"}
        if not self._storeLastfmGenresWithInheritance(
                client, "track", trackId, genres, row["artist_id"], row["artist_name"],
                albumId=row["album_id"], timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS):
            return {"status": "transient"}

        return {"status": "ok", "name": row["name"]}
