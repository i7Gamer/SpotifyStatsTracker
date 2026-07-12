import logging
import logging.handlers
from pathlib import Path

from Database import db as dbModule

LOG_FILE_NAME = "app.log"
LOG_MAX_BYTES = 5 * 1024 * 1024   #< rotate once the active log file passes this size
LOG_BACKUP_COUNT = 3               #< how many rotated files to keep alongside the active one
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_addedHandlers: list[logging.Handler] = []


def configureLogging(logDir: Path | str | None = None) -> Path:
    """Route every module's logging.getLogger(__name__) calls to both stderr and
    a rotating file (Database/Data/app.log by default, next to the database) -
    so a failure that used to only ever reach print() (invisible once the
    console is gone) leaves a persistent trail instead.

    Idempotent: safe to call more than once (e.g. from tests with a different
    logDir each time) - re-running it replaces this module's handlers rather
    than stacking duplicates onto the root logger.
    """
    root = logging.getLogger()
    for handler in _addedHandlers:
        root.removeHandler(handler)
        handler.close()  #< release the previous log file (Windows keeps it locked otherwise)
    _addedHandlers.clear()

    targetDir = Path(logDir) if logDir is not None else dbModule.DEFAULT_DB_PATH.parent
    targetDir.mkdir(parents=True, exist_ok=True)
    logFile = targetDir / LOG_FILE_NAME

    root.setLevel(logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)

    fileHandler = logging.handlers.RotatingFileHandler(
        logFile, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    fileHandler.setFormatter(formatter)
    root.addHandler(fileHandler)
    _addedHandlers.append(fileHandler)

    streamHandler = logging.StreamHandler()
    streamHandler.setFormatter(formatter)
    root.addHandler(streamHandler)
    _addedHandlers.append(streamHandler)

    return logFile
