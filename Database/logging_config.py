# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import datetime
import logging
import logging.handlers
from pathlib import Path

from Database import db as dbModule
#< the module, not `from ... import tz`: the zone is chosen once at import there,
#  and reading it as an attribute per record is what lets a test substitute one.
#  Database/utils.py imports nothing of ours (stdlib only), so it cannot take
#  part in the import cycle Database/dbmodule.py exists to break.
from Database import utils as dbUtils

LOG_FILE_NAME = "app.log"
LOG_MAX_BYTES = 5 * 1024 * 1024   #< rotate once the active log file passes this size
LOG_BACKUP_COUNT = 3               #< how many rotated files to keep alongside the active one
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"   #< milliseconds and the UTC offset are appended below

_addedHandlers: list[logging.Handler] = []


class InstanceZoneFormatter(logging.Formatter):
    """Stamps each line in the instance's configured zone (Database/utils.py's
    `tz`, resolved from TZ through ZoneInfo) rather than the C runtime's.

    logging.Formatter renders %(asctime)s through time.localtime, and on Windows
    the UCRT reads TZ itself and parses it as a POSIX spec - so an IANA name is
    neither understood nor ignored: TZ=Europe/Zurich resolved to a fixed UTC+1
    with no DST, putting every line an hour behind the wall clock for half the
    year. (TZ=America/Los_Angeles lands on that same wrong answer, which is how
    you can tell it is reading letters rather than looking anything up.) The
    app's own dates never had this - they go through ZoneInfo - so what this
    fixes is the log disagreeing with the data printed beside it.

    Going through the same `tz` the data uses is the point: the two agree by
    construction afterwards, on any platform, however TZ is spelled. TZ itself
    stays exactly as it was - it is the only thing that tells ZoneInfo which
    zone this instance keeps, and it was never the wrong setting.

    The offset is written out for a related reason - a bare local time in a file
    that outlives a DST change cannot be read back unambiguously, and this log
    gets read months later, by hand, next to an incident."""

    def formatTime(self, record, datefmt=None):
        stamped = datetime.datetime.fromtimestamp(record.created, tz=dbUtils.tz)
        if datefmt:
            return stamped.strftime(datefmt)
        #< the shape the file has always had (…13:18:12,095), plus the offset
        return (f"{stamped.strftime(LOG_TIME_FORMAT)},{int(record.msecs):03d}"
                f"{stamped.strftime('%z')}")


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
    formatter = InstanceZoneFormatter(LOG_FORMAT)

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
