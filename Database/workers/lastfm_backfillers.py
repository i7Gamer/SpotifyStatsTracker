from __future__ import annotations

import Database.database as _dbmod  # noqa: F401 - module-global names
# (LastfmClient, requests, Importer, logger, time, Path, ...) are reached through
# the database module so the suite's patch("Database.database.X") targets keep
# working after this relocation.


class LastfmBackfillMixin:
    """The three Last.fm backfillers - genre tags, artist biographies, album biographies - and their shared claim/lookup/inheritance helpers."""

    def getLastfmWorkerStatus(self) -> dict:
        return {
            "configured": bool(self.repo.getUserLastfmApiKey(self.user)),
            "running": self.lastfm_thread is not None and self.lastfm_thread.is_alive(),
        }

    def getLastfmBiographyWorkerStatus(self) -> dict:
        """Same shape as getLastfmWorkerStatus, for the artist biography
        backfiller - used by the Overview "Biography Backfill Progress"
        widget's Artist row."""
        return {
            "configured": bool(self.repo.getUserLastfmApiKey(self.user)),
            "running": self.lastfm_biography_thread is not None and self.lastfm_biography_thread.is_alive(),
        }

    def getLastfmAlbumBiographyWorkerStatus(self) -> dict:
        """Same shape as getLastfmWorkerStatus, for the album biography
        backfiller - used by the Overview widget's Album row."""
        return {
            "configured": bool(self.repo.getUserLastfmApiKey(self.user)),
            "running": self.lastfm_album_biography_thread is not None and self.lastfm_album_biography_thread.is_alive(),
        }

    def startLastfmGenreBackfiller(self) -> None:
        """Start the background thread that backfills genres from Last.fm.
        No-op without a stored API key - keyless users get no idle thread,
        and the profile page's key save calls this again once a key exists."""
        if not hasattr(self, "lastfm_thread") or not hasattr(self, "lastfm_stop_event"):
            return
        if self.lastfm_thread is not None and self.lastfm_thread.is_alive():
            return
        import sqlite3
        try:
            hasApiKey = bool(self.repo.getUserLastfmApiKey(self.user))
        except sqlite3.OperationalError as e:
            # A Database constructed against a pre-1.19 file outside the
            # app's migration path (standalone script/REPL) has no
            # users.lastfm_api_key column yet - skip the worker instead of
            # crashing __init__; it starts once the migration has run.
            _dbmod.logger.warning("[LastfmWorker-%s] Not starting - schema not migrated yet: %s",
                           self.user, e)
            return
        if not hasApiKey:
            return
        # A FRESH event per run, passed into the loop: stop() joins with a
        # timeout, so a worker blocked in a slow HTTP call can outlive it -
        # reusing (and clearing) the old event here would revive that zombie
        # thread alongside the new one. With its own still-set event it exits
        # at its next loop check instead (it may finish its current batch
        # first - bounded, since per-row aborts read self.lastfm_stop_event).
        stop_event = _dbmod.threading.Event()
        self.lastfm_stop_event = stop_event
        self.lastfm_thread = _dbmod.threading.Thread(
            target=self._lastfmGenreBackfillLoop,
            args=(stop_event,),
            name=f"lastfm-genres-{self.user}",
            daemon=True
        )
        self.lastfm_thread.start()

    def stopLastfmGenreBackfiller(self) -> None:
        """Signal and wait for the background genre backfiller to stop."""
        if not hasattr(self, "lastfm_thread") or not hasattr(self, "lastfm_stop_event"):
            return
        if self.lastfm_thread is None:
            return
        self.lastfm_stop_event.set()
        self.lastfm_thread.join(timeout=3)
        self.lastfm_thread = None

    def _lastfmGenreBackfillLoop(self, stop_event: threading.Event | None = None) -> None:
        """Fetches Last.fm genre tags for this user's played artists, albums
        and tracks (most-played first), then - once the own queue is drained -
        for everyone else's (the catalog is shared, so one keyed user's worker
        converges the whole instance). Pacing comes from the process-wide rate
        limiter inside LastfmClient, not from this loop.

        `stop_event` is THIS run's private event (see the fresh-event note in
        startLastfmGenreBackfiller); the loop's own lifecycle checks use it
        exclusively so a later restart can never revive this thread."""
        import random
        if stop_event is None:
            stop_event = self.lastfm_stop_event
        try:
            # Random startup offset so per-user threads don't stampede after a restart.
            startup_delay = random.randint(self.LASTFM_BACKFILLER_MIN_START_DELAY,
                                           self.LASTFM_BACKFILLER_MAX_START_DELAY)
            _dbmod.logger.info("[LastfmWorker-%s] Starting with initial delay of %d seconds", self.user, startup_delay)
            if stop_event.wait(startup_delay):
                _dbmod.logger.info("[LastfmWorker-%s] Stopped during startup delay", self.user)
                return

            while not stop_event.is_set():
                try:
                    # Fresh read each cycle, like the API key below: an admin
                    # flip is picked up without restarting the thread, and
                    # idling (not exiting) means re-enabling resumes on its own.
                    if not self.repo.isLastfmGenreBackfillEnabled():
                        if stop_event.wait(self.LASTFM_IDLE_WAIT_SECONDS):
                            break
                        continue

                    # Fresh read each cycle: a rotated key is picked up here, a
                    # removed key ends the thread (the save handler restarts it).
                    apiKey = self.repo.getUserLastfmApiKey(self.user)
                    if not apiKey:
                        _dbmod.logger.info("[LastfmWorker-%s] No API key stored anymore - exiting", self.user)
                        return
                    client = _dbmod.LastfmClient(apiKey)

                    processedAny = self._runLastfmCycle(client, self.user)
                    if not processedAny and not stop_event.is_set():
                        processedAny = self._runLastfmCycle(client, None)   #< global queue
                    if not processedAny:
                        if stop_event.wait(self.LASTFM_IDLE_WAIT_SECONDS):
                            break
                except _dbmod._LastfmInvalidKeyError:
                    _dbmod.logger.warning("[LastfmWorker-%s] Last.fm rejected the API key (invalid/suspended) - "
                                   "idling; fix the key on the profile page", self.user)
                    if stop_event.wait(self.LASTFM_IDLE_WAIT_SECONDS):
                        break
                except Exception as e:
                    _dbmod.logger.error("[LastfmWorker-%s] Error in genre backfill loop: %s", self.user, _dbmod.parseError(e))
                    if stop_event.wait(self.LASTFM_IDLE_WAIT_SECONDS):
                        break
        finally:
            _dbmod.logger.info("[LastfmWorker-%s] Exited gracefully", self.user)

    def _runLastfmCycle(self, client: LastfmClient, scopeUsername: str | None) -> bool:
        """One batch each of artists -> albums -> tracks (that order is what
        makes same-cycle inheritance work: by the time a tag-less track is
        processed, its primary artist usually has a definitive result).
        Returns whether anything got a definitive result - False means the
        scope's queue is drained (or everything failed transiently) and the
        caller should fall through to the global queue / idle."""
        processedAny = self._processLastfmArtistBatch(client, scopeUsername)
        if self.lastfm_stop_event.is_set():
            return processedAny
        processedAny = self._processLastfmAlbumBatch(client, scopeUsername) or processedAny
        if self.lastfm_stop_event.is_set():
            return processedAny
        return self._processLastfmTrackBatch(client, scopeUsername) or processedAny

    def startLastfmBiographyBackfiller(self) -> None:
        """Start the background thread that backfills artist biographies from
        Last.fm. Runs independently of the genre backfiller (its own thread,
        stop event and idle cycle - see the LASTFM_BIOGRAPHY_* constants),
        not sequentially after it. No-op without a stored API key - keyless
        users get no idle thread, and the profile page's key save calls this
        again once a key exists."""
        if not hasattr(self, "lastfm_biography_thread") or not hasattr(self, "lastfm_biography_stop_event"):
            return
        if self.lastfm_biography_thread is not None and self.lastfm_biography_thread.is_alive():
            return
        import sqlite3
        try:
            hasApiKey = bool(self.repo.getUserLastfmApiKey(self.user))
        except sqlite3.OperationalError as e:
            # Same pre-migration guard as startLastfmGenreBackfiller: a
            # Database constructed against a pre-1.19 file outside the app's
            # migration path has no users.lastfm_api_key column yet.
            _dbmod.logger.warning("[LastfmBioWorker-%s] Not starting - schema not migrated yet: %s",
                           self.user, e)
            return
        if not hasApiKey:
            return
        # A FRESH event per run (see startLastfmGenreBackfiller's note on why
        # a lingering thread's event must never be revived).
        stop_event = _dbmod.threading.Event()
        self.lastfm_biography_stop_event = stop_event
        self.lastfm_biography_thread = _dbmod.threading.Thread(
            target=self._lastfmBiographyBackfillLoop,
            args=(stop_event,),
            name=f"lastfm-bios-{self.user}",
            daemon=True
        )
        self.lastfm_biography_thread.start()

    def stopLastfmBiographyBackfiller(self) -> None:
        """Signal and wait for the background biography backfiller to stop."""
        if not hasattr(self, "lastfm_biography_thread") or not hasattr(self, "lastfm_biography_stop_event"):
            return
        if self.lastfm_biography_thread is None:
            return
        self.lastfm_biography_stop_event.set()
        self.lastfm_biography_thread.join(timeout=3)
        self.lastfm_biography_thread = None

    def _lastfmBiographyBackfillLoop(self, stop_event: threading.Event | None = None) -> None:
        """Fetches Last.fm biographies for this user's played artists (most-
        played first), then - once the own queue is drained - for everyone
        else's, same own-then-global shape as _lastfmGenreBackfillLoop.
        Unlike lazyFetchArtistBio's on-demand one-shot fetch, an artist whose
        lookup came back with no bio is revisited after
        BIOGRAPHY_BACKFILL_RETRY_SECONDS (see getArtistsMissingBiographies).

        `stop_event` is THIS run's private event (see the fresh-event note in
        startLastfmGenreBackfiller); the loop's own lifecycle checks use it
        exclusively so a later restart can never revive this thread."""
        import random
        if stop_event is None:
            stop_event = self.lastfm_biography_stop_event
        try:
            startup_delay = random.randint(self.LASTFM_BIOGRAPHY_BACKFILLER_MIN_START_DELAY,
                                           self.LASTFM_BIOGRAPHY_BACKFILLER_MAX_START_DELAY)
            _dbmod.logger.info("[LastfmBioWorker-%s] Starting with initial delay of %d seconds", self.user, startup_delay)
            if stop_event.wait(startup_delay):
                _dbmod.logger.info("[LastfmBioWorker-%s] Stopped during startup delay", self.user)
                return

            while not stop_event.is_set():
                try:
                    # Fresh read each cycle, like the API key below: an admin
                    # flip is picked up without restarting the thread, and
                    # idling (not exiting) means re-enabling resumes on its own.
                    if not self.repo.isArtistBioEnabled():
                        if stop_event.wait(self.LASTFM_BIOGRAPHY_IDLE_WAIT_SECONDS):
                            break
                        continue

                    # Fresh read each cycle: a rotated key is picked up here, a
                    # removed key ends the thread (the save handler restarts it).
                    apiKey = self.repo.getUserLastfmApiKey(self.user)
                    if not apiKey:
                        _dbmod.logger.info("[LastfmBioWorker-%s] No API key stored anymore - exiting", self.user)
                        return
                    client = _dbmod.LastfmClient(apiKey)

                    processedAny = self._processLastfmBiographyBatch(client, self.user)
                    if not processedAny and not stop_event.is_set():
                        processedAny = self._processLastfmBiographyBatch(client, None)   #< global queue
                    if not processedAny:
                        if stop_event.wait(self.LASTFM_BIOGRAPHY_IDLE_WAIT_SECONDS):
                            break
                except _dbmod._LastfmInvalidKeyError:
                    _dbmod.logger.warning("[LastfmBioWorker-%s] Last.fm rejected the API key (invalid/suspended) - "
                                   "idling; fix the key on the profile page", self.user)
                    if stop_event.wait(self.LASTFM_BIOGRAPHY_IDLE_WAIT_SECONDS):
                        break
                except Exception as e:
                    _dbmod.logger.error("[LastfmBioWorker-%s] Error in biography backfill loop: %s", self.user, _dbmod.parseError(e))
                    if stop_event.wait(self.LASTFM_BIOGRAPHY_IDLE_WAIT_SECONDS):
                        break
        finally:
            _dbmod.logger.info("[LastfmBioWorker-%s] Exited gracefully", self.user)

    def _processLastfmBiographyBatch(self, client: LastfmClient, scopeUsername: str | None) -> bool:
        """One batch of artist.getinfo lookups. Claims/releases under the
        same "bio" kind as lazyFetchArtistBio's on-demand fetch, so the two
        paths can never double-fetch the same artist concurrently. Returns
        whether anything got a definitive result - False means the scope's
        queue is drained (or everything failed transiently) and the caller
        should fall through to the global queue / idle."""
        rows = self.repo.getArtistsMissingBiographies(self.LASTFM_BIOGRAPHY_QUEUE_BATCH_SIZE, scopeUsername)
        claimed = self._claimLastfmEntities("bio", rows)
        processedAny = False
        try:
            for row in claimed:
                if self.lastfm_biography_stop_event.is_set():
                    break
                outcome = client.getArtistInfo(row["name"], stop_event=self.lastfm_biography_stop_event)
                if outcome is None:   #< rate-limit slot aborted: we're stopping
                    break
                if outcome.status == _dbmod.OUTCOME_INVALID_KEY:
                    raise _dbmod._LastfmInvalidKeyError()
                if outcome.status == _dbmod.OUTCOME_TRANSIENT:
                    continue   #< stays unattempted, retried next cycle
                bio = outcome.bio if outcome.status == _dbmod.OUTCOME_OK else None
                self.repo.setArtistBio(row["id"], bio)
                processedAny = True
        finally:
            self._releaseLastfmEntities("bio", claimed)
        return processedAny

    def startLastfmAlbumBiographyBackfiller(self) -> None:
        """Start the background thread that backfills album biographies from
        Last.fm. Runs independently of the artist biography backfiller (its
        own thread, stop event and idle cycle - see the
        LASTFM_ALBUM_BIOGRAPHY_* constants), not sequentially after it.
        No-op without a stored API key - keyless users get no idle thread,
        and the profile page's key save calls this again once a key
        exists."""
        if not hasattr(self, "lastfm_album_biography_thread") or not hasattr(self, "lastfm_album_biography_stop_event"):
            return
        if self.lastfm_album_biography_thread is not None and self.lastfm_album_biography_thread.is_alive():
            return
        import sqlite3
        try:
            hasApiKey = bool(self.repo.getUserLastfmApiKey(self.user))
        except sqlite3.OperationalError as e:
            # Same pre-migration guard as startLastfmBiographyBackfiller: a
            # Database constructed against a pre-1.19 file outside the app's
            # migration path has no users.lastfm_api_key column yet.
            _dbmod.logger.warning("[LastfmAlbumBioWorker-%s] Not starting - schema not migrated yet: %s",
                           self.user, e)
            return
        if not hasApiKey:
            return
        # A FRESH event per run (see startLastfmGenreBackfiller's note on why
        # a lingering thread's event must never be revived).
        stop_event = _dbmod.threading.Event()
        self.lastfm_album_biography_stop_event = stop_event
        self.lastfm_album_biography_thread = _dbmod.threading.Thread(
            target=self._lastfmAlbumBiographyBackfillLoop,
            args=(stop_event,),
            name=f"lastfm-album-bios-{self.user}",
            daemon=True
        )
        self.lastfm_album_biography_thread.start()

    def stopLastfmAlbumBiographyBackfiller(self) -> None:
        """Signal and wait for the background album biography backfiller to stop."""
        if not hasattr(self, "lastfm_album_biography_thread") or not hasattr(self, "lastfm_album_biography_stop_event"):
            return
        if self.lastfm_album_biography_thread is None:
            return
        self.lastfm_album_biography_stop_event.set()
        self.lastfm_album_biography_thread.join(timeout=3)
        self.lastfm_album_biography_thread = None

    def _lastfmAlbumBiographyBackfillLoop(self, stop_event: threading.Event | None = None) -> None:
        """Fetches Last.fm biographies for this user's played albums (most-
        played first), then - once the own queue is drained - for everyone
        else's, same own-then-global shape as _lastfmBiographyBackfillLoop.
        Unlike lazyFetchAlbumBio's on-demand one-shot fetch, an album whose
        lookup came back with no bio is revisited after
        BIOGRAPHY_BACKFILL_RETRY_SECONDS (see getAlbumsMissingBiographies).

        `stop_event` is THIS run's private event (see the fresh-event note in
        startLastfmGenreBackfiller); the loop's own lifecycle checks use it
        exclusively so a later restart can never revive this thread."""
        import random
        if stop_event is None:
            stop_event = self.lastfm_album_biography_stop_event
        try:
            startup_delay = random.randint(self.LASTFM_ALBUM_BIOGRAPHY_BACKFILLER_MIN_START_DELAY,
                                           self.LASTFM_ALBUM_BIOGRAPHY_BACKFILLER_MAX_START_DELAY)
            _dbmod.logger.info("[LastfmAlbumBioWorker-%s] Starting with initial delay of %d seconds", self.user, startup_delay)
            if stop_event.wait(startup_delay):
                _dbmod.logger.info("[LastfmAlbumBioWorker-%s] Stopped during startup delay", self.user)
                return

            while not stop_event.is_set():
                try:
                    # Fresh read each cycle, like the API key below: an admin
                    # flip is picked up without restarting the thread, and
                    # idling (not exiting) means re-enabling resumes on its own.
                    if not self.repo.isAlbumBioEnabled():
                        if stop_event.wait(self.LASTFM_ALBUM_BIOGRAPHY_IDLE_WAIT_SECONDS):
                            break
                        continue

                    # Fresh read each cycle: a rotated key is picked up here, a
                    # removed key ends the thread (the save handler restarts it).
                    apiKey = self.repo.getUserLastfmApiKey(self.user)
                    if not apiKey:
                        _dbmod.logger.info("[LastfmAlbumBioWorker-%s] No API key stored anymore - exiting", self.user)
                        return
                    client = _dbmod.LastfmClient(apiKey)

                    processedAny = self._processLastfmAlbumBiographyBatch(client, self.user)
                    if not processedAny and not stop_event.is_set():
                        processedAny = self._processLastfmAlbumBiographyBatch(client, None)   #< global queue
                    if not processedAny:
                        if stop_event.wait(self.LASTFM_ALBUM_BIOGRAPHY_IDLE_WAIT_SECONDS):
                            break
                except _dbmod._LastfmInvalidKeyError:
                    _dbmod.logger.warning("[LastfmAlbumBioWorker-%s] Last.fm rejected the API key (invalid/suspended) - "
                                   "idling; fix the key on the profile page", self.user)
                    if stop_event.wait(self.LASTFM_ALBUM_BIOGRAPHY_IDLE_WAIT_SECONDS):
                        break
                except Exception as e:
                    _dbmod.logger.error("[LastfmAlbumBioWorker-%s] Error in album biography backfill loop: %s",
                                self.user, _dbmod.parseError(e))
                    if stop_event.wait(self.LASTFM_ALBUM_BIOGRAPHY_IDLE_WAIT_SECONDS):
                        break
        finally:
            _dbmod.logger.info("[LastfmAlbumBioWorker-%s] Exited gracefully", self.user)

    def _processLastfmAlbumBiographyBatch(self, client: LastfmClient, scopeUsername: str | None) -> bool:
        """One batch of album.getinfo lookups. Claims/releases under the
        "album_bio" kind - distinct from "bio" (artist bios) and "album"
        (genre lookups), so none of the three ever collide over the same
        row. Albums with no resolvable primary artist (album.getinfo needs
        one, like the genre album batch's own lookup) are marked attempted
        with no bio rather than skipped forever. Returns whether anything
        got a definitive result - False means the scope's queue is drained
        (or everything failed transiently) and the caller should fall
        through to the global queue / idle."""
        rows = self.repo.getAlbumsMissingBiographies(self.LASTFM_ALBUM_BIOGRAPHY_QUEUE_BATCH_SIZE, scopeUsername)
        claimed = self._claimLastfmEntities("album_bio", rows)
        processedAny = False
        try:
            primaries = self.repo.getAlbumPrimaryArtists([row["id"] for row in claimed])
            for row in claimed:
                if self.lastfm_album_biography_stop_event.is_set():
                    break
                primary = primaries.get(row["id"])
                if primary is None:
                    self.repo.setAlbumBio(row["id"], None)
                    processedAny = True
                    continue
                outcome = self._lastfmLookupBioOutcome(
                    lambda name: client.getAlbumInfo(primary["artist_name"], name,
                                                     stop_event=self.lastfm_album_biography_stop_event),
                    row["name"])
                if outcome is None:   #< rate-limit slot aborted: we're stopping
                    break
                if outcome.status == _dbmod.OUTCOME_INVALID_KEY:
                    raise _dbmod._LastfmInvalidKeyError()
                if outcome.status == _dbmod.OUTCOME_TRANSIENT:
                    continue   #< stays unattempted, retried next cycle

                bio = outcome.bio if outcome.status == _dbmod.OUTCOME_OK else None
                self.repo.setAlbumBio(row["id"], bio)
                processedAny = True

        finally:
            self._releaseLastfmEntities("album_bio", claimed)
        return processedAny

    def _claimLastfmEntities(self, kind: str, rows: list[dict]) -> list[dict]:
        """Process-wide in-flight dedup across users' workers (the catalog is
        shared): only rows not already claimed elsewhere are returned, and the
        caller must release them via _releaseLastfmEntities."""
        claimed = []
        with _dbmod.Database._lastfm_active_lock:
            for row in rows:
                key = (kind, row["id"])
                if key not in _dbmod.Database._lastfm_active:
                    _dbmod.Database._lastfm_active.add(key)
                    claimed.append(row)
        return claimed

    def _releaseLastfmEntities(self, kind: str, rows: list[dict]) -> None:
        with _dbmod.Database._lastfm_active_lock:
            for row in rows:
                _dbmod.Database._lastfm_active.discard((kind, row["id"]))

    @staticmethod
    def _lastfmOutcomeGenres(outcome) -> tuple[bool, list[str]]:
        """(isDefinitive, filteredGenres) for a fetch outcome. Transient
        outcomes aren't definitive - the entity stays unmarked and retries
        next cycle. Invalid-key outcomes escalate to pause the whole loop."""
        if outcome.status == _dbmod.OUTCOME_INVALID_KEY:
            raise _dbmod._LastfmInvalidKeyError()
        if outcome.status == _dbmod.OUTCOME_TRANSIENT:
            return False, []
        # OK (tags, possibly empty) and NOT_FOUND are both definitive.
        genres = _dbmod.filterTagsToGenres(outcome.tags) if outcome.status == _dbmod.OUTCOME_OK else []
        return True, genres

    def _lastfmLookupOwnGenres(self, lookup, entityName: str) -> tuple[bool, list[str], bool]:
        """(definitive, genres, aborted) for an own-tags lookup with one
        cleaned-name retry: a definitive-empty result for a decorated Spotify
        name ("Song - Radio Edit", "Song (feat. X)") re-asks with the cleaned
        form, since Last.fm frequently only knows the undecorated title.
        `lookup(name)` -> FetchOutcome | None (None = rate-limit slot aborted,
        reported via `aborted`). A non-definitive retry reports not-definitive
        so the entity stays unmarked and the pair re-runs next cycle."""
        outcome = lookup(entityName)
        if outcome is None:
            return False, [], True
        definitive, genres = self._lastfmOutcomeGenres(outcome)
        if not definitive or genres:
            return definitive, genres, False
        cleanedName = _dbmod.cleanLookupName(entityName)
        if cleanedName == entityName:
            return True, [], False
        outcome = lookup(cleanedName)
        if outcome is None:
            return False, [], True
        definitive, genres = self._lastfmOutcomeGenres(outcome)
        return definitive, genres, False

    def _lastfmLookupBioOutcome(self, lookup, entityName: str):
        """Final ArtistInfoOutcome|AlbumInfoOutcome (or None = rate-limit slot
        aborted) for a *.getinfo bio lookup with one cleaned-name retry - the
        bio counterpart of _lastfmLookupOwnGenres, sharing the exact same
        decoration-stripping fallback (cleanLookupName) so the background
        backfillers and the admin "Refresh Last.fm Data" button behave
        identically. When the verbatim name yields a definitive result
        carrying no bio, it re-asks with the cleaned form, since Last.fm often
        only knows the undecorated title ("Album (Deluxe Edition)" -> "Album").
        A non-definitive (transient/invalid-key) outcome is returned unchanged
        for the caller to classify; the retry fires only on a definitive-but-
        bioless result and its own outcome replaces the first, whatever the
        status. `lookup(name)` -> outcome | None."""
        outcome = lookup(entityName)
        if outcome is None:
            return None
        definitive = outcome.status in (_dbmod.OUTCOME_OK, _dbmod.OUTCOME_NOT_FOUND)
        if not definitive or outcome.bio is not None:
            return outcome
        cleanedName = _dbmod.cleanLookupName(entityName)
        if cleanedName == entityName:
            return outcome
        return lookup(cleanedName)

    def _processLastfmArtistBatch(self, client: LastfmClient, scopeUsername: str | None) -> bool:
        rows = self.repo.getArtistsMissingGenres(self.LASTFM_QUEUE_BATCH_SIZE, scopeUsername)
        claimed = self._claimLastfmEntities("artist", rows)
        processedAny = False
        try:
            for row in claimed:
                if self.lastfm_stop_event.is_set():
                    break
                outcome = client.getArtistTopTags(row["name"], stop_event=self.lastfm_stop_event)
                if outcome is None:   #< rate-limit slot aborted: we're stopping
                    break
                definitive, genres = self._lastfmOutcomeGenres(outcome)
                if not definitive:
                    continue
                if genres:
                    self.repo.replaceArtistGenres(row["id"], genres)
                self.repo.markArtistsLastfmAttempted([row["id"]])
                processedAny = True
        finally:
            self._releaseLastfmEntities("artist", claimed)
        return processedAny

    def _processLastfmAlbumBatch(self, client: LastfmClient, scopeUsername: str | None) -> bool:
        rows = self.repo.getAlbumsMissingGenres(self.LASTFM_QUEUE_BATCH_SIZE, scopeUsername)
        claimed = self._claimLastfmEntities("album", rows)
        processedAny = False
        try:
            primaries = self.repo.getAlbumPrimaryArtists([row["id"] for row in claimed])
            for row in claimed:
                if self.lastfm_stop_event.is_set():
                    break
                primary = primaries.get(row["id"])
                if primary is None:
                    # No derivable artist: album.getTopTags needs artist+album,
                    # and there's nothing to inherit from either.
                    self.repo.markAlbumsLastfmAttempted([row["id"]])
                    processedAny = True
                    continue
                definitive, genres, aborted = self._lastfmLookupOwnGenres(
                    lambda name: client.getAlbumTopTags(primary["artist_name"], name,
                                                        stop_event=self.lastfm_stop_event),
                    row["name"])
                if aborted:
                    break
                if not definitive:
                    continue
                if self._storeLastfmGenresWithInheritance(
                        client, "album", row["id"], genres,
                        primary["artist_id"], primary["artist_name"]):
                    processedAny = True
        finally:
            self._releaseLastfmEntities("album", claimed)
        return processedAny

    def _processLastfmTrackBatch(self, client: LastfmClient, scopeUsername: str | None) -> bool:
        rows = self.repo.getTracksMissingGenres(self.LASTFM_QUEUE_BATCH_SIZE, scopeUsername)
        claimed = self._claimLastfmEntities("track", rows)
        processedAny = False
        try:
            for row in claimed:
                if self.lastfm_stop_event.is_set():
                    break
                definitive, genres, aborted = self._lastfmLookupOwnGenres(
                    lambda name: client.getTrackTopTags(row["artist_name"], name,
                                                        stop_event=self.lastfm_stop_event),
                    row["name"])
                if aborted:
                    break
                if not definitive:
                    continue
                if self._storeLastfmGenresWithInheritance(
                        client, "track", row["id"], genres,
                        row["artist_id"], row["artist_name"], albumId=row["album_id"]):
                    processedAny = True
        finally:
            self._releaseLastfmEntities("track", claimed)
        return processedAny

    def _storeLastfmGenresWithInheritance(self, client: LastfmClient, kind: str,
                                          entityId: str, ownGenres: list[str],
                                          artistId: str, artistName: str,
                                          albumId: str | None = None) -> bool:
        """Store a definitive lookup result for a track/album. Own tags win;
        with none, a track first materializes its album's OWN genres as
        inherited rows (the closer granularity - the album batch runs earlier
        in the same cycle), then both kinds fall back to the primary artist's
        genres. An artist without a definitive result yet is resolved INLINE
        with one extra request: it may never enter the artist queue at all
        (an album's derived primary artist needs no played track), and
        leaving the entity unmarked would re-fetch it every cycle forever.
        Returns False only when the artist lookup couldn't complete
        (stopping/transient) - the entity stays unmarked and retries."""
        replaceGenres = (self.repo.replaceTrackGenres if kind == "track"
                         else self.repo.replaceAlbumGenres)
        markAttempted = (self.repo.markTracksLastfmAttempted if kind == "track"
                         else self.repo.markAlbumsLastfmAttempted)
        if ownGenres:
            replaceGenres(entityId, ownGenres, inherited=False)
            markAttempted([entityId])
            return True
        if kind == "track" and albumId is not None:
            # Only the album's own tags cascade - its inherited rows are
            # already artist genres, which the fallback below covers from
            # the (possibly fresher) artist state directly.
            albumOwnGenres = [g["genre"] for g in self.repo.getAlbumGenres(albumId)
                              if not g["inherited"]]
            if albumOwnGenres:
                replaceGenres(entityId, albumOwnGenres, inherited=True)
                markAttempted([entityId])
                return True
        artistState = self.repo.getArtistLastfmState(artistId)   #< fresh: this cycle's artist batch is visible
        if artistState["attempted_at"] is None and not artistState["genres"]:
            # Rarely duplicates another worker's in-flight artist fetch (the
            # claim set only guards batch rows) - harmless, the write is
            # idempotent.
            outcome = client.getArtistTopTags(artistName, stop_event=self.lastfm_stop_event)
            if outcome is None:
                return False
            definitive, artistGenres = self._lastfmOutcomeGenres(outcome)
            if not definitive:
                return False
            if artistGenres:
                self.repo.replaceArtistGenres(artistId, artistGenres)
            self.repo.markArtistsLastfmAttempted([artistId])
            artistState = {"attempted_at": _dbmod.time.time(), "genres": artistGenres}
        if artistState["genres"]:
            replaceGenres(entityId, artistState["genres"], inherited=True)
            markAttempted([entityId])
            return True
        markAttempted([entityId])   #< definitively tag-less everywhere
        return True
