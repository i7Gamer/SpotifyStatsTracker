import os
import random
import time
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

try:
    from Database.utils import parseError
except ModuleNotFoundError:
    import sys
    folderPath = "../"
    if folderPath not in sys.path:
        sys.path.append(folderPath)
    from utils import parseError

class Watchdog:
    def __init__(self):
        self.run = True
        self._stop_event = threading.Event()

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
        if not os.path.exists(pathToWatch):
            os.makedirs(pathToWatch)
        try:
            knownFiles = {f for f in os.listdir(pathToWatch) if os.path.isfile(os.path.join(pathToWatch, f))}
            if callbackInitialFiles and knownFiles:
                fullPaths = sorted(os.path.join(pathToWatch, f) for f in knownFiles)
                for fullPath in fullPaths:
                    logger.info(f"File found: {fullPath}")
                callback(fullPaths)
        except FileNotFoundError:
            logger.error(f"Error: The directory {pathToWatch} does not exist.")
            return
        try:
            pendingSizes = {}   #< name -> size at last poll, for files waiting to stabilize
            while self.run and not self._stop_event.is_set():
                self._stop_event.wait(checkInterval)
                if not self.run or self._stop_event.is_set():
                    break
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
                    callback(readyPaths)
            logger.info("Watchdog stopped peacefully")

        except Exception as e:
            logger.error(f"Stopping monitor... {parseError(e)}")

    def watchFolder(self, pathToWatch, callback, checkInterval=5, startupDelaySeconds=0):
        self._stop_event.clear()
        self.thread = threading.Thread(
            target=self.watchFolder_blocking,
            args=(pathToWatch, callback, checkInterval),
            kwargs={"startupDelaySeconds": startupDelaySeconds},
            daemon=True
        )
        self.thread.start()
    
    def signalStop(self):
        """Signal-only half of stop() - no join, safe for shutdown's
        signal-everything-first phase."""
        self._stop_event.set()
        self.run = False

    def stop(self):
        self.signalStop()
        if hasattr(self, "thread") and self.thread.is_alive():
            self.thread.join(timeout=WATCHDOG_STOP_JOIN_TIMEOUT_SECONDS)

class AutoImporter:
    def __init__(self, folderPath, importCallback, pollInterval=5, keyword=None):
        self.folderPath = folderPath
        self.pollInterval = pollInterval
        self.importCallback = importCallback
        self.keyword = keyword
        self.wd = Watchdog()

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
        at the end of the previous file - spans the whole batch."""
        toImport = []  #< (path, content) of keyword-matching, readable files
        for path in sorted(paths):
            try:
                fileName = os.path.basename(path)
                if self.keyword is not None and self.keyword not in fileName:
                    logger.info(f"Keyword '{self.keyword}' not found in '{fileName}'. Skipping import and moving directly to DONE.")
                    shutil.move(path, self._destinationPath(path))
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    toImport.append((path, f.read()))
            except Exception as e:
                logger.error(f"Error reading file {path}: {e}")

        if not toImport:
            return

        try:
            outcomes = self.importCallback([content for _, content in toImport])
        except Exception as e:
            # Files stay in the watch folder so a restart retries them.
            logger.error(f"Error importing batch of {len(toImport)} file(s): {parseError(e)}")
            return

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

    def start(self):
        self.wd.watchFolder(self.folderPath, self._handleImport, self.pollInterval,
                            startupDelaySeconds=random.randint(AUTO_IMPORT_MIN_START_DELAY_SECONDS,
                                                               AUTO_IMPORT_MAX_START_DELAY_SECONDS))


if __name__ == "__main__":
    autoImporter = AutoImporter("../../autoImport", print, pollInterval=1, keyword="Weekly")
    autoImporter.start()