from __future__ import annotations

import Database.database as _dbmod  # noqa: F401 - module-global names
# (LastfmClient, requests, Importer, logger, time, Path, ...) are reached through
# the database module so the suite's patch("Database.database.X") targets keep
# working after this relocation.


class ListenerMixin:
    """Spotify listener lifecycle: connect/reconnect, live play ingestion, web-API reconcile, now-playing, and overall stop coordination."""

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

            if track:
                # Per-item isolation: if the callback raised, the listener would
                # retry the whole batch forever and record nothing new until the
                # bad item aged out of the recently-played feed. Sub-threshold
                # events are no longer split off to a separate table here -
                # appendTrackData records every event into plays, with is_skip
                # materialized from the current skip threshold.
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
                get_backfill_enabled=self.repo.isSpotifyApiBackfillEnabled,
                on_scope_status_change=self.setSpotifyNeedsReauth))
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
            if self.listener.loginFailed:
                # The stored cookies didn't authenticate at all (see
                # Listener.__init__'s isLoggedIn guard) - same DEAD-with-reason
                # treatment as contaminationDetected, instead of leaving this
                # user's Database uncached (get_user_db's except-and-rollback,
                # triggered by the AttributeError this used to raise) with
                # nothing in the UI explaining why.
                with self._health_lock:
                    self.listener_health = "DEAD"
                    self.listener_last_error = (
                        "Spotify login failed - stored cookies may be invalid or expired; "
                        "re-login to resume tracking"
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
