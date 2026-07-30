# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cookie-string parsing and session-file writing for the login flow.

parseCookieString accepts whatever a user actually pastes into the login form:
a devtools "k=v; k2=v2" header line, a Netscape tab-separated export (with its
NAME/VALUE header row and # comments), or bare k=v lines. saveSession writes
the sessions file spotapi's JSONSaver and the login verification read - a JSON
list of {"identifier", "cookies"} entries, one per account.
"""

import json
from pathlib import Path


def _pairsFromSeparatedLine(line: str) -> dict:
    """k=v pairs out of a "k=v; k2=v2"-style line (a lone k=v included)."""
    cookies = {}
    for chunk in line.split(";"):
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        if name.strip():
            cookies[name.strip()] = value.strip()
    return cookies


def parseCookieString(cookieString: str) -> dict:
    """Pasted cookie text to a {name: value} dict. Unrecognizable lines are
    skipped rather than fatal - a stray comment must not blank the whole
    paste."""
    cookies = {}
    for line in str(cookieString or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            # Netscape/table export: name<TAB>value<TAB>domain... The header
            # row spells its value column "VALUE" - skip it, no cookie is
            # named that way.
            columns = line.split("\t")
            if len(columns) >= 2 and columns[0].strip() and columns[1].strip() != "VALUE":
                cookies[columns[0].strip()] = columns[1].strip()
        elif "=" in line:
            cookies.update(_pairsFromSeparatedLine(line))
    return cookies


def saveSession(cookies: dict, identifier: str, outputFile: str = "sessions.json") -> bool:
    """Write one account's cookies into the sessions file, replacing any
    existing entry for the same identifier (a re-login supersedes the old
    cookies; appending would leave login() picking whichever came first). A
    missing or corrupt file starts fresh rather than failing - the entry being
    written is the only state worth keeping at that point."""
    outputPath = Path(outputFile)

    sessions = []
    if outputPath.exists():
        try:
            sessions = json.loads(outputPath.read_text(encoding="utf-8"))
        except Exception:
            sessions = []
    if not isinstance(sessions, list):
        sessions = []

    sessions = [entry for entry in sessions
                if isinstance(entry, dict) and entry.get("identifier") != identifier]
    sessions.append({"identifier": identifier, "cookies": cookies})

    outputPath.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
    return True
