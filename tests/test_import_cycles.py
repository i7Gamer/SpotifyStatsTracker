"""Every module here must import on its own, in any order.

Database.database imports Database.workers for its mixins, and the worker
modules read Database.database's module globals back (`_dbmod.logger`,
`_dbmod.parseError`, ...) so the suite's patch("Database.database.X") targets
reach them. That is a cycle, and it used to be survivable only in one
direction: importing Database.database FIRST left the package half-initialized
but working, while importing any worker submodule first raised

    ImportError: cannot import name 'WorkerLifecycleMixin' from partially
    initialized module 'Database.workers'

Which is not hypothetical - it bit Database/backup.py (imported standalone by
the migrators, before any of that exists) and then the test that reads the
join constants out of Database.workers.periodic.

Each import runs in its OWN interpreter: import order is process-global, and
by the time any test in this suite runs, conftest has long since imported
Database.database - so an in-process check would pass no matter what.
"""
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# One entry per import that must stand alone. Each is a module that either sits
# inside the cycle or is pulled in by something outside it.
FIRST_IMPORTS = (
    "Database.workers.periodic",
    "Database.workers.listener",
    "Database.workers.metadata_backfiller",
    "Database.workers.lastfm_backfillers",
    "Database.workers.wrapped_worker",
    "Database.workers",
    "Database.backup",          #< the migrators import this one standalone
    "Database.telemetry",
    "Database.import_service",
    "Database.media_fetch",
    "Database.Listeners.spotifyListener",
    "Database.Importers.AutoImporter",
    "Database.database",        #< the direction that always worked - the control
)

IMPORT_TIMEOUT_SECONDS = 120   #< a cold import of this package tree, with slack

# The interpreters below are independent, so they run concurrently rather than
# one after another: serially this was the second-slowest test in the suite
# (~17s of the ~41s total), and xdist cannot help because parallelism there is
# ACROSS tests, not within one. Capped rather than unbounded - each child
# imports the whole package tree, and the suite is usually already running
# under `-n auto`, so this should not try to own the machine.
IMPORT_WORKERS = min(8, len(FIRST_IMPORTS))


def _importFirstInFreshInterpreter(moduleName):
    """Import `moduleName` into a brand-new interpreter; returns its result
    for the assertions to read, so failures are still reported per module."""
    return moduleName, subprocess.run(
        [sys.executable, "-c", f"import {moduleName}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
        timeout=IMPORT_TIMEOUT_SECONDS,
        #< the app warns about an unset TZ on import; keep the check
        #  about importability, not about the environment
        env={**os.environ, "TZ": "UTC"},
    )


class TestModulesImportInAnyOrder(unittest.TestCase):
    def test_each_module_imports_first_in_a_fresh_interpreter(self):
        with ThreadPoolExecutor(max_workers=IMPORT_WORKERS) as pool:
            #< map re-raises a child's TimeoutExpired here, as the serial loop did
            results = list(pool.map(_importFirstInFreshInterpreter, FIRST_IMPORTS))

        for moduleName, result in results:
            with self.subTest(module=moduleName):
                self.assertEqual(
                    result.returncode, 0,
                    f"importing {moduleName} first failed:\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
