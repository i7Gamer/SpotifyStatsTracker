"""routes/* must not import the app module.

app.py imports every routes/* module at import time, and each route module used
to `import app as appmod` back - purely to reach constants that app.py itself
had done `from config import *` to get. That cycle is why app.py needs its
`sys.modules.setdefault("app", ...)` guard at line 30: running `py app.py`
registers the module as "__main__", so a route's `import app` found nothing in
sys.modules and re-executed the whole file, re-entering routes.charts mid-import
and raising ImportError.

The constants belong to config, so routes read them from config. Route modules
already receive everything app-specific through `register(app, dashboard)`, so
there is no remaining reason for the back-edge - and this test keeps it from
growing back.
"""
import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_ROUTES_DIR = Path(__file__).resolve().parent.parent / "routes"


def _importedModules(path: Path) -> set[str]:
    """Every top-level module name `path` imports, from both import forms."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


class TestRouteModulesDoNotImportApp(unittest.TestCase):
    def test_routes_dir_is_found(self):
        """Guards the rest of this file: an empty glob would pass vacuously."""
        self.assertGreater(len(list(_ROUTES_DIR.glob("*.py"))), 1)

    def test_no_route_module_imports_app(self):
        offenders = sorted(
            path.name for path in _ROUTES_DIR.glob("*.py")
            if "app" in _importedModules(path)
        )

        self.assertEqual(offenders, [],
                         f"routes/* must reach constants via config, not app: {offenders}")


class TestAppModuleIsNotSelfRegistered(unittest.TestCase):
    def test_app_does_not_alias_itself_into_sys_modules(self):
        """The `sys.modules.setdefault("app", ...)` workaround only existed to
        survive the routes->app back-edge; with that gone it must go too, or a
        future cycle is masked instead of failing loudly."""
        source = (_ROUTES_DIR.parent / "app.py").read_text(encoding="utf-8")

        self.assertNotIn("sys.modules.setdefault", source)


if __name__ == "__main__":
    unittest.main()
