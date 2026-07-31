# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import TYPE_CHECKING

# _ImportRunState is used in the hints below and constructed at runtime via
# _dbmod. Unlike this package's other annotation-only names, it cannot become a
# real import: it lives in Database.database, which imports the mixin defined
# here, so a top-level import would cycle. That means static tooling resolves
# the hint but typing.get_type_hints() still cannot - the accepted cost of
# TYPE_CHECKING, and the reason the leaf names elsewhere are imported normally.
if TYPE_CHECKING:   # pragma: no cover
    from Database.database import _ImportRunState

import sqlite3

import Database.database as _dbmod  # noqa: F401 - module-global names
# (LastfmClient, requests, Importer, logger, time, Path, ...) are reached through
# the database module so the suite's patch("Database.database.X") targets keep
# working after this relocation.

# Drop counters (see StreamingHistoryImporter._processPlay) whose plays WOULD
# import on a later attempt: the lookup failed, the data didn't. An overwrite
# import must not delete a range it can't fully rebuild, so any of these aborts
# the batch. "droppedNoTrack" is deliberately absent - podcast/audiobook rows
# carry no track name and can never resolve, so treating them as retryable
# would make overwrite import impossible for most real exports.
RETRYABLE_DROP_STAT_KEYS = ("droppedTransient", "droppedUnexpected")

# Entries that could not be READ (see StreamingHistoryImporter._parseHistory).
# They abort an overwrite for the same reason as the keys above - the covered
# range is computed from the same parse, so the OTHER entries still mark the
# year covered and the delete would take out a play nothing re-inserts - but the
# advice differs: re-running changes nothing, the file itself is unreadable.
# "droppedNegativeTime" is deliberately absent, matching "droppedNoTrack": it is
# a long-standing sanity filter, and aborting on it would block exports that
# have always been importable.
UNREADABLE_DROP_STAT_KEYS = ("droppedMalformed",)

# How far apart two skip rows for the same track may be and still be treated as
# one physical event. Sized for the recording sources disagreeing about what
# played_at means (start vs end of a sub-5s play) plus clock drift - NOT for the
# real-play path's much wider "duration + 60s" window, which would swallow a
# genuine second skip of the same track later in a session.
SKIP_NEAR_TIME_TOLERANCE_SECONDS = 10


