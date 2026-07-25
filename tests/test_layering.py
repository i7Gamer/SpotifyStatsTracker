"""Boundaries that are cheap to keep and expensive to rediscover.

These are the two layering rules this codebase actually has. Neither fails
loudly when broken - the app keeps working, and the erosion only shows up later
as a query nobody can find or an import cycle nobody can untangle - so they are
asserted here rather than left to review.

1. SQL lives in the repository. Database/database.py once reached through
   repo._conn() to run five of its own statements, which put the data layer's
   boundary wherever a given method happened to stop.
2. config.py imports nothing from the app or from Database. That is what makes
   it safe for any layer to import (including in default arguments), and it is
   the property the whole "which constants live where" question rests on - see
   Database/queries/_base.py's docstring.
"""
import ast
import builtins
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

REPO_ROOT = Path(__file__).resolve().parent.parent
QUERY_LAYER = REPO_ROOT / "Database" / "queries"


def _pythonFiles(*roots: Path):
    for root in roots:
        for path in root.rglob("*.py"):
            if "__pycache__" not in path.parts:
                yield path


class TestSqlStaysInTheQueryLayer(unittest.TestCase):
    """_conn() hands out the raw sqlite3 connection. Anything holding one is
    writing SQL, so where it may be called IS where SQL may be written."""

    #< Repository defines and exposes it. The migrators are one-off scripts
    #  that build their own Repository against an explicit path and do surgery
    #  the query layer has no reason to carry a method for - each runs once,
    #  against a schema version that no longer exists.
    ALLOWED = {REPO_ROOT / "Database" / "repository.py"}
    ALLOWED_DIRS = {REPO_ROOT / "Database" / "Migrators"}

    def test_only_the_query_mixins_touch_the_connection(self):
        offenders = []
        for path in _pythonFiles(REPO_ROOT / "Database", REPO_ROOT / "routes",
                                 REPO_ROOT / "dashboard", REPO_ROOT / "services"):
            if (path in self.ALLOWED or path.parent == QUERY_LAYER
                    or path.parent in self.ALLOWED_DIRS):
                continue
            source = path.read_text(encoding="utf-8")
            for lineNo, line in enumerate(source.splitlines(), start=1):
                if "._conn()" in line and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineNo}")

        self.assertEqual(offenders, [], "\n".join([
            "Raw connection use outside Database/queries/.",
            "Move the statement onto the mixin that owns its tables and call it from here.",
            *offenders]))


class TestConfigStaysALeaf(unittest.TestCase):
    """config.py is importable from every layer precisely because it imports
    from none of them. One import back into Database or app makes every
    `from config import ...` a potential cycle."""

    def test_config_imports_nothing_from_the_app_or_the_database(self):
        tree = ast.parse((REPO_ROOT / "config.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        self.assertEqual(imported & {"app", "Database", "routes", "dashboard", "services"}, set(),
                         f"config.py must stay a leaf module; it imports {sorted(imported)}")


class TestMigratorEntryPointsAreRunnable(unittest.TestCase):
    """Each migrator is also a script. Three of them printed counts off an
    undefined `result`, so running one directly raised NameError - after the
    migration had already been applied, which is the worst moment for it."""

    def test_no_migrator_references_an_undefined_name(self):
        broken = []
        for path in sorted((REPO_ROOT / "Database" / "Migrators").glob("migrate*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            assigned = {node.id for node in ast.walk(tree)
                        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
            assigned |= {node.name for node in ast.walk(tree)
                         if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
            assigned |= {alias.asname or alias.name.split(".")[0] for node in ast.walk(tree)
                         if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
            # Only the module-level script body: a name inside a function may
            # legitimately come from an argument or an enclosing scope.
            for statement in tree.body:
                if not isinstance(statement, ast.If):
                    continue
                for node in ast.walk(statement):
                    if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                            and node.id not in assigned and not hasattr(builtins, node.id)):
                        broken.append(f"{path.name}:{node.lineno} uses undefined `{node.id}`")

        self.assertEqual(broken, [], "\n".join(broken))


if __name__ == "__main__":
    unittest.main()
