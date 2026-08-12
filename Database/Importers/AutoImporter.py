# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import random
import threading
import shutil
import logging

logger = logging.getLogger(__name__)

# Random startup-offset bounds for the folder watchdog: with several users,
# every per-user scan (and any initial-file import) used to hit the disk at
# the same instant after a restart. Kept short - the poll cadence itself is
# only a few seconds.
AUTO_IMPORT_MIN_START_DELAY_SECONDS = 5
AUTO_IMPORT_MAX_START_DELAY_SECONDS = 30

WATCHDOG_STOP_JOIN_TIMEOUT_SECONDS = 2  #< bound how long stop() waits for the poll thread to exit

# Poll threads are named "<prefix><watched folder's name>" (i.e. the username,
# since each user watches autoImport/<user>/) - an anonymous "Thread-17" in a
# thread dump says nothing about whose watcher is still running, and the test
# suite's leaked-thread guard identifies these by name.
WATCHDOG_THREAD_NAME_PREFIX = "auto-import-watchdog-"

try:
    from Database.utils import parseError
except ModuleNotFoundError:
    # Standalone execution from inside Database/: put the parent on the path
    # so `utils` resolves the same way the package import above would.
    import sys
    parentDir = "../"
    if parentDir not in sys.path:
        sys.path.append(parentDir)
    from utils import parseError