class ImportMixin:
    """History import / reconciliation (append*, importHistory*, overwrite range), mixed into Database."""

    # ---- writing plays ---------------------------------------------------------------

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
                      runState: _ImportRunState | None = None):
        """Import one export file. Serialized per user via _importLock (see
        its comment in __init__); the actual work is in _importHistoryLocked."""
        with self._importLock:
            return self._importHistoryLocked(exportedHistory, progressPrefix, isFinalFile, hasPriorError,
                                             track_file_hash, runState)

    def _importHistoryLocked(self, exportedHistory, progressPrefix: str = "", isFinalFile: bool = True, hasPriorError: bool = False, track_file_hash: bool = False,
                             runState: _ImportRunState | None = None):
        """Import one export file: Phase 1 stages it (parse + Spotify metadata
        fetch) holding NO write transaction, then Phase 2 applies the staged
        rows. Splitting them keeps the network-bound fetch out of the write
        transaction - the atomic overwrite batch depends on this to avoid
        holding SQLite's single write lock across Spotify lookups.

        Always deferCommit=False on both phase calls: this path owns one
        transaction per file, so writeProgress's self-commits are safe here.
        The atomic overwrite batch never comes through this method - it
        drives _stageImportData/_applyImportData directly with
        deferCommit=True and its own no-op progress reporter, because a
        self-commit would flush a prior file's staged writes mid-batch (see
        _importHistoryBatchOverwriteLocked). A deferCommit parameter used to
        exist here for that batch and was never called with it; had it been,
        the staging-failure rollback below would have discarded a prior
        file's staged writes in a transaction this method does not own."""
        importer = self._withCookiesFile(lambda cookiesFile: _dbmod.Importer(cookiesFile=cookiesFile, email=self.email))
        if runState is None:
            runState = _dbmod._ImportRunState()

        # Snapshot for the staging-failure path; the apply path restores the
        # same run-state fields itself (see _applyImportData's except).
        claimedRowIdsBefore = set(runState.claimedRowIds)
        insertedPlayKeysBefore = set(runState.insertedPlayKeys)
        try:
            staged = self._stageImportData(importer, exportedHistory, progressPrefix,
                                           hasPriorError, self.writeProgress, runState, deferCommit=False)
        except Exception as e:
            # Staging (parse / Spotify metadata fetch) failed before any DB
            # write. Report it and restore the batch-shared run state, matching
            # the apply path's failure handling.
            self.repo.rollback()
            runState.claimedRowIds = claimedRowIdsBefore
            runState.insertedPlayKeys = insertedPlayKeysBefore
            self.writeProgress("failed", 0, 0, f"{progressPrefix}Import failed: {_dbmod.parseError(e)}", error=True)
            raise
        if staged is None:
            return
        stagedTracks, stagedPlays, total, importStats = staged
        self._applyImportData(stagedTracks, stagedPlays, importStats, total, exportedHistory,
                              progressPrefix, isFinalFile, hasPriorError, track_file_hash,
                              runState, False, self.writeProgress)

    def _stageImportData(self, importer, exportedHistory, progressPrefix, hasPriorError,
                         reportProgress, runState, deferCommit, knownTracks=None):
        """Phase 1 of an import: parse the export and fetch any missing track
        metadata from Spotify. Does NO database writes and holds NO write
        transaction, so this network-bound work cannot block other writers.
        Returns (stagedTracks, stagedPlays, total, importStats), or None when
        the file parsed to nothing; raises ValueError on a corrupt file.

        knownTracks seeds the importer's "don't re-fetch what we already have"
        cache; the overwrite batch passes a shared, growing list so a track one
        file fetched isn't fetched again for a later file (the per-file
        transaction that used to make that happen implicitly is gone)."""
        parsedHistory, exportType = importer._convertToList(exportedHistory)
        if exportType == "None":
            # Unrecognized content (corrupt JSON, a file read mid-copy, the
            # wrong file entirely) must fail loudly: returning silently here
            # used to make AutoImporter move never-imported files to DONE/ as
            # successes and the web UI report the import as complete. The caller
            # (orchestrator / overwrite batch) reports the failure progress.
            raise ValueError("Unrecognized or corrupt export file - expected a Spotify JSON export or Musicolet CSV backup")
        if not parsedHistory:
            return None

        total = len(parsedHistory)
        reportProgress("running", 0, total, f"{progressPrefix}Starting import", error=hasPriorError)

        def progressCallback(status, current, totalSteps, message):
            reportProgress(status, current, totalSteps, f"{progressPrefix}{message}", error=hasPriorError)

        # Staged in memory; written to the DB only in Phase 2 (_applyImportData)
        # once the whole file's metadata has been fetched.
        stagedTracks: dict[str, dict] = {}
        stagedPlays: list[dict] = []
        importStats: dict = {}
        if knownTracks is None:
            knownTracks = self.repo.getAllTracks()
        for index, meta in enumerate(
            importer.importHistory(parsedHistory, knownTracks, exportType, progressCallback=progressCallback,
                                   stats=importStats),
            start=1,
        ):
            entry, track = self._splitEntryAndTrack(meta)
            stagedTracks[track["id"]] = track
            stagedPlays.append(entry)
            if deferCommit:
                # saveImagesFromTrack -> tryClaimImageDownload self-commits (same
                # INVARIANT as reportProgress) and a deferCommit batch may already
                # have rows staged - claim images only after the final commit,
                # see _importHistoryBatchOverwriteLocked.
                runState.pendingImageTracks[track["id"]] = track
            else:
                # Safe here: Phase 2 hasn't run, so nothing is staged on the
                # connection for this self-committing call to flush.
                self.saveImagesFromTrack(track)

            if index % self.PROGRESS_UPDATE_INTERVAL == 0 or index == total:
                reportProgress("running", index, total, f"{progressPrefix}Imported {index} of {total}")

        # A file nothing could be read from fails as loudly as an unrecognized
        # one, and for the same reason. _convertToList types a file from its
        # FIRST row alone, so a JSON list whose rows carry `ts` but not the
        # master_metadata_* keys is typed as an extended export and then fails
        # on every row - which used to import as a silent success ("0 tracks
        # imported", progress complete, AutoImporter filing it under DONE/).
        #
        # Only when EVERY entry was unreadable: a file that is partly readable
        # imports what it can, and the droppedMalformed counter is what stops an
        # overwrite from deleting the rest (see UNREADABLE_DROP_STAT_KEYS).
        # Podcast-only files are unaffected - episode rows parse, and are
        # dropped a layer lower as droppedNoTrack.
        entriesSeen = importStats.get("entriesSeen", 0)
        if entriesSeen and importStats.get("droppedMalformed", 0) == entriesSeen:
            raise ValueError(
                f"None of the {entriesSeen} entries in this file could be read - it looks like a "
                "Spotify export but carries none of the expected fields. Re-download the export "
                "from Spotify (check you uploaded the streaming-history file, not another one)."
            )

        return stagedTracks, stagedPlays, total, importStats

    def _applyImportData(self, stagedTracks, stagedPlays, importStats, total, exportedHistory,
                         progressPrefix, isFinalFile, hasPriorError, track_file_hash, runState,
                         deferCommit, reportProgress):
        """Phase 2 of an import: write the staged tracks/plays to the database.
        Pure DB work (no network), so the write transaction it opens - and, in a
        deferCommit overwrite batch, the covered-range delete sharing that
        transaction - is held only as long as the local writes take. The caller
        must invoke this only after Phase 1 staging has completed."""
        index = 0   #< advanced per applied play below, so the failure path can say how far it got
        # Rolled-back writes must not stay claimed in a batch-shared run state
        claimedRowIdsBefore = set(runState.claimedRowIds)
        insertedPlayKeysBefore = set(runState.insertedPlayKeys)
        try:
            for track in stagedTracks.values():
                self.repo.upsertTrack(track, created_reason=f"history_import (user: {self.user})")

            insertedCount = 0
            updatedCount = 0
            enrichedCount = 0
            skipsSavedCount = 0
            correctedYears = set()
            # Fetch both classifier settings once for the whole batch so each
            # row's is_skip is computed without a per-row settings read.
            skipThreshold = self.repo.getSkipThreshold()
            completionPercent = self.repo.getCompletionCompletePercent()
            for index, entry in enumerate(stagedPlays, start=1):
                track_id = entry["id"]
                played_at = entry["playedAt"]
                time_played = entry["timePlayed"]
                played_from = entry.get("playedFrom")
                extras = entry.get("importExtras") or {}
                extrasValues = [extras.get(column) for column in _dbmod.BEHAVIORAL_COLUMNS]

                track = stagedTracks.get(track_id)
                #< staged tracks carry Client.formatTrack's "duration" key (ms)
                trackDurationMs = track.get("duration") if track else None
                isSkip = self.repo.computeIsSkip(
                    time_played, trackDurationMs,
                    threshold=skipThreshold, completionPercent=completionPercent)

                # Sub-5s events (entry["isSkip"], the fixed import floor) never
                # claim or correct a real play row - they match only against
                # other skips. plays' UNIQUE constraint alone wasn't enough: the
                # live listener records the same physical event, and the two
                # sources' played_at can differ by seconds (Spotify's
                # start-vs-end ambiguity), so one skip landed twice and inflated
                # skip counts.
                #
                # The classifier has to agree, not just the floor. Since 73e1a2c
                # computeIsSkip caps its threshold at the completion boundary, so
                # a play under 5s is a COMPLETE play whenever duration x
                # completion% < 5000ms - any track shorter than 6.25s at the
                # default 80%. A 3s interlude played to its end used to be stored
                # is_skip=1 here, which both disagreed with the classifier (the
                # next recomputeSkipFlags flipped it) and hid the listener's
                # correctly-classified is_skip=0 row for the same event from the
                # dedup, so that recompute turned one event into two real plays.
                # Those fall through to the real-play path below instead, which
                # is the matcher that can see the row they need to dedup against.
                #
                # The converse is deliberately NOT routed here: an event ABOVE
                # the floor that the classifier calls a skip (a high admin
                # threshold) still goes to the real-play path, because it may
                # legitimately be a correction of an existing longer play, and
                # this path cannot correct.
                if entry.get("isSkip") and isSkip:
                    nearbySkips = [
                        skip for skip in self.repo.getSkipsNearTime(
                            self.user, track_id, played_at, SKIP_NEAR_TIME_TOLERANCE_SECONDS)
                        # Rows this run wrote belong to other entries of the same
                        # export - two genuinely distinct skips must not collapse
                        # into one (same rule as the real-play path below).
                        if not runState.isOwnWrite(track_id, skip)
                    ]
                    if nearbySkips:
                        # Claim it, exactly as the real-play path below does. An
                        # unclaimed match stayed a candidate for every LATER
                        # entry too, so a second genuine skip inside the same
                        # 10s window matched the same row and was dropped as a
                        # duplicate - silently, and counted by nothing. Nearest
                        # first, so which entry pairs with which row does not
                        # depend on the order the query returned them in.
                        closest = min(nearbySkips, key=lambda skip: abs(skip["played_at"] - played_at))
                        runState.claimedRowIds.add(closest["id"])
                        continue
                    if self.repo.insertPlay(self.user, track_id, played_at, time_played,
                                            created_reason=f"history_import (user: {self.user})",
                                            extras=entry.get("importExtras"), is_skip=1):
                        skipsSavedCount += 1
                        runState.insertedPlayKeys.add((track_id, played_at))
                    continue

                # Check if a play for this track already exists within (duration + 60s) tolerance,
                # same logic as API backfill to handle potential overlap with backfilled data
                # where Spotify's played_at can be ambiguous (start or end time).
                durationSeconds = (trackDurationMs or 0) // 1000
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
                            corrected_is_skip = isSkip
                            try:
                                self.repo.correctPlay(existing_play["id"], played_at, time_played,
                                                       corrected_is_skip, extrasValues)
                            except sqlite3.IntegrityError:
                                # Correcting played_at would collide with an existing
                                # (username, track_id, played_at) row the near-time
                                # matcher can't see - it filters is_skip=0, so a merged
                                # skip sitting at exactly this timestamp is invisible.
                                # Leave the row uncorrected rather than fail the whole
                                # file/batch on the UNIQUE violation.
                                _dbmod.logger.info(
                                    "Skipping played_at correction for track %s: target timestamp already recorded",
                                    track_id,
                                )
                                continue
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
                            self.repo.enrichPlayBehavioralColumns(existing_play["id"], extrasValues)
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

                # If no matches, proceed to insert as usual, with the is_skip
                # computed above from the batch threshold + this track's duration.
                if self.repo.insertPlay(self.user, track_id, played_at, time_played, played_from,
                                        created_reason=f"history_import (user: {self.user})",
                                        extras=entry.get("importExtras"), is_skip=isSkip):
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
            self.raiseMilestoneRecalcFlag()
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
        """Atomic overwrite in two phases. Phase 1 stages every file (parse +
        Spotify metadata fetch) into memory holding NO write transaction; Phase 2
        then runs the covered-range delete and every file's apply in ONE short
        transaction with no network in between, committed once at the end. That
        keeps SQLite's single write lock from being held across Spotify lookups -
        the old flow held it for the whole run, timing other writers (the live
        listener) out and losing their plays. Any failure rolls back everything
        and aborts, leaving the original data untouched; only an all-success
        batch commits."""
        if not fileContents:
            return []

        total = len(fileContents)

        # Phase 0 - covered range (parse only, no DB, no lock). Detects an
        # unrecognized/corrupt file before anything is deleted.
        coverage = self._computeCoveredRange(fileContents)
        if coverage is None:
            self.writeProgress("failed", 0, total,
                               "Overwrite import aborted: unrecognized or corrupt export file - nothing was deleted",
                               error=True)
            return ["failed"] * total
        minStart, maxEnd, coveredYears = coverage

        def noProgress(*args, **kwargs):
            return None

        # Phase 1 - stage every file (parse + Spotify metadata fetch) into
        # memory. Holds NO write transaction, so these network-bound lookups
        # can't block other writers. writeProgress here is safe: nothing is
        # staged on the connection during staging, only in-memory structures.
        runState = _dbmod._ImportRunState()
        try:
            importer = self._withCookiesFile(lambda cookiesFile: _dbmod.Importer(cookiesFile=cookiesFile, email=self.email))
            knownTracks = self.repo.getAllTracks()   #< shared, grown per file below
            stagedFiles = []
            for index, content in enumerate(fileContents, start=1):
                progressPrefix = f"File {index}/{total}: "
                self.writeProgress("running", index - 1, total, f"{progressPrefix}Fetching metadata")
                staged = self._stageImportData(importer, content, progressPrefix,
                                               False, noProgress, runState, deferCommit=True,
                                               knownTracks=knownTracks)
                if staged is not None:
                    # Feed this file's fetched tracks forward so a later file
                    # that replays the same track doesn't re-fetch it.
                    knownTracks.extend(staged[0].values())
                stagedFiles.append((staged, content, progressPrefix, index == total))
        except Exception as e:
            self.writeProgress("failed", 0, total,
                               f"Overwrite import aborted: no changes were applied, original data is intact - {_dbmod.parseError(e)}",
                               error=True)
            return ["failed"] * total

        # Phase 1b - the delete range covers every play the files PARSED, but
        # only the plays that survived staging get re-inserted. A play dropped
        # for a retryable reason (rate limit, timeout, unexpected error) would
        # therefore be deleted and never replaced, inside the same transaction
        # that makes the rest atomic - permanent, silent loss of rows the
        # listener or an earlier import had already recorded. Abort while
        # nothing has been deleted yet and let the user re-run.
        droppedStats = self._sumImportStats(stagedFiles)
        retryableDropped = sum(droppedStats.get(key, 0) for key in RETRYABLE_DROP_STAT_KEYS)
        if retryableDropped:
            self.writeProgress("failed", 0, total,
                               f"Overwrite import aborted: {retryableDropped} play(s) could not be looked up "
                               "(Spotify rate limit or outage) - nothing was deleted, your data is unchanged. "
                               "Please try the import again.",
                               error=True)
            return ["failed"] * total

        unreadableDropped = sum(droppedStats.get(key, 0) for key in UNREADABLE_DROP_STAT_KEYS)
        if unreadableDropped:
            self.writeProgress("failed", 0, total,
                               f"Overwrite import aborted: {unreadableDropped} entr(y/ies) in the uploaded file(s) "
                               "could not be read, so the plays they describe cannot be restored - nothing was "
                               "deleted, your data is unchanged. Re-download the export from Spotify, or import "
                               "without the overwrite option to add what is readable.",
                               error=True)
            return ["failed"] * total

        # Phase 2 - ONE short transaction: delete the covered range, then apply
        # every file's staged rows. No network here, so the write lock is held
        # only for the local DB work. writeProgress must NOT run between the
        # delete and the final commit (it self-commits - INVARIANT).
        try:
            self.writeProgress("running", 0, total, f"Overwrite: applying {total} file(s)")
            deletedPlays, deletedSkips, skippedYears = self._deletePlaysInCoveredRange(minStart, maxEnd, coveredYears)
            message = f"Overwrite: staged deletion of {deletedPlays} plays and {deletedSkips} skip events in the covered range"
            if skippedYears:
                yearsText = ", ".join(str(year) for year in skippedYears)
                message += f" ({yearsText} not covered by uploaded files - left untouched)"
            _dbmod.logger.info("%s for user %s", message, self.user)

            for staged, content, progressPrefix, isFinalFile in stagedFiles:
                if staged is None:
                    continue  #< a valid-but-empty file staged nothing
                stagedTracks, stagedPlays, fileTotal, importStats = staged
                self._applyImportData(stagedTracks, stagedPlays, importStats, fileTotal, content,
                                      progressPrefix, isFinalFile, False, True,
                                      runState, True, noProgress)

            self.repo.commit()
        except Exception as e:
            # _applyImportData's except already rolled back the whole
            # transaction (the delete plus every prior file's staged writes)
            # when the failure came from an apply; call it again defensively
            # (a no-op if nothing is pending) in case it came from the delete.
            self.repo.rollback()
            _dbmod.logger.error("Overwrite import aborted after a failure - no changes were applied, "
                        "original data is intact: %s", _dbmod.parseError(e))
            self.writeProgress("failed", 0, total,
                               f"Overwrite import aborted: no changes were applied, original data is intact - {_dbmod.parseError(e)}",
                               error=True)
            return ["failed"] * total

        # Post-commit cleanup, OUTSIDE the try above: the commit is the point
        # of no return, and these are repairable side effects, not part of the
        # atomic apply. When they shared the try, a failure here fell into the
        # rollback handler - which told the user "no changes were applied,
        # original data is intact" about an overwrite that had durably landed,
        # and returned all-failed, so AutoImporter moved the successfully
        # imported files to FAILED/ and importHistoryBatch never raised the
        # milestone recalc flag. Each loop is guarded on its own so a Wrapped
        # hiccup still lets the cover art queue.
        for year in sorted(coveredYears | runState.correctedYears):
            try:
                self.repo.deleteUserWrapped(self.user, year)
            except Exception as e:
                #< stale-but-repairable: the wrapped worker's staleness check
                #  compares the cached (max_played_at, play_count) snapshot to
                #  the plays this batch just rewrote, so the year rebuilds on
                #  its next cycle even with the stale row left behind
                _dbmod.logger.warning("Overwrite import committed, but clearing the cached Wrapped "
                                      "for %d failed (it may show stale data until recalculated): %s",
                                      year, _dbmod.parseError(e))
        for track in runState.pendingImageTracks.values():
            try:
                self.saveImagesFromTrack(track)
            except Exception as e:
                #< missing art self-heals too: the detail/list pages lazy-fetch
                _dbmod.logger.warning("Overwrite import committed, but queueing cover art for track %s "
                                      "failed: %s", track.get("id"), _dbmod.parseError(e))

        # Per-file progress is routed to noProgress in overwrite mode, so this
        # line is the only place a permanent drop can surface at all.
        summary = f"Overwrite import complete: {total}/{total} files imported"
        permanentDropped = droppedStats.get("droppedNoTrack", 0)
        if permanentDropped:
            summary += f" ({permanentDropped} entries dropped: no track info, e.g. podcasts)"
        self.writeProgress("complete", total, total, summary)
        return ["imported"] * total

    @staticmethod
    def _sumImportStats(stagedFiles: list) -> dict:
        """Batch-wide totals of the per-file drop counters staging produced.
        stagedFiles entries are (staged, content, progressPrefix, isFinalFile),
        where `staged` is None for a valid-but-empty file and otherwise carries
        its importStats dict as its last element."""
        totals: dict = {}
        for staged, *_ in stagedFiles:
            if staged is None:
                continue
            for key, count in staged[3].items():
                totals[key] = totals.get(key, 0) + count
        return totals

    def _computeCoveredRange(self, fileContents: list[str]) -> tuple | None:
        """Parse every file (no DB writes, no lock) and return
        (minStart, maxEnd, coveredYears) for the overwrite delete: the batch
        span [earliest entry, latest entry] and the union of covered years (a
        year counts as covered only if some entry STARTS in it - see
        Importer.coverage). Returns (None, None, set()) when the files cover
        nothing (all valid-but-empty), or None when any file is unrecognized/
        corrupt - the caller must abort WITHOUT deleting."""
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

        return minStart, maxEnd, coveredYears

    def _deletePlaysInCoveredRange(self, minStart, maxEnd, coveredYears) -> tuple[int, int, list[int]]:
        """Delete this user's plays and skips in each covered year's segment of
        [minStart, maxEnd]. Years inside the span that no file covers (missing
        files) are left untouched and returned as skippedYears. Does NOT commit
        and does NOT touch the Wrapped cache - runs inside the overwrite batch's
        single transaction, so the caller commits and invalidates Wrapped once
        the whole batch succeeds. Returns (deletedPlays, deletedSkips,
        skippedYears); an empty covered range deletes nothing."""
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

        return deletedPlays, deletedSkips, skippedYears
