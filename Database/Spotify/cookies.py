# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cookie-string parsing and session-file writing for the login flow.

parseCookieString accepts whatever a user actually pastes into the login form:
a devtools "k=v; k2=v2" header line, a DevTools table copy (name/value-first
tab columns, with its NAME/VALUE header row), a real Netscape cookies.txt
export (domain-first 7-column lines, # comments, #HttpOnly_-prefixed rows), or
bare k=v lines. saveSession writes the sessions file spotapi's JSONSaver and
the login verification read - a JSON list of {"identifier", "cookies"}
entries, one per account.
"""

import json
from pathlib import Path

# A real Netscape cookies.txt row is domain-first, 7 tab-separated columns:
# domain, include-subdomains flag, path, secure flag, expiry, name, value.
# The two flag columns are TRUE/FALSE literals - the discriminator that tells
# such a row apart from a DevTools table copy, whose column 2 is the cookie's
# VALUE and column 4 (when present) a path, neither ever a bare flag. Column
# count alone can't discriminate: a table copy has 7+ columns too.
_NETSCAPE_MIN_COLUMNS = 7
_NETSCAPE_SUBDOMAIN_FLAG_COLUMN = 1
_NETSCAPE_SECURE_FLAG_COLUMN = 3
_NETSCAPE_NAME_COLUMN = 5
_NETSCAPE_VALUE_COLUMN = 6
_NETSCAPE_FLAG_VALUES = frozenset({"TRUE", "FALSE"})
# curl and the cookies.txt exporters hide HttpOnly cookies behind this prefix -
# comment-shaped rows that are not comments, and sp_dc/sp_key (the cookies the
# login actually needs) are HttpOnly, so dropping them killed the whole paste.
_HTTPONLY_PREFIX = "#HttpOnly_"


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


def _isNetscapeRow(columns: list) -> bool:
    """Whether these tab columns are a domain-first Netscape cookies.txt row
    (see the column constants above) rather than a name-first table copy."""
    return (len(columns) >= _NETSCAPE_MIN_COLUMNS
            and columns[_NETSCAPE_SUBDOMAIN_FLAG_COLUMN].strip().upper() in _NETSCAPE_FLAG_VALUES
            and columns[_NETSCAPE_SECURE_FLAG_COLUMN].strip().upper() in _NETSCAPE_FLAG_VALUES)


def parseCookieString(cookieString: str) -> dict:
    """Pasted cookie text to a {name: value} dict. Unrecognizable lines are
    skipped rather than fatal - a stray comment must not blank the whole
    paste."""
    cookies = {}
    for line in str(cookieString or "").splitlines():
        line = line.strip()
        if line.startswith(_HTTPONLY_PREFIX):
            #< before the comment check below, or the row is silently dropped
            line = line[len(_HTTPONLY_PREFIX):]
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            columns = line.split("\t")
            if _isNetscapeRow(columns):
                # Real cookies.txt export: the name and value are the last two
                # of the seven columns.
                if columns[_NETSCAPE_NAME_COLUMN].strip():
                    cookies[columns[_NETSCAPE_NAME_COLUMN].strip()] = \
                        columns[_NETSCAPE_VALUE_COLUMN].strip()
            elif len(columns) >= 2 and columns[0].strip() and columns[1].strip() != "VALUE":
                # DevTools/table copy: name<TAB>value<TAB>domain... The header
                # row spells its value column "VALUE" - skip it, no cookie is
                # named that way.
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
