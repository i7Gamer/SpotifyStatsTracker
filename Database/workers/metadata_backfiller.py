# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# Imported for the type hints below. Everything used at RUNTIME still goes
# through _dbmod, so the suite's patch("Database.database.X") targets are
# unaffected - these names were simply never defined here, leaving the hints
# unresolvable for tooling and for typing.get_type_hints(). All leaf modules
# (stdlib, or Database/lastfm.py, which imports nothing of ours), so a real
# import costs nothing and cannot cycle.
import threading

# Module-global names (LastfmClient, requests, Importer, logger, time, Path, ...)
# are reached through the database module, so the suite's
# patch("Database.database.X") targets keep working here. Late-bound rather than
# imported: database.py imports this file's mixin, so importing it back by name
# made the cycle break whichever module was imported first (see Database/dbmodule.py).
from Database.dbmodule import dbmod as _dbmod


# The Web API's GET /v1/albums hard cap on ids per request; also bounds the
# cookie-client fallback's per-cycle batch so both paths pace identically.
SPOTIFY_BULK_ALBUM_LIMIT = 20


class MetadataBackfillMixin:
    """The Spotify Web-API metadata backfiller (missing album/track dates, artistless tracks)."""

    def getSpotifyApiWorkerStatus(self) -> dict:
        """Same shape as getLastfmWorkerStatus, for the Spotify API metadata
        backfiller worker thread."""
        return self._workerStatus(
            "backfiller_thread", "spotify_api",
            configured=bool(self.repo.getUserSpotifyCredentials(self.user)))

    def startMetadataBackfiller(self) -> None:
        """Start the background thread to fill in missing album metadata."""
        self._startPeriodicWorker("backfiller_thread", "backfiller_stop_event",
                                   self._metadataBackfillLoop,
                                   f"metadata-backfiller-{self.user}", logPrefix="MetadataBackfiller")

    def stopMetadataBackfiller(self) -> None:
        """Signal and wait for the background backfiller thread to stop."""
        self._stopPeriodicWorker("backfiller_thread", "backfiller_stop_event")

    @staticmethod
    def _normalizeBackfillArtists(artistsRaw: list) -> list[dict]:
        """Repo-shaped artist dicts from an album payload's per-track artist
        list (Web API and the cookie client both expose id/name/external_urls).
        Entries without a real id or name are dropped - fabricating links
        would be worse than leaving the track for the next repair pass."""
        artists = []
        for artist in artistsRaw:
            if not isinstance(artist, dict):
                continue
            artistId = artist.get("id")
            name = artist.get("name")
            if not artistId or not name:
                continue
            url = (artist.get("external_urls") or {}).get("spotify") or \
                f"https://open.spotify.com/artist/{artistId}"
            artists.append({"id": artistId, "name": name, "url": url, "imageId": artistId})
        return artists

    def _metadataBackfillLoop(self, stop_event: threading.Event | None = None) -> None:
        """Periodically queries Spotify for missing album release dates and tracks.

        `stop_event` is THIS run's private event (see the fresh-event note in
        startMetadataBackfiller) - a later restart can never revive this
        thread."""
        import random
        if stop_event is None:
            stop_event = self.backfiller_stop_event
        try:
            # 1. Random startup offset to prevent multiple user threads from starting at the same moment
            startup_delay = random.randint(self.BACKFILLER_MIN_START_DELAY, self.BACKFILLER_MAX_START_DELAY)
            _dbmod.logger.info("[Backfiller-%s] Starting with initial delay of %d seconds", self.user, startup_delay)
            if stop_event.wait(startup_delay):
                _dbmod.logger.info("[Backfiller-%s] Stopped during startup delay", self.user)
                return

            while not stop_event.is_set():
                target_ids = []
                try:
                    if not self.repo.isSpotifyApiBackfillEnabled():
                        if stop_event.wait(self.BACKFILLER_IDLE_WAIT_SECONDS):
                            break
                        continue

                    # 2. Get Spotify API credentials if configured
                    creds = self.getUserSpotifyCredentials()

                    # 3. Query up to N missing album IDs. Albums whose tracks
                    # lack artist links piggyback on the same fetch: the album
                    # payload carries per-track artists, repairing tracks that
                    # were saved from degraded payloads without artist data.
                    missing_ids = self.repo.getAlbumsMissingMetadata(limit=self.BACKFILLER_ALBUM_QUEUE_SIZE)
                    if len(missing_ids) < self.BACKFILLER_ALBUM_QUEUE_SIZE:
                        known_ids = set(missing_ids)
                        missing_ids.extend(
                            albumId for albumId in self.repo.getAlbumsWithArtistlessTracks(
                                self.BACKFILLER_ALBUM_QUEUE_SIZE - len(missing_ids))
                            if albumId not in known_ids)
                    if not missing_ids:
                        if stop_event.wait(self.BACKFILLER_IDLE_WAIT_SECONDS):
                            break
                        continue

                    # 4. Process-wide deduplication: filter out already active backfills
                    with _dbmod.Database._backfill_lock:
                        for album_id in missing_ids:
                            if album_id not in _dbmod.Database._active_backfills:
                                target_ids.append(album_id)
                                _dbmod.Database._active_backfills.add(album_id)
                                if len(target_ids) >= SPOTIFY_BULK_ALBUM_LIMIT:
                                    break

                    # 5. If nothing eligible remains, wait and try next iteration
                    if not target_ids:
                        if stop_event.wait(self.BACKFILLER_IDLE_WAIT_SECONDS):
                            break
                        continue

                    # 6. Fetch detailed metadata
                    _dbmod.logger.info("[Backfiller-%s] Fetching metadata for %d albums", self.user, len(target_ids))
                    fetched_albums = []
                    attempted_ids = []  #< albums that got a definitive response (incl. "gone") - rate-limits their next retry
                    use_fallback = True

                    #< all three, like the listener's own gate - see the same
                    #  comment on _fetchArtistImageUrl's copy in media_fetch.py
                    if creds and creds.get("client_id") and creds.get("client_secret") and creds.get("refresh_token"):
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
                                if _dbmod.os.environ.get("FLASK_DEBUG", "").lower() in _dbmod.TRUTHY_DEBUG_VALUES:
                                    _dbmod.logger.warning(
                                        "[Backfiller-%s] Spotify Web API returned status %d. Falling back to the cookie client.",
                                        self.user, resp.status_code
                                    )
                        else:
                            _dbmod.logger.warning("[Backfiller-%s] Failed to refresh access token. Falling back to the cookie client.", self.user)

                    if use_fallback:
                        import Database.Spotify
                        # No cookiesFile on purpose: album() is a public lookup
                        # through spotapi's pooled client and never touches the
                        # login. Constructing with cookies ran a full login()
                        # whose TLSClient atexit-pinned one live curl session
                        # per backfill cycle (every 5 minutes, for every user
                        # without Web-API credentials) - the same leak
                        # _pooledPublicClient was built to close.
                        sp = Database.Spotify.Spotify()
                        for album_id in target_ids:
                            if stop_event.is_set():
                                break
                            try:
                                album_raw = sp.album(album_id)
                                if album_raw:
                                    fetched_albums.append(album_raw)
                                attempted_ids.append(album_id)  #< a clean "no data" reply is definitive; exceptions stay unmarked for a next-cycle retry
                            except Exception as fe:
                                _dbmod.logger.warning("[Backfiller-%s] Cookie client failed for album %s: %s", self.user, album_id, fe)
                            stop_event.wait(1.0)

                        if fetched_albums:
                            _dbmod.logger.info("[Backfiller-%s] Cookie client fetched %d album(s)", self.user, len(fetched_albums))
                        else:
                            _dbmod.logger.warning("[Backfiller-%s] Cookie-client fallback failed to fetch any albums", self.user)

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
                            if not track_id:
                                continue
                            track_name = track_raw.get("name")
                            if track_name:
                                duration_ms = track_raw.get("duration_ms") or 0
                                self.repo.updateTrackName(track_id, track_name,
                                                          duration_ms=duration_ms if duration_ms > 0 else None)
                            # Repair path: link artists for tracks that have none
                            # (addMissingTrackArtists never touches existing links).
                            repair_artists = self._normalizeBackfillArtists(track_raw.get("artists") or [])
                            if repair_artists:
                                self.repo.addMissingTrackArtists(track_id, repair_artists)

                        updated_count += 1

                    if attempted_ids:
                        self.repo.markAlbumsBackfillAttempted(attempted_ids)

                    if updated_count > 0:
                        _dbmod.logger.info(
                            "[Backfiller-%s] Updated metadata for %d album(s)",
                            self.user, updated_count
                        )

                    # 7. Release lock on the processed IDs
                    with _dbmod.Database._backfill_lock:
                        for album_id in target_ids:
                            _dbmod.Database._active_backfills.discard(album_id)

                except Exception as e:
                    self._recordWorkerCycle("spotify_api", success=False, error=_dbmod.parseError(e))
                    _dbmod.logger.error("[Backfiller-%s] Error in metadata backfiller loop: %s", self.user, e)
                    # Cleanup registry if error occurred mid-process
                    try:
                        with _dbmod.Database._backfill_lock:
                            for album_id in target_ids:
                                _dbmod.Database._active_backfills.discard(album_id)
                    except Exception as cleanupError:
                        # Losing this cleanup leaks the ids in _active_backfills
                        # for the life of the process, and every later cycle
                        # skips those albums as "already in flight" - a backfill
                        # that quietly stops making progress on them.
                        _dbmod.logger.warning("[Backfiller-%s] Failed to release %d in-flight album ids: %s",
                                               self.user, len(target_ids), cleanupError)
                else:
                    self._recordWorkerCycle("spotify_api", success=True)

                if stop_event.wait(self.BACKFILLER_IDLE_WAIT_SECONDS):
                    break

        finally:
            _dbmod.logger.info("[Backfiller-%s] Exited gracefully", self.user)
