from __future__ import annotations

import Database.database as _dbmod  # noqa: F401 - module-global names
# (LastfmClient, requests, Importer, logger, time, Path, ...) are reached through
# the database module so the suite's patch("Database.database.X") targets keep
# working after this relocation.


class ImportMixin:
    """History import / reconciliation (append*, importHistory*, overwrite range), mixed into Database."""

    # ---- writing plays ---------------------------------------------------------------

    def appendEntries(self, entry: dict):
        """Record a single play. Named for compatibility with the previous
        JSON-backed API (it always took one entry despite the plural name)."""
        if not entry:
            return
        self.repo.insertPlay(self.user, entry["id"], entry["playedAt"], entry["timePlayed"], entry.get("playedFrom"),
                              created_reason=f"manual_entry (user: {self.user})")
        self.repo.commit()

    def appendMetadata(self, meta: dict, created_reason: str | None = None) -> bool:
        self.saveImagesFromTrack(meta)
        entry, track = self._splitEntryAndTrack(meta)
        self.repo.upsertTrack(track, created_reason=created_reason)
        # Classify against the current threshold + the track's duration (percent
        # mode needs it); a sub-threshold event now lands as is_skip=1 in plays
        # rather than in a separate table.
        is_skip = self.repo.computeIsSkip(entry["timePlayed"], track.get("duration"))
        was_inserted = self.repo.insertPlay(self.user, entry["id"], entry["playedAt"], entry["timePlayed"], entry.get("playedFrom"),
                              created_reason=created_reason, is_skip=is_skip)
        self.repo.commit()
        self.updatePlaylists(entry.get("playedFrom"))
        return was_inserted

    def appendTrackData(self, timestamp, track, timePlayed, context=None, source="listener"):
        formatted_track = _dbmod.Client.formatTrack(track, timestamp, timePlayed, context=context)
        track_id = track.get("id", "unknown")
        track_name = track.get("name", "unknown")

        if source == self.WEB_API_BACKFILL_SOURCE:
            # Wide, defense-in-depth guard: skip if this exact track already has a
            # play within (duration + 60s) of this one. Deliberately NOT applied to
            # the live listener's own inserts (source == "listener") - the listener
            # is the primary, trusted source, and a genuine short-track replay
            # within this window is normal listening behavior that must not be
            # silently dropped. Backfill is a catch-up mechanism and should be
            # conservative about re-adding something a trusted source may already
            # have captured - this window is symmetric so it catches a duplicate
            # regardless of whether Spotify reported this entry's played_at as a
            # start or end time (see _checkWebApiBackfill for why that can't be
            # assumed one way or the other).
            durationSeconds = (track.get("duration_ms", 0) or 0) // 1000
            tolerance = durationSeconds + self.BACKFILL_INSERT_GUARD_EXTRA_SECONDS
            if self.repo.hasPlayNearTime(self.user, track_id, formatted_track["playedAt"], tolerance):
                if _dbmod.os.environ.get("FLASK_DEBUG", "").lower() in _dbmod.TRUTHY_DEBUG_VALUES:
                    _dbmod.logger.info(
                        "Skipping backfilled play for track %s (%s): an existing play already exists "
                        "within %ds (duration+60s) of played_at=%s",
                        track_id, track_name, tolerance, formatted_track["playedAt"],
                    )
                return False

        created_reason = f"{source}_play (user: {self.user})"
        was_inserted = self.appendMetadata(formatted_track, created_reason=created_reason)
        if was_inserted:
            _dbmod.logger.info(
                "Recording play for user %s: track=%s (%s), timestamp=%s, duration=%dms, source=%s",
                self.user, track_id, track_name, timestamp, timePlayed, source
            )
        return was_inserted

    def importHistory(self, exportedHistory, progressPrefix: str = "", isFinalFile: bool = True, hasPriorError: bool = False, track_file_hash: bool = False,
                      runState: _ImportRunState | None = None, deferCommit: bool = False):
        """Import one export file. Serialized per user via _importLock (see
        its comment in __init__); the actual work is in _importHistoryLocked."""
        with self._importLock:
            return self._importHistoryLocked(exportedHistory, progressPrefix, isFinalFile, hasPriorError,
                                             track_file_hash, runState, deferCommit)

    def _importHistoryLocked(self, exportedHistory, progressPrefix: str = "", isFinalFile: bool = True, hasPriorError: bool = False, track_file_hash: bool = False,
                             runState: _ImportRunState | None = None, deferCommit: bool = False):
        importer = self._withCookiesFile(lambda cookiesFile: _dbmod.Importer(cookiesFile=cookiesFile, email=self.email))
        if runState is None:
            runState = _dbmod._ImportRunState()

        # INVARIANT: repo methods that self-commit ("with conn:" - writeProgress,
        # image-status writes, playlist upserts) run on this same thread-local
        # connection, and "with conn:" commits WHATEVER is pending on it. In
        # single-file mode that's only safe while no import rows are staged in
        # the transaction: during the staging loop below (which writes nothing
        # to the tracks/plays tables) or after the final commit()/rollback().
        # In deferCommit mode (an atomic overwrite batch) that window doesn't
        # exist - a PRIOR file in the same batch may already have staged
        # uncommitted writes - so every progress write below is routed through
        # reportProgress, which no-ops instead of self-committing, for the
        # whole duration of deferCommit mode.
        def reportProgress(status, current, totalSteps, message, error=False):
            if deferCommit:
                return
            self.writeProgress(status, current, totalSteps, message, error=error)

        parsedHistory, exportType = importer._convertToList(exportedHistory)
        if exportType == "None":
            # Unrecognized content (corrupt JSON, a file read mid-copy, the
            # wrong file entirely) must fail loudly: returning silently here
            # used to make AutoImporter move never-imported files to DONE/ as
            # successes and the web UI report the import as complete.
            reportProgress("failed", 0, 0,
                           f"{progressPrefix}Import failed: unrecognized or corrupt export file", error=True)
            raise ValueError("Unrecognized or corrupt export file - expected a Spotify JSON export or Musicolet CSV backup")
        if not parsedHistory:
            return

        total = len(parsedHistory)
        reportProgress("running", 0, total, f"{progressPrefix}Starting import", error=hasPriorError)

        def progressCallback(status, current, totalSteps, message):
            reportProgress(status, current, totalSteps, f"{progressPrefix}{message}", error=hasPriorError)

        # Imported tracks/plays are staged locally and only written to the database
        # once the whole import has succeeded. SQLite only allows one writer
        # transaction at a time, so committing incrementally here would either
        # block progress-polling reads for the whole import, or (worse) let a
        # failure partway through leave a half-imported batch committed.
        stagedTracks: dict[str, dict] = {}
        stagedPlays: list[dict] = []
        index = 0
        # Rolled-back writes must not stay claimed in a batch-shared run state
        claimedRowIdsBefore = set(runState.claimedRowIds)
        insertedPlayKeysBefore = set(runState.insertedPlayKeys)
        try:
            knownTracks = self.repo.getAllTracks()
            importStats = {}
            for index, meta in enumerate(
                importer.importHistory(parsedHistory, knownTracks, exportType, progressCallback=progressCallback,
                                       stats=importStats),
                start=1,
            ):
                entry, track = self._splitEntryAndTrack(meta)
                stagedTracks[track["id"]] = track
                stagedPlays.append(entry)
                if deferCommit:
                    # saveImagesFromTrack -> tryClaimImageDownload also
                    # self-commits (same INVARIANT as reportProgress above) -
                    # claim images for the whole batch only after the final
                    # commit succeeds, see _importHistoryBatchOverwriteLocked.
                    runState.pendingImageTracks[track["id"]] = track
                else:
                    self.saveImagesFromTrack(track)

                if index % self.PROGRESS_UPDATE_INTERVAL == 0 or index == total:
                    reportProgress("running", index, total, f"{progressPrefix}Imported {index} of {total}")

            for track in stagedTracks.values():
                self.repo.upsertTrack(track, created_reason=f"history_import (user: {self.user})")

            insertedCount = 0
            updatedCount = 0
            enrichedCount = 0
            skipsSavedCount = 0
            correctedYears = set()
            behavioralSetSql = ", ".join(f"{column} = COALESCE(?, {column})" for column in _dbmod.BEHAVIORAL_COLUMNS)
            # Fetch the skip threshold once for the whole batch so each row's
            # is_skip is computed without a per-row settings read.
            skipThreshold = self.repo.getSkipThreshold()
            for entry in stagedPlays:
                track_id = entry["id"]
                played_at = entry["playedAt"]
                time_played = entry["timePlayed"]
                played_from = entry.get("playedFrom")
                extras = entry.get("importExtras") or {}
                extrasValues = [extras.get(column) for column in _dbmod.BEHAVIORAL_COLUMNS]

                # Sub-5s events (entry["isSkip"], the fixed import floor) bypass
                # near-time play matching entirely: they must never claim/correct
                # a real play row, and their dedup is plays' UNIQUE constraint.
                # They're always is_skip=1 (the stats threshold is >= 5s).
                if entry.get("isSkip"):
                    if self.repo.insertPlay(self.user, track_id, played_at, time_played,
                                            created_reason=f"history_import (user: {self.user})",
                                            extras=entry.get("importExtras"), is_skip=1):
                        skipsSavedCount += 1
                    continue

                # Check if a play for this track already exists within (duration + 60s) tolerance,
                # same logic as API backfill to handle potential overlap with backfilled data
                # where Spotify's played_at can be ambiguous (start or end time).
                track = stagedTracks.get(track_id)
                #< staged tracks carry Client.formatTrack's "duration" key (ms)
                durationSeconds = (track.get("duration", 0) or 0) // 1000 if track else 0
                tolerance = durationSeconds + self.BACKFILL_INSERT_GUARD_EXTRA_SECONDS
                raw_matches = self.repo.getPlaysNearTime(self.user, track_id, played_at, tolerance)
                matches = []
                for m in raw_matches:
                    # Rows this run already wrote belong to other import entries and
                    # are never candidates - otherwise a replay would "correct" the
                    # skip play inserted moments earlier instead of being recorded
                    # itself (see _ImportRunState).
                    if runState.isOwnWrite(track_id, m):
                        continue
                    db_played_at = m["played_at"]
                    diff_start = abs(db_played_at - played_at)
                    diff_end = abs(db_played_at - (played_at + durationSeconds))
                    if diff_start <= self.IMPORT_MATCH_START_WINDOW_SECONDS or diff_end <= self.IMPORT_MATCH_END_WINDOW_SECONDS:
                        matches.append(m)

                if matches:
                    if len(matches) == 1:
                        # Exactly one match - safe to update if data differs
                        existing_play = matches[0]
                        runState.claimedRowIds.add(existing_play["id"])
                        data_differs = (
                            existing_play["time_played"] != time_played or
                            existing_play["played_at"] != played_at
                        )
                        # Behavioral columns the import can fill/correct on the
                        # matched row - a non-null import value wins, a None
                        # never clobbers a stored one (COALESCE below).
                        extras_differ = any(
                            extras.get(column) is not None and extras.get(column) != existing_play.get(column)
                            for column in _dbmod.BEHAVIORAL_COLUMNS
                        )

                        if data_differs:
                            # Update both fields with imported data (more accurate source).
                            # A corrected time_played can cross the skip threshold, so
                            # is_skip is recomputed alongside it.
                            conn = self.repo._conn()
                            corrected_is_skip = self.repo.computeIsSkip(
                                time_played, track.get("duration") if track else None, threshold=skipThreshold)
                            conn.execute(
                                f"UPDATE plays SET played_at = ?, time_played = ?, is_skip = ?, {behavioralSetSql} WHERE id = ?",
                                (played_at, time_played, corrected_is_skip, *extrasValues, existing_play["id"])
                            )
                            changes = []
                            if int(existing_play["played_at"]) != int(played_at):
                                changes.append(f"played_at corrected from {int(existing_play['played_at'])} to {int(played_at)}")
                            if existing_play["time_played"] != time_played:
                                changes.append(f"time_played corrected from {existing_play['time_played']}ms to {time_played}ms")

                            _dbmod.logger.info(
                                "Updated import play for track %s: %s",
                                track_id, ", ".join(changes)
                            )
                            updatedCount += 1
                            # A correction can move a play without changing its
                            # year's play count or max timestamp - invisible to
                            # _wrappedCacheNeedsRecalc, so those years' cached
                            # Wrapped is dropped after commit (see below).
                            correctedYears.add(_dbmod.convertToDatetime(existing_play["played_at"], tz=self.tz).year)
                            correctedYears.add(_dbmod.convertToDatetime(played_at, tz=self.tz).year)
                            continue
                        elif extras_differ:
                            # Same play, but this import carries behavioral
                            # metadata the row lacks - backfill it in place.
                            conn = self.repo._conn()
                            conn.execute(
                                f"UPDATE plays SET {behavioralSetSql} WHERE id = ?",
                                (*extrasValues, existing_play["id"])
                            )
                            enrichedCount += 1
                            continue
                        else:
                            # Data matches - skip, no update needed
                            if _dbmod.os.environ.get("FLASK_DEBUG", "").lower() in _dbmod.TRUTHY_DEBUG_VALUES:
                                _dbmod.logger.info(
                                    "Skipping import play for track %s: duplicate found with identical data",
                                    track_id,
                                )
                            continue
                    else:
                        # Multiple matches - ambiguous, skip to avoid wrong update
                        if _dbmod.os.environ.get("FLASK_DEBUG", "").lower() in _dbmod.TRUTHY_DEBUG_VALUES:
                            _dbmod.logger.info(
                                "Skipping import play for track %s: %d plays found within tolerance - ambiguous, "
                                "not updating to avoid wrong match",
                                track_id, len(matches),
                            )
                        continue

                # If no matches, proceed to insert as usual. is_skip uses the
                # batch threshold + this track's duration (percent mode).
                is_skip = self.repo.computeIsSkip(
                    time_played, track.get("duration") if track else None, threshold=skipThreshold)
                if self.repo.insertPlay(self.user, track_id, played_at, time_played, played_from,
                                        created_reason=f"history_import (user: {self.user})",
                                        extras=entry.get("importExtras"), is_skip=is_skip):
                    insertedCount += 1
                runState.insertedPlayKeys.add((track_id, played_at))

            if track_file_hash:
                import hashlib
                content_bytes = exportedHistory.encode("utf-8") if isinstance(exportedHistory, str) else str(exportedHistory).encode("utf-8")
                file_hash = hashlib.sha256(content_bytes).hexdigest()
                self.repo.markFileImported(self.user, file_hash)

            if deferCommit:
                # Atomic overwrite batch: the caller commits once for the
                # whole batch. deleteUserWrapped self-commits (INVARIANT
                # above), so invalidating now would flush this transaction's
                # still-uncommitted writes early - the caller invalidates
                # these years itself after its own commit succeeds.
                runState.correctedYears |= correctedYears
            else:
                self.repo.commit()

                # INVARIANT-safe only here: deleteUserWrapped self-commits, so it
                # must never run while import rows are staged. Corrections can be
                # invisible to _wrappedCacheNeedsRecalc (play count and max
                # played_at unchanged) - drop the touched years' cache explicitly.
                for year in sorted(correctedYears):
                    self.repo.deleteUserWrapped(self.user, year)

            droppedNoTrack = importStats.get("droppedNoTrack", 0)
            summary = (f"{insertedCount} new, {updatedCount} corrected, {enrichedCount} enriched, "
                       f"{skipsSavedCount} skips saved")
            if droppedNoTrack:
                summary += f", {droppedNoTrack} without track info dropped"
            _dbmod.logger.info("Imported %d tracks for user %s: %s", len(stagedTracks), self.user, summary)

            status = "complete" if isFinalFile else "running"
            reportProgress(status, total, total, f"{progressPrefix}Import complete: {summary}", error=hasPriorError)
        except Exception as e:
            self.repo.rollback()
            runState.claimedRowIds = claimedRowIdsBefore
            runState.insertedPlayKeys = insertedPlayKeysBefore
            self.writeProgress("failed", index, total, f"{progressPrefix}Import failed: {_dbmod.parseError(e)}", error=True)
            raise

    def importHistoryBatch(self, fileContents: list[str], overwriteRange: bool = False) -> list[str]:
        """Import multiple export files sequentially - cached up front by the
        caller (app.py reads every upload before starting this thread) and then
        processed one after another, mirroring AutoImporter's existing
        one-file-at-a-time folder-watching behavior. Serialized per user via
        _importLock (see its comment in __init__).

        overwriteRange=False: a failure in one file is logged and skipped
        rather than aborting the whole batch, so a single bad upload doesn't
        block the rest. Returns one outcome per input file, in order -
        "imported", "skipped" (already imported before, by hash), or "failed"
        - so AutoImporter can route each file to DONE/ or FAILED/ instead of
        assuming success.

        overwriteRange=True: the covered-range delete (see _deleteCoveredRange)
        and every file's import share ONE transaction - see
        _importHistoryBatchOverwriteLocked. A failure anywhere aborts the
        whole batch and rolls back everything, so either every file's data
        lands or none of it does; the returned outcomes are all "imported" or
        all "failed" accordingly. Also bypasses the already-imported hash gate
        so unchanged files re-import fresh."""
        with self._importLock:
            if overwriteRange:
                outcomes = self._importHistoryBatchOverwriteLocked(fileContents)
            else:
                outcomes = self._importHistoryBatchLocked(fileContents)
        if "imported" in outcomes:
            # Milestone achieved_at dates derive from play history, which this
            # batch just changed - raise the marker the periodic milestone pass
            # consumes to re-derive them (see Database.consumeMilestoneRecalcFlag).
            # All-skipped/all-failed batches changed nothing, so nothing is due.
            self.milestonesRecalcPending = True
        return outcomes

    def _importHistoryBatchLocked(self, fileContents: list[str]) -> list[str]:
        if not fileContents:
            return []

        import hashlib
        total = len(fileContents)
        outcomes: list[str] = []

        # One run state for the whole batch: files commit separately, so a
        # skip/replay pair straddling a file boundary would otherwise collapse
        # (the replay in file N+1 matching the skip committed by file N).
        runState = _dbmod._ImportRunState()
        for index, content in enumerate(fileContents, start=1):
            failedSoFar = outcomes.count("failed")
            try:
                isFinalFile = (index == total)
                content_bytes = content.encode("utf-8") if isinstance(content, str) else str(content).encode("utf-8")
                file_hash = hashlib.sha256(content_bytes).hexdigest()

                if self.repo.isFileImported(self.user, file_hash):
                    _dbmod.logger.info("File %s/%s already imported (hash: %s). Skipping.", index, total, file_hash)
                    outcomes.append("skipped")
                    status = "complete" if isFinalFile else "running"
                    self.writeProgress(status, index, total, f"File {index}/{total}: Skipping already imported file", error=(failedSoFar > 0))
                    continue

                self.importHistory(
                    content,
                    progressPrefix=f"File {index}/{total}: ",
                    isFinalFile=isFinalFile,
                    hasPriorError=(failedSoFar > 0),
                    track_file_hash=True,
                    runState=runState
                )
                outcomes.append("imported")
            except Exception as e:
                outcomes.append("failed")
                _dbmod.logger.error("Import failed for file %s/%s: %s", index, total, _dbmod.parseError(e))

        failedCount = outcomes.count("failed")
        skippedCount = outcomes.count("skipped")
        succeededCount = total - failedCount - skippedCount
        if failedCount == 0:
            if skippedCount == total:
                self.writeProgress("complete", total, total, "All files were already imported")
            else:
                self.writeProgress("complete", total, total, f"Imported {succeededCount}/{total} files ({skippedCount} skipped)")
        elif succeededCount == 0 and skippedCount == 0:
            self.writeProgress("failed", total, total, f"Imported 0/{total} files (all failed)", error=True)
        else:
            self.writeProgress("complete", total, total,
                                f"Imported {succeededCount}/{total} files ({skippedCount} skipped, {failedCount} failed)", error=True)
        return outcomes

    def _importHistoryBatchOverwriteLocked(self, fileContents: list[str]) -> list[str]:
        """Atomic overwrite: _deleteCoveredRange's delete and every file's
        import run in ONE transaction (each file's own commit deferred - see
        _importHistoryLocked's deferCommit), committed once at the very end.
        Any failure - an unrecognized file (caught before anything is
        staged), an error inside the delete pass itself, or any single file's
        import raising - rolls back everything staged so far and aborts the
        rest of the batch, leaving the database exactly as it was before the
        upload. Only a batch where every file succeeds is committed."""
        if not fileContents:
            return []

        total = len(fileContents)

        try:
            # Last safe point to report progress: nothing has been staged on
            # this connection yet. From here until the final commit() below,
            # writeProgress must not run - it self-commits (INVARIANT in
            # _importHistoryLocked) and would flush the delete and any
            # already-processed files' staged writes early.
            self.writeProgress("running", 0, total, f"Overwrite: deleting covered range for {total} file(s)")

            deleted = self._deleteCoveredRange(fileContents)
            if deleted is None:
                # A file didn't parse - _deleteCoveredRange never wrote
                # anything, so there is nothing to roll back.
                self.writeProgress("failed", 0, total,
                                   "Overwrite import aborted: unrecognized or corrupt export file - nothing was deleted",
                                   error=True)
                return ["failed"] * total
            deletedPlays, deletedSkips, skippedYears, coveredYears = deleted
            message = f"Overwrite: staged deletion of {deletedPlays} plays and {deletedSkips} skip events in the covered range"
            if skippedYears:
                yearsText = ", ".join(str(year) for year in skippedYears)
                message += f" ({yearsText} not covered by uploaded files - left untouched)"
            _dbmod.logger.info("%s for user %s", message, self.user)

            runState = _dbmod._ImportRunState()
            for index, content in enumerate(fileContents, start=1):
                isFinalFile = (index == total)
                self.importHistory(
                    content,
                    progressPrefix=f"File {index}/{total}: ",
                    isFinalFile=isFinalFile,
                    track_file_hash=True,
                    runState=runState,
                    deferCommit=True,
                )

            self.repo.commit()
            for year in sorted(coveredYears | runState.correctedYears):
                self.repo.deleteUserWrapped(self.user, year)
            for track in runState.pendingImageTracks.values():
                self.saveImagesFromTrack(track)
        except Exception as e:
            # _importHistoryLocked's except already rolled back the whole
            # transaction (the delete plus every prior file staged in this
            # batch) when the failure came from a file import; call it again
            # defensively (a no-op if there's nothing pending) in case the
            # failure came from _deleteCoveredRange itself.
            self.repo.rollback()
            _dbmod.logger.error("Overwrite import aborted after a failure - no changes were applied, "
                        "original data is intact: %s", _dbmod.parseError(e))
            self.writeProgress("failed", 0, total,
                               f"Overwrite import aborted: no changes were applied, original data is intact - {_dbmod.parseError(e)}",
                               error=True)
            return ["failed"] * total

        self.writeProgress("complete", total, total, f"Overwrite import complete: {total}/{total} files imported")
        return ["imported"] * total

    def _deleteCoveredRange(self, fileContents: list[str]) -> tuple[int, int, list[int], set[int]] | None:
        """The overwrite pre-pass: parse every file, take the batch span
        [earliest entry, latest entry] and the union of covered years (a year
        counts as covered only if some entry STARTS in it - see
        Importer.coverage), then delete this user's plays and skips in each
        covered year's segment of the span. Years inside the span no file
        covers (missing files) are skipped and reported.

        Does NOT commit and does NOT touch the Wrapped cache - the caller
        (the atomic overwrite batch) shares one transaction across this
        delete and every file's import, and only commits once the whole
        batch succeeds; deleteUserWrapped self-commits (INVARIANT, see
        _importHistoryLocked) so it must run after that commit, not here.

        Returns (deletedPlays, deletedSkips, skippedYears, coveredYears), or
        None when any file is unrecognized - the caller must abort WITHOUT
        deleting."""
        importer = self._withCookiesFile(lambda cookiesFile: _dbmod.Importer(cookiesFile=cookiesFile, email=self.email))

        minStart = None
        maxEnd = None
        coveredYears: set[int] = set()
        for content in fileContents:
            parsedHistory, exportType = importer._convertToList(content)
            if exportType == "None":
                return None
            fileCoverage = importer.coverage(parsedHistory, exportType)
            if fileCoverage is None:
                continue  #< a valid-but-empty export covers nothing
            fileMin, fileMax, fileYears = fileCoverage
            minStart = fileMin if minStart is None else min(minStart, fileMin)
            maxEnd = fileMax if maxEnd is None else max(maxEnd, fileMax)
            coveredYears |= fileYears

        if minStart is None:
            return 0, 0, []

        # Same timezone as Importer.coverage's year bucketing, so segments
        # line up exactly with the covered-years set.
        tz = _dbmod.getTimezone()

        def yearStartTs(year: int) -> float:
            return _dbmod.datetime.datetime(year, 1, 1, tzinfo=tz).timestamp()

        deletedPlays = 0
        deletedSkips = 0
        skippedYears: list[int] = []
        firstYear = _dbmod.convertToDatetime(minStart, tz).year
        lastYear = _dbmod.convertToDatetime(maxEnd, tz).year
        for year in range(firstYear, lastYear + 1):
            if year not in coveredYears:
                skippedYears.append(year)
                continue
            segmentStart = max(yearStartTs(year), minStart)
            segmentEnd = min(yearStartTs(year + 1) - self.YEAR_SEGMENT_BOUNDARY_EPSILON_SECONDS, maxEnd)
            deletedPlays += self.repo.deletePlaysInRange(self.user, segmentStart, segmentEnd)
            deletedSkips += self.repo.deleteSkipsInRange(self.user, segmentStart, segmentEnd)

        return deletedPlays, deletedSkips, skippedYears, coveredYears