class Watchdog:  #< polls a folder and hands over files once their size stops changing
    def __init__(self):
        self.run = True   #< signalStop()/stop() clear it; the poll loop checks it every pass
        self._stop_event = threading.Event()

    @staticmethod
    def _forgetRetryable(retryPaths, knownFiles):
        """Drop the callback's un-taken paths from knownFiles so a later poll
        sights them again.

        knownFiles.add(name) happens BEFORE the callback deliberately - it is
        what stops an unreadable file being re-delivered every checkInterval
        for the life of the process. The cost was that a file the callback
        could not read was never re-delivered either, so the "reads fine on a
        later pass" recovery _handleImport documents had no later pass. Only
        what the callback explicitly hands back is forgotten, and the bound on
        how often it may do so lives with the callback (see
        MAX_TRANSIENT_READ_ATTEMPTS), not here."""
        if not isinstance(retryPaths, (list, tuple, set, frozenset)):
            return   #< the documented "anything else means accepted". A truthy
                     #  non-iterable (a callback returning True) would otherwise
                     #  raise TypeError, and on the initial-scan call that is
                     #  outside the poll loop's guard - it would kill the watcher
                     #  thread, which nothing restarts.
        for path in retryPaths:
            knownFiles.discard(os.path.basename(path))

    @staticmethod
    def _fileSizeOrNone(path):
        """Size in bytes, or None if it can't be read right now (mid-move,
        locked by the copying process, already deleted) - the caller re-checks
        on the next poll instead of treating that as fatal."""
        try:
            return os.path.getsize(path)
        except OSError:
            return None

    def watchFolder_blocking(self, pathToWatch, callback, checkInterval=5, callbackInitialFiles=True,
                             startupDelaySeconds=0):
        """`callback` receives the LIST of files discovered in one scan (sorted
        by path), not one call per file: files dropped together must be
        processed as a single batch so batch-scoped import state (duplicate-
        claim tracking across file boundaries, see Database.importHistoryBatch)
        covers all of them.

        It may RETURN the subset of paths it could not take, which are then
        forgotten and delivered again on a later poll. A callback that returns
        None (or anything else) is taken to have accepted the whole batch -
        that is the original contract, and the once-per-process rule it
        implies. See _forgetRetryable.

        A newly appearing file is only delivered once its size is unchanged
        between two consecutive polls: a large export still being copied into
        the folder used to be read mid-copy, fail to parse, and be swallowed.
        Files already present at startup are delivered immediately - they've
        been sitting there since before this process started.

        `startupDelaySeconds` postpones the whole watcher (including the
        initial scan) - AutoImporter passes a random offset so per-user
        watchers don't all hit the disk at the same instant after a restart."""
        if startupDelaySeconds and self._stop_event.wait(startupDelaySeconds):
            return
        logger.info(f"Monitoring {pathToWatch} for new files (Polling)...")
        if not os.path.exists(pathToWatch):   #< first run for this user
            os.makedirs(pathToWatch)   #< create the drop folder before the initial scan
        try:
            knownFiles = {f for f in os.listdir(pathToWatch) if os.path.isfile(os.path.join(pathToWatch, f))}
            if callbackInitialFiles and knownFiles:
                fullPaths = sorted(os.path.join(pathToWatch, f) for f in knownFiles)
                for fullPath in fullPaths:
                    logger.info(f"File found: {fullPath}")
                self._forgetRetryable(callback(fullPaths), knownFiles)
        except FileNotFoundError:
            logger.error(f"Error: The directory {pathToWatch} does not exist.")
            return   #< nothing to watch; startAutoImporter would have to recreate it anyway
        pendingSizes = {}   #< name -> size at last poll, for files waiting to stabilize
        while self.run and not self._stop_event.is_set():
            self._stop_event.wait(checkInterval)
            if not self.run or self._stop_event.is_set():
                break
            try:
                currentFiles = {f for f in os.listdir(pathToWatch) if os.path.isfile(os.path.join(pathToWatch, f))}
                knownFiles &= currentFiles   #< forget deleted files so a later re-drop counts as new

                newlySighted = set()
                for name in currentFiles - knownFiles - pendingSizes.keys():
                    size = self._fileSizeOrNone(os.path.join(pathToWatch, name))
                    if size is not None:
                        pendingSizes[name] = size
                        newlySighted.add(name)

                readyPaths = []
                for name in list(pendingSizes):
                    if name in newlySighted:
                        continue   #< first sighted this poll - a same-poll re-read would compare two
                                   #  reads microseconds apart and call a mid-copy file "stable"
                    if name not in currentFiles:
                        del pendingSizes[name]   #< vanished before stabilizing (user pulled it back out)
                        continue
                    size = self._fileSizeOrNone(os.path.join(pathToWatch, name))
                    if size is None:
                        continue   #< transiently unreadable - re-check next poll
                    if size == pendingSizes[name]:
                        del pendingSizes[name]
                        knownFiles.add(name)
                        readyPaths.append(os.path.join(pathToWatch, name))
                    else:
                        pendingSizes[name] = size   #< still growing - wait another poll

                if readyPaths:
                    readyPaths.sort()
                    for fullPath in readyPaths:
                        logger.info(f"New file created: {fullPath}")
                    self._forgetRetryable(callback(readyPaths), knownFiles)
            except Exception as e:
                # Per-iteration, deliberately: this used to wrap the whole
                # loop, so one transient failure (an os.listdir denied while
                # an antivirus scanner or backup tool held the folder, a
                # network path blipping) killed the watcher for the rest of
                # the process's life. Nothing restarts it - startAutoImporter
                # runs once per activation, and the periodic login pass only
                # restarts listeners - so drop-folder imports stayed silently
                # dead until the app was restarted. Log and poll again.
                logger.error(f"Watchdog poll failed, continuing to watch: {parseError(e)}")
        logger.info("Watchdog stopped peacefully")

    def watchFolder(self, pathToWatch, callback, checkInterval=5, startupDelaySeconds=0):
        self._stop_event.clear()
        self.thread = threading.Thread(
            target=self.watchFolder_blocking,
            args=(pathToWatch, callback, checkInterval),
            kwargs={"startupDelaySeconds": startupDelaySeconds},
            name=f"{WATCHDOG_THREAD_NAME_PREFIX}{os.path.basename(str(pathToWatch))}",
            daemon=True
        )
        self.thread.start()

    def signalStop(self):
        """Signal-only half of stop() - no join, safe for shutdown's
        signal-everything-first phase."""
        self._stop_event.set()
        self.run = False   #< the poll loop re-checks this after every wait

    def stop(self):
        self.signalStop()
        if hasattr(self, "thread") and self.thread.is_alive():
            self.thread.join(timeout=WATCHDOG_STOP_JOIN_TIMEOUT_SECONDS)

# How many times one file may come back unreadable before it is quarantined.
# The retry exists for a lock that clears (an antivirus scanner, a backup tool,
# a copy still finishing); a lock that outlives three polls is not clearing, and
# retrying forever is a log line every pollInterval for the life of the process.
# FAILED/ then makes it visible to someone who never reads the log.
MAX_TRANSIENT_READ_ATTEMPTS = 3


