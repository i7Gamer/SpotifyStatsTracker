from __future__ import annotations

import Database.database as _dbmod  # noqa: F401 - module-global names
# (LastfmClient, requests, Importer, logger, time, Path, ...) are reached through
# the database module so the suite's patch("Database.database.X") targets keep
# working after this relocation.


class WrappedWorkerMixin:
    """The periodic Wrapped recalculation worker and its per-year cache invalidation."""

    def getWrappedWorkerStatus(self) -> dict:
        """Same shape as getLastfmWorkerStatus, for the asynchronous Wrapped stats calculator."""
        running = hasattr(self, "wrapped_thread") and self.wrapped_thread is not None and self.wrapped_thread.is_alive()
        return {
            "configured": True,
            "running": running,
            **self._getWorkerTelemetry("wrapped"),
        }

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
                    self._recordWorkerCycle("wrapped", success=False, error=_dbmod.parseError(e))
                    _dbmod.logger.error("[WrappedWorker-%s] Error checking wrapped: %s", self.user, _dbmod.parseError(e))
                else:
                    self._recordWorkerCycle("wrapped", success=True)

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
