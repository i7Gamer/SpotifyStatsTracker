"""Shared factory for a stubbed SpotifyDashboardApp test instance.

~40 test classes each repeated the same 5-decorator ``_makeApp`` that builds a
SpotifyDashboardApp with migrations, the version/login background threads and
secret-key persistence patched out. This centralizes that setup:

- subclass :class:`AppTestCase` to keep calling ``self._makeApp()`` unchanged, or
- call :func:`makeApp` directly (module-level tests, or when extra setup is needed).

Imported by bare module name (``from _app_factory import ...``) like the suite's
other shared helpers, since tests/ is on sys.path with no package __init__.
"""
import unittest
from unittest.mock import patch

from app import SpotifyDashboardApp

# The instance method that persists/reads the Flask session-signing key; stubbed
# so construction never touches secrets/ on disk.
_SECRET_KEY_PATCH = "app.SpotifyDashboardApp._get_or_create_secret_key"


def makeApp():
    """A ready-to-use SpotifyDashboardApp with no filesystem, network or threads.

    Migrations, the periodic version-check and login-check background threads,
    and secret-key persistence are all patched out for the duration of
    construction (they run in ``__init__``); the returned instance is otherwise
    real, with an in-memory/temp Repository (see conftest's DB isolation)."""
    with patch(_SECRET_KEY_PATCH, return_value="test-secret-key"), \
         patch("app.SpotifyDashboardApp.startVersionCheck_thread"), \
         patch("app.SpotifyDashboardApp.checkLogin_thread"), \
         patch("app.migrateIfNeeded"), \
         patch("app.Path.exists", return_value=False):
        return SpotifyDashboardApp()


class AppTestCase(unittest.TestCase):
    """Base for tests that build a stubbed app via ``self._makeApp()``."""

    def _makeApp(self):
        return makeApp()
