from __future__ import annotations

import Database.database as _dbmod  # noqa: F401 - module-global names
# (LastfmClient, requests, Importer, logger, time, Path, ...) are reached through
# the database module so the suite's patch("Database.database.X") targets keep
# working after this relocation.


class MetadataBackfillMixin:
    """The Spotify Web-API metadata backfiller (missing album/track dates, artistless tracks)."""

    def getSpotifyApiWorkerStatus(self) -> dict:
        """Same shape as getLastfmWorkerStatus, for the Spotify API metadata
        backfiller worker thread."""
        has_creds = bool(self.repo.getUserSpotifyCredentials(self.user))
        running = hasattr(self, "backfiller_thread") and self.backfiller_thread is not None and self.backfiller_thread.is_alive()
        return {
            "configured": has_creds,
            "running": running,
        }

    def startMetadataBackfiller(self) -> None:
        """Start the background thread to fill in missing album metadata."""
        if not hasattr(self, "backfiller_thread") or not hasattr(self, "backfiller_stop_event"):
            return
        if self.backfiller_thread is not None and self.backfiller_thread.is_alive():
            return
        # A FRESH event per run (see startLastfmGenreBackfiller): stop() joins
        # with a timeout, so a thread blocked in a slow fetch can outlive it -
        # clearing a shared event here would revive that zombie alongside the
        # new thread. With its own still-set event it exits on its own instead.
        stop_event = _dbmod.threading.Event()
        self.backfiller_stop_event = stop_event
        self.backfiller_thread = _dbmod.threading.Thread(
            target=self._metadataBackfillLoop,
            args=(stop_event,),
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

    @staticmethod
    def _normalizeBackfillArtists(artistsRaw: list) -> list[dict]:
        """Repo-shaped artist dicts from an album payload's per-track artist
        list (Web API and SpotipyFree both expose id/name/external_urls).
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
                                if len(target_ids) >= 20:  # Spotify bulk limit is 20
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
                                if _dbmod.os.environ.get("FLASK_DEBUG", "").lower() in _dbmod.TRUTHY_DEBUG_VALUES:
                                    _dbmod.logger.warning(
                                        "[Backfiller-%s] Spotify Web API returned status %d. Falling back to SpotipyFree.",
                                        self.user, resp.status_code
                                    )
                        else:
                            _dbmod.logger.warning("[Backfiller-%s] Failed to refresh access token. Falling back to SpotipyFree.", self.user)

                    if use_fallback:
                        import SpotipyFree
                        import time
                        try:
                            cookiesFile = self._materializeCookiesFile()
                            sp = SpotipyFree.Spotify(cookiesFile=str(cookiesFile))
                            for album_id in target_ids:
                                if stop_event.is_set():
                                    break
                                try:
                                    album_raw = sp.album(album_id)
                                    if album_raw:
                                        fetched_albums.append(album_raw)
                                    attempted_ids.append(album_id)  #< a clean "no data" reply is definitive; exceptions stay unmarked for a next-cycle retry
                                except Exception as fe:
                                    _dbmod.logger.warning("[Backfiller-%s] SpotipyFree failed for album %s: %s", self.user, album_id, fe)
                                stop_event.wait(1.0)
                        finally:
                            cookiesFile.unlink(missing_ok=True)

                        if fetched_albums:
                            _dbmod.logger.info("[Backfiller-%s] SpotipyFree fetched %d album(s)", self.user, len(fetched_albums))
                        else:
                            _dbmod.logger.warning("[Backfiller-%s] SpotipyFree fallback failed to fetch any albums", self.user)

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
                    _dbmod.logger.error("[Backfiller-%s] Error in metadata backfiller loop: %s", self.user, e)
                    # Cleanup registry if error occurred mid-process
                    try:
                        with _dbmod.Database._backfill_lock:
                            for album_id in target_ids:
                                _dbmod.Database._active_backfills.discard(album_id)
                    except Exception:
                        pass

                if stop_event.wait(self.BACKFILLER_IDLE_WAIT_SECONDS):
                    break

        finally:
            _dbmod.logger.info("[Backfiller-%s] Exited gracefully", self.user)