class AutoImporter:  #< drop-folder importer: Watchdog feeds _handleImport; files end in DONE/ or FAILED/
    def __init__(self, folderPath, importCallback, pollInterval=5, keyword=None):
        self.folderPath = folderPath   #< the user's autoImport/<user>/ drop folder
        self.importCallback = importCallback   #< receives one poll's whole batch of stable files
        self.pollInterval = pollInterval
        self.keyword = keyword   #< None = import everything; else the filename must contain it
        self.wd = Watchdog()
        # path -> (consecutive unreadable deliveries, the file's stamp when we
        # last counted one). Only the watchdog thread touches it, and an entry
        # is cleared on the first successful read or quarantine. The stamp is
        # what stops a NEW file inheriting an old one's budget: nothing here
        # can see that the user pulled a half-copied export back out and
        # re-copied it, and this deployment drops the same filename every week,
        # so a path alone is not an identity.
        self._readAttempts = {}
        # Paths already announced as being quarantined. A quarantine whose MOVE
        # fails is retried on every later delivery - unbounded on purpose, so
        # the file recovers the moment the lock or the read-only mount clears -
        # and this is what keeps that from being an unbounded log line too.
        self._quarantineAnnounced = set()

    def _quarantine(self, path, retryable, reason):
        """Move a file to FAILED/, saying why exactly once.

        On a failed move the path is handed back for another delivery: the
        watchdog keeps a delivered name in knownFiles, so a path not returned
        is never sighted again and the file is stranded in the watch folder
        under a log line claiming it was quarantined. On Windows the lock that
        makes open() raise makes MoveFileEx fail too, so that is the expected
        pairing, not a corner case.

        Both failure arms route through here so they cannot drift: the
        undecodable one and the read-failed-too-often one differ only in the
        reason they announce."""
        firstTime = path not in self._quarantineAnnounced
        if firstTime:
            self._quarantineAnnounced.add(path)
            logger.error(reason)
        try:
            shutil.move(path, self._destinationPath(path, subdirName="FAILED"))
            self._readAttempts.pop(path, None)
            self._quarantineAnnounced.discard(path)
        except Exception as moveError:
            logger.log(logging.ERROR if firstTime else logging.DEBUG,
                       "Could not move %s to FAILED/, will try again: %s", path, moveError)
            retryable.append(path)

    @staticmethod
    def _fileStamp(path):
        """(size, mtime), or None when it cannot be read right now.

        A locked file still answers os.stat - that is what the watchdog's own
        size-stabilization gate relies on - so this works in exactly the case
        it is needed for: telling a re-copied file apart from the one that
        failed before it."""
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return (stat.st_size, stat.st_mtime)

    def _countReadFailure(self, path):
        """Bump and return this path's consecutive unreadable-delivery count,
        starting over when the file is not the one the last failure was
        against."""
        stamp = self._fileStamp(path)
        previous = self._readAttempts.get(path)
        attempts = previous[0] + 1 if previous and previous[1] == stamp else 1
        self._readAttempts[path] = (attempts, stamp)
        return attempts

    def _destinationPath(self, path, subdirName="DONE"):
        fileDirectory = os.path.dirname(path)
        fileName = os.path.basename(path)
        destinationDirectory = os.path.join(fileDirectory, subdirName)
        if not os.path.exists(destinationDirectory):
            os.makedirs(destinationDirectory)
            logger.info(f"Created directory: {destinationDirectory}")
        destinationPath = os.path.join(destinationDirectory, fileName)
        if os.path.exists(destinationPath):
            base, ext = os.path.splitext(fileName)
            counter = 1
            while os.path.exists(os.path.join(destinationDirectory, f"{base}_{counter}{ext}")):
                counter += 1
            destinationPath = os.path.join(destinationDirectory, f"{base}_{counter}{ext}")
        return destinationPath

    def _handleImport(self, paths):
        """Import one watchdog batch. Every file dropped in the same poll cycle
        goes through a single importCallback call (Database.importHistoryBatch)
        so batch-scoped import state - the duplicate-claim tracking that stops
        a replay at the start of one file from "correcting away" the skip play
        at the end of the previous file - spans the whole batch.

        Returns the paths that could not be read but may read later, for the
        watchdog to deliver again (Watchdog._forgetRetryable). Everything else
        - imported, skipped, quarantined - is absent from that list and so
        stays known."""
        toImport = []  #< (path, content) of keyword-matching, readable files
        retryable = []  #< transiently unreadable: hand back for a later poll
        for path in sorted(paths):
            fileName = os.path.basename(path)
            if self.keyword is not None and self.keyword not in fileName:
                # Outside the read try/except on purpose: this file is never
                # read, so a failure MOVING it is not a read failure. Folded in,
                # it counted against MAX_TRANSIENT_READ_ATTEMPTS and put a
                # perfectly good non-matching file in FAILED/ under a message
                # blaming an unreadable file.
                logger.info(f"Keyword '{self.keyword}' not found in '{fileName}'. Skipping import and moving directly to DONE.")
                try:
                    shutil.move(path, self._destinationPath(path))
                except Exception as moveError:
                    logger.error(f"Could not move the non-matching file {path} to DONE/: {moveError}")
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    toImport.append((path, f.read()))
                self._readAttempts.pop(path, None)   #< it read; earlier locks are history
            except UnicodeDecodeError as e:
                # Undecodable content is permanent, so it is routed exactly like
                # an import failure below: left in the watch folder it would be
                # re-read and re-logged by every restart's initial scan (the
                # watchdog only suppresses the retry within one process), and it
                # would stay invisible to anyone not reading the log.
                # Deliberately NOT folded into the except below: a file that is
                # merely unreadable right now - held by an antivirus scanner or
                # backup tool, or still being copied when the startup scan
                # picked it up - raises OSError and reads fine on a later pass,
                # so quarantining that one would lose a good import.
                self._quarantine(path, retryable,
                                 f"Could not decode {os.path.basename(path)} as UTF-8 - moving to "
                                 f"FAILED/. Re-export it (or re-save it as UTF-8) and drop it back "
                                 f"into the watch folder: {e}")
            except Exception as e:
                # Transient by assumption (a lock that clears), so it is handed
                # back for another delivery rather than quarantined - but only
                # for a bounded number of tries, because the watchdog cannot
                # tell a lock that clears from one that never will.
                attempts = self._countReadFailure(path)
                if attempts < MAX_TRANSIENT_READ_ATTEMPTS:
                    logger.error(f"Error reading file {path} (attempt {attempts} of "
                                 f"{MAX_TRANSIENT_READ_ATTEMPTS}, will retry): {e}")
                    retryable.append(path)
                    continue
                # _quarantine owns the announce-once and the re-queue-on-failed-
                # move, shared with the decode arm above so the two cannot drift.
                self._quarantine(path, retryable,
                                 f"Could not read {os.path.basename(path)} after "
                                 f"{MAX_TRANSIENT_READ_ATTEMPTS} attempts - moving to FAILED/. "
                                 f"Close whatever is holding it and drop it back into the "
                                 f"watch folder: {e}")

        if not toImport:
            return retryable

        try:
            outcomes = self.importCallback([content for _, content in toImport])
        except Exception as e:
            # Files stay in the watch folder so a restart retries them. They are
            # NOT handed back for re-delivery: the read succeeded, so this is an
            # import failure, and re-delivering it would re-run the same failing
            # import every poll. Restart-scoped retry is the deliberate bound.
            logger.error(f"Error importing batch of {len(toImport)} file(s): {parseError(e)}")
            return retryable

        # Database.importHistoryBatch reports one outcome per file; a callback
        # that doesn't (older/simple callbacks, tests) gets the previous
        # assume-success behavior.
        if not isinstance(outcomes, list) or len(outcomes) != len(toImport):
            outcomes = ["imported"] * len(toImport)

        for (path, _), outcome in zip(toImport, outcomes):
            fileName = os.path.basename(path)
            try:
                if outcome == "failed":
                    # Visible in FAILED/ instead of celebrated in DONE/ - and
                    # out of the watch folder, so it isn't retried (and failed
                    # again) on every restart.
                    logger.error(f"Import failed for {fileName} - moving to FAILED/. "
                                 "Check the file (it may be corrupt or not a Spotify/Musicolet export) "
                                 "and drop a fixed copy back into the watch folder.")
                    shutil.move(path, self._destinationPath(path, subdirName="FAILED"))
                else:
                    logger.info(f"Successfully imported {fileName}")
                    shutil.move(path, self._destinationPath(path))
                    logger.info(f"Successfully moved {fileName} to DONE/")
            except Exception as e:
                logger.error(f"Error moving file {path}: {e}")

        return retryable

    def start(self) -> None:
        self.wd.watchFolder(self.folderPath, self._handleImport, self.pollInterval,
                            startupDelaySeconds=random.randint(AUTO_IMPORT_MIN_START_DELAY_SECONDS,
                                                               AUTO_IMPORT_MAX_START_DELAY_SECONDS))


if __name__ == "__main__":   #< manual smoke test: watch ../../autoImport and print each batch
    AutoImporter("../../autoImport", print, pollInterval=1, keyword="Weekly").start()