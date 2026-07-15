import os
import time
import threading
import shutil
import logging

logger = logging.getLogger(__name__)

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

    def watchFolder_blocking(self, pathToWatch, callback, checkInterval=5, callbackInitialFiles=True):
        """`callback` receives the LIST of files discovered in one scan (sorted
        by path), not one call per file: files dropped together must be
        processed as a single batch so batch-scoped import state (duplicate-
        claim tracking across file boundaries, see Database.importHistoryBatch)
        covers all of them."""
        logger.info(f"Monitoring {pathToWatch} for new files (Polling)...")
        if not os.path.exists(pathToWatch):
            os.makedirs(pathToWatch)
        try:
            filesBefore = {f for f in os.listdir(pathToWatch) if os.path.isfile(os.path.join(pathToWatch, f))}
            if callbackInitialFiles and filesBefore:
                fullPaths = sorted(os.path.join(pathToWatch, f) for f in filesBefore)
                for fullPath in fullPaths:
                    logger.info(f"File found: {fullPath}")
                callback(fullPaths)
        except FileNotFoundError:
            logger.error(f"Error: The directory {pathToWatch} does not exist.")
            return
        try:
            while self.run and not self._stop_event.is_set():
                self._stop_event.wait(checkInterval)
                if not self.run or self._stop_event.is_set():
                    break
                filesAfter = {f for f in os.listdir(pathToWatch) if os.path.isfile(os.path.join(pathToWatch, f))}
                filesAdded = filesAfter - filesBefore

                if filesAdded:
                    fullPaths = sorted(os.path.join(pathToWatch, f) for f in filesAdded)
                    for fullPath in fullPaths:
                        logger.info(f"New file created: {fullPath}")
                    callback(fullPaths)
                filesBefore = filesAfter
            logger.info("Watchdog stopped peacefully")

        except Exception as e:
            logger.error(f"Stopping monitor... {parseError(e)}")

    def watchFolder(self, pathToWatch, callback, checkInterval=5):
        self._stop_event.clear()
        self.thread = threading.Thread(
            target=self.watchFolder_blocking,
            args=(pathToWatch, callback, checkInterval),
            daemon=True
        )
        self.thread.start()
    
    def stop(self):
        self._stop_event.set()
        self.run = False
        if hasattr(self, "thread") and self.thread.is_alive():
            self.thread.join(timeout=2)

class AutoImporter:
    def __init__(self, folderPath, importCallback, pollInterval=5, keyword=None):
        self.folderPath = folderPath
        self.pollInterval = pollInterval
        self.importCallback = importCallback
        self.keyword = keyword
        self.wd = Watchdog()

    def _destinationPath(self, path):
        fileDirectory = os.path.dirname(path)
        fileName = os.path.basename(path)
        doneDirectory = os.path.join(fileDirectory, "DONE")
        if not os.path.exists(doneDirectory):
            os.makedirs(doneDirectory)
            logger.info(f"Created directory: {doneDirectory}")
        destinationPath = os.path.join(doneDirectory, fileName)
        if os.path.exists(destinationPath):
            base, ext = os.path.splitext(fileName)
            counter = 1
            while os.path.exists(os.path.join(doneDirectory, f"{base}_{counter}{ext}")):
                counter += 1
            destinationPath = os.path.join(doneDirectory, f"{base}_{counter}{ext}")
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
            self.importCallback([content for _, content in toImport])
        except Exception as e:
            # Files stay in the watch folder so a restart retries them.
            logger.error(f"Error importing batch of {len(toImport)} file(s): {parseError(e)}")
            return

        for path, _ in toImport:
            fileName = os.path.basename(path)
            try:
                logger.info(f"Successfully imported {fileName}")
                shutil.move(path, self._destinationPath(path))
                logger.info(f"Successfully moved {fileName} to DONE/")
            except Exception as e:
                logger.error(f"Error moving file {path}: {e}")

    def start(self):
        self.wd.watchFolder(self.folderPath, self._handleImport, self.pollInterval)


if __name__ == "__main__":
    autoImporter = AutoImporter("../../autoImport", print, pollInterval=1, keyword="Weekly")
    autoImporter.start()