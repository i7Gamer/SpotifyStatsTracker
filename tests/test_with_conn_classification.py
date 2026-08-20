# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""No NEW `with conn:` block may decide a write from a read it does not hold.

Python's sqlite3 under legacy transaction control BEGINs only for DML, so a
SELECT inside `with conn:` runs in autocommit and takes no lock. A block that
reads, decides, then writes is therefore deciding from state a concurrent
writer can change before the write lands - the lost-update class that produced
four confirmed bugs in the 2026-08-15 review.

The 2026-08-16 sweep classified all ~100 blocks and fixed the guard-shaped
ones. What it did not leave behind was a way to KEEP that true:
tests/test_guard_transactions.py pins six verbs BY NAME, so a new guard-shaped
block added anywhere else fails nothing. The population grew from 103 to 108
within four days of the sweep, unexamined. This file is the missing half - the
sweep's result as a property rather than as a moment.

The classification is structural, from the AST: within each block, the leading
SQL keyword of every execute() call, in source order. A block is fine if it
never writes, if it writes without reading first, or if it opens with an
explicit BEGIN IMMEDIATE (which does take the lock). It is a finding if a read
precedes its first write - and then it must be named in GUARD_EXEMPT with the
reason it is safe anyway.

Deliberately not a lint rule or a grep: the property is POSITIONAL (a read
AFTER the first write is fine, and several verbs rely on that), which no
pattern over text can see.
"""
import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories with no production code in them. tools/ and the migrators are
# deliberately INCLUDED: a migrator runs against a live database at startup,
# which is exactly when getting this wrong is least recoverable.
SKIPPED_DIRECTORIES = {".venv", "tests", "__pycache__", ".git", "node_modules"}

READ_KEYWORDS = ("SELECT", "PRAGMA", "WITH", "EXPLAIN")
WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER",
                  "DROP", "BEGIN", "VACUUM", "REINDEX", "ANALYZE")
EXECUTE_METHODS = ("execute", "executemany", "executescript")
# SQL this test could not read (a variable, an attribute, a f-string with no
# leading literal). Counted as a READ for ordering, which is the conservative
# direction: it can raise a false finding, which someone then classifies, but
# it can never hide a real one.
UNREADABLE_SQL = "?UNREADABLE"

# Blocks that DO read before writing and are safe anyway. Keyed by enclosing
# function, not by line number - a line number churns on every edit above it,
# and a test whose upkeep is "bump the number" gets its numbers bumped without
# anybody re-reading the code.
#
# Adding an entry here is a claim that needs the same evidence the sweep
# demanded: name what serializes the block, or why the write is safe to lose
# the race.
GUARD_EXEMPT = {
    ("Database/queries/shares.py", "createShareRequest"):
        "Serialized by SharesQueries._shareWriteLock, a CLASS-level threading.Lock "
        "held around the whole block - see its comment. The lock exists for exactly "
        "this: two crossing requests (A->B and B->A) could both pass the "
        "reverse-pending check before either INSERT landed, leaving two "
        "opposite-direction pending rows that the same-direction UNIQUE constraint "
        "does not cover. Single-process deployment, so one process-wide lock is a "
        "complete answer.",
    ("Database/queries/users.py", "promoteEarliestUserToAdminIfNoneExists"):
        "Losing this race changes nothing. Both threads read the same empty-admin "
        "state, both pick the same row (ORDER BY created_at, username is total - "
        "there is no tie to break differently), and both write is_admin=1 to it. "
        "The write is idempotent and the selection deterministic, so the "
        "interleaving is unobservable. It also only runs at startup and from "
        "migration 1.17.0, before any request thread exists.",
}


def _leadingKeyword(call: ast.Call) -> str | None:
    """The leading SQL keyword of an execute()-family call, or None if this
    call is not one."""
    function = call.func
    if not isinstance(function, ast.Attribute) or function.attr not in EXECUTE_METHODS:
        return None
    if not call.args:
        return None

    argument = call.args[0]
    text = None
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        text = argument.value
    elif isinstance(argument, ast.JoinedStr):
        #< the first literal chunk: every f-string SQL in this repo interpolates
        #  a column list or placeholders, never the leading verb
        for part in argument.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str) and part.value.strip():
                text = part.value
                break

    if text is None or not text.strip():
        return UNREADABLE_SQL
    return text.strip().split()[0].upper()


def _isConnectionWith(node: ast.AST) -> bool:
    """`with conn:` / `with self._conn():`-style blocks, by the name bound.

    Matched on the name ending in "conn" rather than by resolving types: this
    repo names every one of them `conn`, and a test that needed type inference
    to see them would be a test nobody trusts."""
    if not isinstance(node, ast.With):
        return False
    for item in node.items:
        expression = item.context_expr
        if isinstance(expression, ast.Name):
            name = expression.id
        elif isinstance(expression, ast.Attribute):
            name = expression.attr
        else:
            continue
        if name.lower().endswith("conn"):
            return True
    return False


def _enclosingFunctions(tree: ast.AST) -> dict[int, str]:
    """Line number -> name of the innermost function containing it."""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            for line in range(node.lineno, end + 1):
                #< innermost wins: an inner def is walked after its parent only
                #  by luck, so prefer the SMALLER span rather than the last seen
                previous = owner.get(line)
                if previous is None or node.end_lineno - node.lineno < owner.get((line, "span"), 10 ** 9):
                    owner[line] = node.name
                    owner[(line, "span")] = node.end_lineno - node.lineno
    return {k: v for k, v in owner.items() if isinstance(k, int)}


def classifyBlock(block: ast.With) -> tuple[str, list[str]]:
    """(verdict, the block's SQL keywords in source order)."""
    statements = []
    for node in ast.walk(block):
        if isinstance(node, ast.Call):
            keyword = _leadingKeyword(node)
            if keyword:
                statements.append((node.lineno, node.col_offset, keyword))
    statements.sort()
    keywords = [k for _line, _col, k in statements]

    writeIndexes = [i for i, k in enumerate(keywords) if k in WRITE_KEYWORDS]
    if not writeIndexes:
        return "read-only", keywords
    firstWrite = writeIndexes[0]
    if keywords[firstWrite] == "BEGIN":
        return "begin-immediate", keywords
    readIndexes = [i for i, k in enumerate(keywords)
                   if k in READ_KEYWORDS or k == UNREADABLE_SQL]
    if any(i < firstWrite for i in readIndexes):
        return "read-before-write", keywords
    return "write-only", keywords


def productionSourceFiles():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if SKIPPED_DIRECTORIES & set(path.parts):
            continue
        yield path


def collectBlocks():
    """(relativePath, functionName, verdict, keywords) for every `with conn:`."""
    found = []
    for path in productionSourceFiles():
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        owners = _enclosingFunctions(tree)
        relative = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not _isConnectionWith(node):
                continue
            verdict, keywords = classifyBlock(node)
            found.append((relative, owners.get(node.lineno, "<module>"), verdict, keywords))
    return found


class TestNoUnclassifiedGuardShapedBlock(unittest.TestCase):
    def setUp(self):
        self.blocks = collectBlocks()

    def test_the_scan_finds_the_blocks_at_all(self):
        """The guard against this whole file silently passing: if the AST walk
        stops matching (a rename of `conn`, a restructure), every assertion
        below goes vacuously green while the property stops being checked."""
        self.assertGreater(len(self.blocks), 50,
                           "the `with conn:` scan found almost nothing - it has stopped working, "
                           "and every other assertion in this file is now vacuous")

    def test_no_block_reads_before_writing_without_saying_why(self):
        """The property the 2026-08-16 sweep established, kept.

        A read before the first write in a `with conn:` holds no lock, so the
        write is decided from state another thread can move underneath it. If a
        block genuinely is safe, GUARD_EXEMPT is where the reason goes."""
        offenders = [
            (path, function, keywords)
            for path, function, verdict, keywords in self.blocks
            if verdict == "read-before-write" and (path, function) not in GUARD_EXEMPT
        ]

        self.assertEqual([], offenders, "\n".join(
            [""]
            + [f"  {path}::{function} reads before it writes: {keywords}" for path, function, keywords in offenders]
            + ["",
               "Either take the lock (conn.execute('BEGIN IMMEDIATE') as the block's first",
               "statement - see Database/queries/schema.py for the template), or add the site to",
               "GUARD_EXEMPT in this file with the reason it is safe without one."]))

    def test_every_exemption_still_describes_a_real_block(self):
        """A stale exemption is worse than none: it reads as a reviewed
        decision about code that has since been rewritten, and it silently
        widens the allowlist for whatever takes that name next."""
        actual = {(path, function) for path, function, verdict, _ in self.blocks
                  if verdict == "read-before-write"}

        for key in GUARD_EXEMPT:
            with self.subTest(site=key):
                self.assertIn(key, actual,
                              f"{key[0]}::{key[1]} is exempted here but no longer reads before it "
                              "writes - remove the entry rather than leaving a decision recorded "
                              "about code that no longer exists")

    def test_every_exemption_carries_an_actual_reason(self):
        """The entry IS the review. A placeholder would make the allowlist a
        way to silence this test rather than a way to record a judgement."""
        for key, reason in GUARD_EXEMPT.items():
            with self.subTest(site=key):
                self.assertGreater(len(reason.split()), 20,
                                   f"{key} needs a real explanation, not a note")


if __name__ == "__main__":
    unittest.main()
