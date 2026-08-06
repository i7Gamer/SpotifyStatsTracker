# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether the files on disk are still the ones this process is running.

A file-copy deploy that skips the restart leaves an instance running two builds
at once. Flask serves static/ from disk on every request - and stamps each URL
with that file's mtime, so cache-busting works perfectly and delivers the new
scripts - while route code and compiled Jinja templates stay in memory from
boot. Old markup, new JavaScript.

It presents as unrelated frontend bugs rather than as a deploy problem, and
the obvious evidence points away: the version badge reports the RUNNING
version and is telling the truth. /admin renders what this reports so the
state names itself.

Two signals, because either alone has a hole:

  * The VERSION file is authoritative but only moves on a release bump - 25
    commits once shipped between two bumps, an entire frontend migration
    among them.
  * A source file newer than the process catches those, but cannot say what
    changed.

The comparison is pure; the two readings it needs come from app.py."""
import os
from pathlib import Path

# A deploy copies files and then starts the app, so a source mtime BEFORE
# startup is the normal case and needs no allowance. This covers the clock
# skew and filesystem timestamp granularity around that boundary, not a copy
# that lands mid-boot - which is a broken deploy either way.
DEPLOY_MTIME_GRACE_SECONDS = 60

# The trees holding what this process loads ONCE: the Python modules imported
# at boot and the templates Jinja compiles on first render (auto_reload is off
# outside debug). static/ is deliberately absent - it is read from disk per
# request, so a change there is already live.
_SOURCE_TREES = ("templates", "routes", "services", "dashboard", "Database")
_SOURCE_SUFFIXES = (".py", ".html")
# Data is the users' live databases, written constantly; the rest is build and
# runtime residue. Walking any of them in would make the banner permanent.
_SKIP_DIRS = {"__pycache__", "Data", "backups", "logs", ".git", ".venv", "node_modules"}


def newestSourceMtime(baseDir) -> float | None:
    """The newest mtime among this app's own source files, or None when there
    is nothing to read (an unusual layout, or a path that does not exist -
    either way, no claim to make)."""
    baseDir = Path(baseDir)
    newest = None

    def consider(path):
        nonlocal newest
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return   #< vanished between the walk and the stat; not our business
        if newest is None or mtime > newest:
            newest = mtime

    for tree in _SOURCE_TREES:
        for dirPath, dirNames, fileNames in os.walk(baseDir / tree):
            dirNames[:] = [name for name in dirNames if name not in _SKIP_DIRS]
            for name in fileNames:
                if name.endswith(_SOURCE_SUFFIXES):
                    consider(Path(dirPath) / name)

    #< app.py, config.py and wsgi.py sit at the root rather than in a package
    if baseDir.is_dir():
        for path in baseDir.glob("*.py"):
            consider(path)
    return newest


def deployMismatch(runningVersion: str, diskVersion: str | None,
                   startedAt: float, sourceMtime: float | None,
                   grace: float = DEPLOY_MTIME_GRACE_SECONDS) -> dict | None:
    """None when this process matches its files, else what differs::

        {"runningVersion": str, "diskVersion": str | None, "filesChanged": bool}

    `diskVersion` is None in the result when the version itself is unchanged,
    so the banner has nothing misleading to render for an unbumped deploy. A
    version that could not be READ (None or blank) is not a mismatch: the
    reading failed, the deploy did not, and a banner that cries wolf is a
    banner an admin learns to ignore."""
    versionChanged = bool(diskVersion) and diskVersion != runningVersion
    filesChanged = sourceMtime is not None and sourceMtime > startedAt + grace
    if not (versionChanged or filesChanged):
        return None
    return {"runningVersion": runningVersion,
            "diskVersion": diskVersion if versionChanged else None,
            "filesChanged": filesChanged}
