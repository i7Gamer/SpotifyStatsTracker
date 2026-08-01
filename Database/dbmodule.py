# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Late-bound access to Database.database's module globals.

The mixins Database is composed from (Database/workers/, import_service,
media_fetch) read names back off the module they are mixed into -
`_dbmod.logger`, `_dbmod.parseError`, `_dbmod.Database`, `_dbmod.OUTCOME_OK`
and friends. Reaching them through the module rather than importing them
directly is deliberate: it is what makes the suite's patch("Database.database.X")
targets apply to code living in these files.

Doing that with a plain `import Database.database as _dbmod` cost an import
CYCLE, though - database.py imports the mixins at line ~180, and each mixin
module imported database.py straight back. Python tolerated it in exactly one
direction: import Database.database first and the half-initialized module object
was harmless (nothing touches its attributes at import time), but import any
mixin module first and database.py ran while its own package was mid-import:

    ImportError: cannot import name 'WorkerLifecycleMixin' from partially
    initialized module 'Database.workers'

which is not hypothetical - it bit Database/backup.py, imported standalone by
the migrators before any of that exists.

This module is the same indirection with the import removed: attribute lookups
resolve against sys.modules when they RUN, by which point database.py has
finished. Patching still reaches them, because the lookup lands on the same
module object patch() mutates. tests/test_import_cycles.py pins that every one
of these modules imports first in a fresh interpreter.
"""
import importlib
import sys

_DATABASE_MODULE_NAME = "Database.database"


class _DatabaseModuleProxy:
    """Forwards attribute access to Database.database at call time.

    Reads sys.modules rather than holding a reference, so a reload (or an
    import still in flight) can't leave this pointing at a stale module."""

    __slots__ = ()

    def __getattr__(self, name):
        module = sys.modules.get(_DATABASE_MODULE_NAME)
        if module is None:
            module = importlib.import_module(_DATABASE_MODULE_NAME)
        return getattr(module, name)

    def __repr__(self):   #< so a traceback names it for what it is
        return f"<late-bound {_DATABASE_MODULE_NAME}>"


dbmod = _DatabaseModuleProxy()
