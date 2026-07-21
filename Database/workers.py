from __future__ import annotations

import Database.database as _dbmod  # noqa: F401 - module-global names
# (LastfmClient, requests, Importer, logger, time, Path, ...) are reached through
# the database module so the suite's patch("Database.database.X") targets keep
# working after this relocation.


class WorkerLifecycleMixin:
    """Background worker + listener lifecycle (start/stop/loops/status; Wrapped, metadata and Last.fm backfillers), mixed into Database."""

    def _addToDatabaseFromListener(self, data) -> None:
        """Record plays from the listener. Includes validation to detect cross-user
        data contamination (a bug that previously caused plays from one user to be
        recorded under another user's account)."""
        if not data:
            return
        if _dbmod.os.environ.get("FLASK_DEBUG"):
            source = data[0].get("_source", "unknown") if data else "unknown"
            _dbmod.logger.debug("_addToDatabaseFromListener called for user=%s with %d items, source=%s",
                        self.user, len(data), source)
        had_errors = False
        for item in data:
            track = item.get("track")
            timestamp = item.get("played_at")
            msPlayed = item.get("ms_played", 0)
            source = item.get("_source", "listener")



            # Reject completely unparseable or corrupt timestamps
            numeric_ts = _dbmod.timeToInt(timestamp)
            if numeric_ts <= 0:
                _dbmod.logger.warning(
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
                _dbmod.logger.error(
                    "CONTAMINATION CHECK FAILED: Track %s has timestamp %s (%.0f seconds in future). "
                    "This suggests cross-user data contamination. Skipping this play.",
                    track.get("id") if track else "unknown",
                    timestamp,
                    numeric_ts - current_time
                )
                had_errors = True
                continue

            # Sanity check: validate play duration is reasonable for a track
            # (SpotipyFree sometimes returns insane values like 7062895ms for a
            # 171s track). The played_at timestamp is still trustworthy, so
            # record the play with the track's own length - what the Web API
            # backfill would store - instead of dropping it: the recently-played
            # feed doesn't always contain the track later, and a skip then loses
            # the play for good (2026-07-17, timorzipa).
            track_duration = track.get("duration_ms", 0) if track else 0
            if track_duration > 0 and msPlayed > track_duration * self.LISTENER_DURATION_CORRUPTION_FACTOR:
                _dbmod.logger.warning(
                    "Track %s: recorded duration %dms is %dx the track's actual duration (%dms). "
                    "Likely SpotipyFree data corruption - recording with the track's actual duration instead.",
                    track.get("id"),
                    msPlayed, msPlayed // max(track_duration, 1), track_duration
                )
                msPlayed = track_duration

            # Events under the skip threshold are recorded as skip events
            # (play_skips), not plays - same boundary as the importer.
            if msPlayed < _dbmod.SKIP_THRESHOLD_MS:
                if track:
                    try:
                        self.appendSkipData(timestamp, track, msPlayed, source=source)
                    except Exception as e:
                        _dbmod.logger.error("Error recording skip for track %s from listener: %s", track.get("id"), _dbmod.parseError(e))
                        had_errors = True
                continue
            if track:
                # Per-item isolation: if the callback raised, the listener would
                # retry the whole batch forever and record nothing new until the
                # bad item aged out of the recently-played feed.
                try:
                    self.appendTrackData(timestamp, track, msPlayed, context=item.get("context", None), source=source)
                except Exception as e:
                    _dbmod.logger.error("Error adding track %s from listener: %s", track.get("id"), _dbmod.parseError(e))
                    had_errors = True
        # Mark successful poll (only if no errors occurred during processing)
        with self._health_lock:
            self.listener_last_poll_time = _dbmod.time.monotonic()
            if had_errors:
                self.listener_error_count += 1
                self.listener_last_error = "One or more tracks failed to add from listener"
                if self.listener_error_count > 5:
                    self.listener_health = "DEGRADED"
                    _dbmod.logger.warning("Listener error count exceeded threshold, marking as DEGRADED")
            else:
                self.listener_error_count = 0
                self.listener_last_error = None
                if self.listener_health != "HEALTHY":
                    self.listener_health = "HEALTHY"
                    _dbmod.logger.info("Listener recovered to HEALTHY state")

    # ---- catalog / track metadata --------------------------------------------------

    def _fetchTrackFromListener(self, trackId: str) -> dict | None:
        """Fetch and cache full metadata for a track we don't have yet, via the
        live listener client. Returns None (and logs) if the fetch fails - a play
        for an unknown track can't be recorded without its metadata, since plays
        has a foreign key to tracks."""
        if self.listener is None:
            return None
        try:
            track = _dbmod.Client.formatTrack(self.listener.track(trackId), embedPlaybackInfo=False)
            self.repo.upsertTrack(track, created_reason=f"listener_fetch (user: {self.user})")
            self.repo.commit()
            _dbmod.logger.info("Created track %s (%s) via listener fetch", trackId, track.get("name", "unknown"))
            return track
        except Exception:
            _dbmod.logger.error("Failed to download track %s", trackId)
            return None

    def _ensureTrackMetadata(self, trackId: str) -> dict | None:
        track = self.repo.getTrack(trackId)
        if track is not None:
            return track
        _dbmod.logger.info("Missing track metadata for %s, downloading it", trackId)
        return self._fetchTrackFromListener(trackId)

    def _stopRequested(self) -> bool:
        """True once this instance is being stopped or the whole app is
        shutting down - reconnect/start paths must refuse from then on."""
        return self._stopping or self.shutdown_event.is_set()

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
                    _dbmod.logger.warning(
                        "Reconnection attempt %d/%d, waiting %ds before retry",
                        attempt, self.RECONNECT_MAX_RETRIES, backoff_delay
                    )
                    # Interruptible: shutdown arriving mid-backoff aborts the
                    # wait instead of sleeping out up to RECONNECT_MAX_DELAY
                    # and reconnecting into a shutting-down process.
                    if self.shutdown_event.wait(backoff_delay):
                        _dbmod.logger.info("Reconnection abandoned for user %s: shutting down", self.user)
                        return

                if self._stopRequested():
                    _dbmod.logger.info("Reconnection abandoned for user %s: stop requested", self.user)
                    return

                try:
                    _dbmod.logger.info("Attempting to reconnect (attempt %d/%d)", attempt + 1, self.RECONNECT_MAX_RETRIES)
                    if self.startListener(email=self.email) is False:
                        _dbmod.logger.info("Reconnection abandoned for user %s: stop requested", self.user)
                        return
                    _dbmod.logger.info("Reconnection succeeded on attempt %d", attempt + 1)
                    return
                except Exception as e:
                    _dbmod.logger.warning("Reconnection attempt %d failed: %s", attempt + 1, _dbmod.parseError(e))
                    with self._health_lock:
                        self.listener_last_error = _dbmod.parseError(e)
                    if attempt == self.RECONNECT_MAX_RETRIES - 1:
                        _dbmod.logger.error(
                            "Reconnection failed after %d attempts, tracking paused for this user",
                            self.RECONNECT_MAX_RETRIES
                        )
                        with self._health_lock:
                            self.listener_health = "DEAD"

        return onStaleWithBackoff

    def startListener(self, cookiesFile=None, email=None) -> bool:
        """(Re)build and start this user's listener. Returns False when the
        start was refused or abandoned because stop/shutdown was requested;
        True otherwise. The whole body holds _listener_lock: concurrent
        reconnects (health check vs onStale) are serialized, and stop() can
        rely on the swap below never interleaving with its own teardown."""
        if self._stopRequested():
            _dbmod.logger.info("Not starting listener for user %s: stop requested", self.user)
            return False
        with self._listener_lock:
            if self._stopRequested():
                _dbmod.logger.info("Not starting listener for user %s: stop requested", self.user)
                return False
            if cookiesFile:
                self.cookiesFile = cookiesFile
            if email:
                if self.email and email != self.email:
                    _dbmod.logger.warning(
                        "Email mismatch in startListener for user %s: was %s, now %s. "
                        "This could indicate confused session state.",
                        self.user, self.email, email
                    )
                self.email = email
            if self.listener is not None:
                _dbmod.logger.info("Stopping existing listener for user %s before re-starting", self.user)
                try:
                    self.listener.stop()
                except Exception as e:
                    _dbmod.logger.error("Failed to stop existing listener for user %s: %s", self.user, _dbmod.parseError(e))
            newListener = self._withCookiesFile(lambda cf: _dbmod.Listener(
                cf, email=self.email, get_credentials=self.getUserSpotifyCredentials,
                get_backfill_enabled=self.repo.isSpotifyApiBackfillEnabled))
            if self._stopRequested():
                # stop() gave up waiting on this lock while the (slow,
                # uninterruptible) Listener login above was in flight - tear
                # the fresh listener down instead of leaving an orphan running
                # that nothing can reach (the 2026-07-17 shutdown hang).
                _dbmod.logger.info("Stop requested while listener for user %s was connecting - discarding it", self.user)
                try:
                    newListener.stop()
                except Exception as e:
                    _dbmod.logger.error("Failed to stop just-built listener for user %s: %s", self.user, _dbmod.parseError(e))
                return False
            self.listener = newListener
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
                return True
            with self._health_lock:
                self.listener_health = "HEALTHY"
                self.listener_error_count = 0
            self.listener.startListener_thread(
                callback=self._addToDatabaseFromListener,
                onStale=self._makeOnStaleCallback(),
                onWebApiSnapshot=self._reconcileWithWebApiHistory,
            )
        return True

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

    def getSpotifyApiWorkerStatus(self) -> dict:
        """Same shape as getLastfmWorkerStatus, for the Spotify API metadata
        backfiller worker thread."""
        has_creds = bool(self.repo.getUserSpotifyCredentials(self.user))
        running = hasattr(self, "backfiller_thread") and self.backfiller_thread is not None and self.backfiller_thread.is_alive()
        return {
            "configured": has_creds,
            "running": running,
        }

    def getAutoImporterWorkerStatus(self) -> dict:
        """Same shape as getLastfmWorkerStatus, for the user's autoImport drop-folder watchdog."""
        auto_imp = getattr(self, "autoImporter", None)
        wd = getattr(auto_imp, "wd", None) if auto_imp is not None else None
        thread = getattr(wd, "thread", None) if wd is not None else None
        running = thread is not None and thread.is_alive() and getattr(wd, "run", False)
        return {
            "configured": True,
            "running": running,
        }

    def getWrappedWorkerStatus(self) -> dict:
        """Same shape as getLastfmWorkerStatus, for the asynchronous Wrapped stats calculator."""
        running = hasattr(self, "wrapped_thread") and self.wrapped_thread is not None and self.wrapped_thread.is_alive()
        return {
            "configured": True,
            "running": running,
        }


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
            _dbmod.timeToInt(item["played_at"])
            for item in apiItems
            if item.get("track", {}).get("id") and item.get("played_at")
        ]
        if not apiTimes:
            _dbmod.logger.debug("Reconciliation skipped: no API items with both track id and played_at")
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
                        _dbmod.logger.debug(
                            "Reconciliation deleted duplicate play: user=%s track=%s time=%d",
                            self.user, play["id"], play["playedAt"]
                        )

        if deletedCount:
            self.repo.commit()
            _dbmod.logger.info(
                "Web API reconciliation: removed %d duplicate play(s) for user %s",
                deletedCount, self.user,
            )

    @staticmethod
    def _connectStateInt(value) -> int:
        """Connect-state numeric fields arrive as strings ("duration":
        "215000"); 0 for anything missing or malformed."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def getNowPlaying(self) -> dict | None:
        """What this user is playing right now, read from the listener's
        cached connect player_state (zero extra network calls - see
        Listener.getConnectPlayerState). None when nothing is playing, the
        state looks stale, or the track can't be identified. Track metadata
        comes from the catalog; a first-ever listen isn't in the catalog yet
        (metadata is only fetched when a play completes), so the connect
        state's own metadata is the fallback."""
        if self.listener is None:
            return None
        state = self.listener.getConnectPlayerState()
        if not state or not state.get("is_playing"):
            return None
        stateTrack = state.get("track") or {}
        trackUri = stateTrack.get("uri") or ""
        if not trackUri.startswith("spotify:track:"):
            return None   #< ads/episodes aren't tracks we can show
        trackId = trackUri.rsplit(":", 1)[-1]
        isPaused = bool(state.get("is_paused"))

        timestampMs = self._connectStateInt(state.get("timestamp"))
        positionMs = self._connectStateInt(state.get("position_as_of_timestamp"))
        durationMs = self._connectStateInt(state.get("duration"))
        # Standard connect-state position math: the state only updates on
        # play/pause/seek/track change, so the live position is the snapshot
        # position plus time elapsed since the snapshot (unless paused).
        elapsedMs = max(0, int(_dbmod.time.time() * 1000) - timestampMs) if timestampMs else 0
        currentPositionMs = positionMs if isPaused else positionMs + elapsedMs
        if not isPaused and durationMs and timestampMs and currentPositionMs > durationMs + self.NOW_PLAYING_STALE_GRACE_MS:
            return None
        if durationMs:
            currentPositionMs = min(currentPositionMs, durationMs)

        track = self.repo.getTrack(trackId)
        if track:
            name = track.get("name")
            artistsText = ", ".join(a.get("name", "") for a in track.get("artists", []))
            imageId = track.get("imageId")
        else:
            stateMeta = stateTrack.get("metadata") or {}
            # spotapi may have already hydrated metadata into a Metadata
            # dataclass (which is truthy but has no .get()), so handle both.
            if isinstance(stateMeta, dict):
                name = stateMeta.get("title")
                artistsText = stateMeta.get("artist_name") or ""
                imageId = _dbmod._imageIdFromConnectMeta(stateMeta)
                imageUrl = _dbmod._imageUrlFromConnectMeta(stateMeta)
            else:
                _dbmod.logger.warning(
                    "getNowPlaying: unexpected metadata type %s for track %s "
                    "(stateTrack type=%s, value=%r); falling back to getattr",
                    type(stateMeta).__name__, trackId,
                    type(stateTrack).__name__, stateMeta,
                )
                name = getattr(stateMeta, "title", None)
                artistsText = getattr(stateMeta, "artist_name", None) or ""
                imageId = _dbmod._imageIdFromConnectMeta(stateMeta)
                imageUrl = _dbmod._imageUrlFromConnectMeta(stateMeta)
            # Kick off a background download so the cover is ready on the next
            # poll (or shortly after). saveTrackImg is fire-and-forget and
            # already deduped via tryClaimImageDownload.
            if imageId and imageUrl:
                self.saveTrackImg(imageUrl, imageId)

        if not name:
            return None   #< nothing presentable to show

        return {
            "trackId": trackId,
            "name": name,
            "artistsText": artistsText,
            "imageId": imageId,
            "isPaused": isPaused,
            "positionMs": currentPositionMs,
            "durationMs": durationMs,
        }

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
                seconds_since_last_poll = _dbmod.time.monotonic() - self.listener_last_poll_time
            return {
                "status": self.listener_health,
                "error_count": self.listener_error_count,
                "last_error": self.listener_last_error,
                "seconds_since_last_poll": seconds_since_last_poll,
            }

    def signalStop(self) -> None:
        """Phase 1 of shutdown: flip every stop flag/event for this user
        WITHOUT joining or closing anything. shutdown() calls this for every
        user before any (potentially slow) join runs, closing the window where
        one user's still-running listener fires a stale-feed reconnect while
        another user's threads are being joined (the 2026-07-17 hang).
        Permanent: a signaled instance never starts a listener again."""
        self._stopping = True
        listener = self.listener
        if listener is not None:
            try:
                listener.signalStop()
            except Exception as e:
                _dbmod.logger.error("Error signaling listener stop for %s: %s", self.user, _dbmod.parseError(e))
        wd = getattr(self.autoImporter, "wd", None)
        if wd is not None:
            wd.signalStop()
        for eventName in ("backfiller_stop_event", "wrapped_stop_event", "lastfm_stop_event",
                         "lastfm_biography_stop_event"):
            event = getattr(self, eventName, None)
            if event is not None:
                event.set()

    def stop(self):
        # Signal first even when called directly (idempotent when shutdown()
        # already ran signalStop): every thread starts winding down before the
        # joins below block.
        self.signalStop()
        acquired = self._listener_lock.acquire(timeout=self.LISTENER_STOP_LOCK_TIMEOUT_SECONDS)
        # On timeout an in-flight startListener holds the lock (a live Spotify
        # login) - proceed anyway: it re-checks _stopping after connecting and
        # discards its own listener, and stopping the current listener without
        # the lock is safe (Listener.stop() is idempotent).
        try:
            if self.listener is not None:
                self.listener.stop()
        finally:
            if acquired:
                self._listener_lock.release()
        self.autoImporter.wd.stop()
        self.stopMetadataBackfiller()
        self.stopWrappedCalculationsWorker()
        self.stopLastfmGenreBackfiller()
        self.stopLastfmBiographyBackfiller()
        self.stopLastfmAlbumBiographyBackfiller()

    def startWrappedCalculationsWorker(self) -> None:
        """Start the background thread to precalculate wrapped data."""
        if not hasattr(self, "wrapped_thread") or not hasattr(self, "wrapped_stop_event"):
            return
        if self.wrapped_thread is not None and self.wrapped_thread.is_alive():
            return
        # A FRESH event per run (see startLastfmGenreBackfiller): never revive
        # a thread that outlived stop()'s join timeout by clearing its event.
        stop_event = _dbmod.threading.Event()
        self.wrapped_stop_event = stop_event
        self.wrapped_thread = _dbmod.threading.Thread(
            target=self._wrappedCalculationsLoop,
            args=(stop_event,),
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

    def _wrappedCalculationsLoop(self, stop_event: threading.Event | None = None) -> None:
        """Periodically checks if plays have changed and recalculates wrapped stats.

        `stop_event` is THIS run's private event (see the fresh-event note in
        startWrappedCalculationsWorker) - a later restart can never revive
        this thread."""
        import random
        if stop_event is None:
            stop_event = self.wrapped_stop_event
        try:
            # 1. Random startup delay to distribute CPU load if multiple users are loaded
            startup_delay = random.randint(self.WRAPPED_WORKER_MIN_START_DELAY, self.WRAPPED_WORKER_MAX_START_DELAY)
            _dbmod.logger.info("[WrappedWorker-%s] Starting with initial delay of %d seconds", self.user, startup_delay)
            if stop_event.wait(startup_delay):
                return

            while not stop_event.is_set():
                try:
                    self._checkAndRecalculateWrapped(stop_event)
                except Exception as e:
                    _dbmod.logger.error("[WrappedWorker-%s] Error checking wrapped: %s", self.user, _dbmod.parseError(e))

                # Check loop interval
                if stop_event.wait(self.WRAPPED_WORKER_LOOP_INTERVAL):
                    break
        except Exception as e:
            _dbmod.logger.error("[WrappedWorker-%s] Worker loop crashed: %s", self.user, _dbmod.parseError(e))

    def _getWrappedRecalcLock(self, year: int) -> threading.Lock:
        """Per-(user instance, year) lock so the periodic worker and an
        on-demand /wrapped recalculation never run _calculateAndSaveWrapped
        for the same year at the same time."""
        with self._wrapped_recalc_locks_guard:
            lock = self._wrapped_recalc_locks.get(year)
            if lock is None:
                lock = _dbmod.threading.Lock()
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

    def _checkAndRecalculateWrapped(self, stop_event: threading.Event | None = None) -> None:
        """Checks for each year if there is new data and triggers recalculation if needed."""
        if stop_event is None:
            stop_event = self.wrapped_stop_event
        nowLocal = _dbmod.datetime.datetime.now(tz=self.tz)
        currentYear = nowLocal.year

        oldestEntries = self.getEntriesFromOld(count=1, fullPagination=False)
        earliestYear = _dbmod.convertToDatetime(oldestEntries[0]["playedAt"], tz=self.tz).year if oldestEntries else currentYear
        availableYears = list(range(currentYear, earliestYear - 1, -1))

        for year in availableYears:
            if stop_event.is_set():
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
                _dbmod.logger.info("[WrappedWorker-%s] Year %d recalculation already in progress elsewhere, skipping this cycle", self.user, year)
                continue
            try:
                cachedMaxDisplay = _dbmod.convertToDatetime(cached_max, tz=self.tz).isoformat() if cached_max is not None else "none"
                actualMaxDisplay = _dbmod.convertToDatetime(max_played_at, tz=self.tz).isoformat()
                _dbmod.logger.info("[WrappedWorker-%s] Recalculating wrapped for year %d (cached max: %s, actual max: %s, cached plays: %s, actual plays: %s)",
                            self.user, year, cachedMaxDisplay, actualMaxDisplay, str(cached_total), str(current_total))
                self._calculateAndSaveWrapped(year, yearStart, yearEnd, max_played_at)
            finally:
                lock.release()
            # Sleep briefly between years to distribute database load
            if stop_event.wait(self.WRAPPED_YEAR_DELAY_SECONDS):
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
            "calculated_at": _dbmod.time.time(),
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
            "time_series_day": _dbmod.json.dumps(timeSeriesDay),
            "time_series_week": _dbmod.json.dumps(timeSeriesWeek),
            "time_series_month": _dbmod.json.dumps(timeSeriesMonth),
            "top_songs": _dbmod.json.dumps(topSongs),
            "top_artists": _dbmod.json.dumps(topArtists),
            "top_albums": _dbmod.json.dumps(topAlbums),
            "discovered_songs_list": _dbmod.json.dumps(discoveredSongsList),
            "discovered_artists_list": _dbmod.json.dumps(discoveredArtistsList),
            "discovered_albums_list": _dbmod.json.dumps(discoveredAlbumsList),
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
        nowLocal = _dbmod.datetime.datetime.now(tz=self.tz)
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
                outcome = client.getAlbumInfo(primary["artist_name"], row["name"],
                                              stop_event=self.lastfm_album_biography_stop_event)
                if outcome is None:   #< rate-limit slot aborted: we're stopping
                    break
                if outcome.status == _dbmod.OUTCOME_INVALID_KEY:
                    raise _dbmod._LastfmInvalidKeyError()
                if outcome.status == _dbmod.OUTCOME_TRANSIENT:
                    continue   #< stays unattempted, retried next cycle

                bio = outcome.bio if outcome.status == _dbmod.OUTCOME_OK else None
                if bio is None:
                    cleanedName = _dbmod.cleanLookupName(row["name"])
                    if cleanedName != row["name"]:
                        retryOutcome = client.getAlbumInfo(primary["artist_name"], cleanedName,
                                                             stop_event=self.lastfm_album_biography_stop_event)
                        if retryOutcome is None:
                            break
                        if retryOutcome.status == _dbmod.OUTCOME_INVALID_KEY:
                            raise _dbmod._LastfmInvalidKeyError()
                        if retryOutcome.status == _dbmod.OUTCOME_TRANSIENT:
                            continue
                        bio = retryOutcome.bio if retryOutcome.status == _dbmod.OUTCOME_OK else None

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
