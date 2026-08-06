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
  * A source tree that no longer fingerprints the way it did at startup
    catches those, but cannot say what changed.

The comparison is pure; the readings it needs come from app.py, which takes the
startup fingerprint while what is on disk is still what got loaded."""
import hashlib
import os
from pathlib import Path

# The trees holding what this process loads ONCE: the Python modules imported
# at boot and the templates Jinja compiles on first render (auto_reload is off
# outside debug). static/ is deliberately absent - it is read from disk per
# request, so a change there is already live.
_SOURCE_TREES = ("templates", "routes", "services", "dashboard", "Database")
_SOURCE_SUFFIXES = (".py", ".html")
# Data is the users' live databases, written constantly; the rest is build and
# runtime residue. Walking any of them in would make the banner permanent.
_SKIP_DIRS = {"__pycache__", "Data", "backups", "logs", ".git", ".venv", "node_modules"}


def sourceFingerprint(baseDir) -> str | None:
    """A digest of this app's own source files - each one's path, mtime and
    size - or None when there is nothing to read (an unusual layout, or a path
    that does not exist; either way, no claim to make).

    Compared against the SAME reading taken at startup, rather than against the
    clock. The obvious check is "is any source file newer than this process",
    and it is wrong for half of all deploys: a copy that preserves timestamps
    leaves the new files dated whenever the build was cut, which is usually
    BEFORE the running process started. robocopy - the obvious tool on this
    platform - preserves them by default, as do rsync -a, cp -p and tar -x. The
    banner would then have silently degraded to the version check alone, which
    is the case the file signal exists to cover.

    A digest notices a change in either direction, including a rollback to an
    older build. Path and size ride along with the mtime because a same-second
    replacement is otherwise invisible; hashing the contents would close the
    last gap and is not worth reading every template on every /admin load."""
    baseDir = Path(baseDir)
    entries = []

    def consider(path):
        try:
            stat = path.stat()
        except OSError:
            return   #< vanished between the walk and the stat; not our business
        entries.append(f"{path.relative_to(baseDir).as_posix()}|{stat.st_mtime_ns}|{stat.st_size}")

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
    if not entries:
        return None
    #< sorted, because os.walk's order is the filesystem's and this has to be
    #  the same string for the same tree on every reading
    return hashlib.sha256("\n".join(sorted(entries)).encode("utf-8")).hexdigest()


def deployMismatch(runningVersion: str, diskVersion: str | None,
                   bootFingerprint: str | None, currentFingerprint: str | None) -> dict | None:
    """None when this process matches its files, else what differs::

        {"runningVersion": str, "diskVersion": str | None, "filesChanged": bool}

    `diskVersion` is None in the result when the version itself is unchanged,
    so the banner has nothing misleading to render for an unbumped deploy. A
    version that could not be READ (None or blank) is not a mismatch: the
    reading failed, the deploy did not, and a banner that cries wolf is a
    banner an admin learns to ignore. A fingerprint that could not be taken -
    at boot or now - is the same kind of silence."""
    versionChanged = bool(diskVersion) and diskVersion != runningVersion
    filesChanged = bool(bootFingerprint) and bool(currentFingerprint) \
        and bootFingerprint != currentFingerprint
    if not (versionChanged or filesChanged):
        return None
    return {"runningVersion": runningVersion,
            "diskVersion": diskVersion if versionChanged else None,
            "filesChanged": filesChanged}
