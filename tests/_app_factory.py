"""Shared factory for a stubbed SpotifyDashboardApp test instance.

~40 test classes each repeated the same 5-decorator ``_makeApp`` that builds a
SpotifyDashboardApp with migrations, the version/login background threads and
secret-key persistence patched out. This centralizes that setup (the background
workers no longer need patching at all - see SpotifyDashboardApp.startWorkers):

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

    Migrations and secret-key persistence are patched out for the duration of
    construction (they still run in ``__init__``); the background workers need
    no patching, since ``startWorkers()`` - which this deliberately never calls
    - is the only thing that starts them. The returned instance is otherwise
    real, with an in-memory/temp Repository (see conftest's DB isolation)."""
    with patch(_SECRET_KEY_PATCH, return_value="test-secret-key"), \
         patch("app.migrateIfNeeded"), \
         patch("app.Path.exists", return_value=False):
        return SpotifyDashboardApp()


class AppTestCase(unittest.TestCase):
    """Base for tests that build a stubbed app via ``self._makeApp()``."""

    def _makeApp(self):
        """The app, with ``shutdown()`` registered as a cleanup.

        Construction starts nothing, but activating a user does:
        ``get_user_db()`` gives that user a live Database, i.e. the five
        periodic workers plus the auto-import watchdog, on real daemon
        threads. Unstopped they outlive the test - waking after their
        randomized startup delay to log into a closed capture stream and poll
        for the rest of the session (see conftest._noLeakedUserThreads).
        Registered here rather than left to each test's tearDown because it is
        needed by every test that activates a user, and harmless (a fast no-op)
        for every test that doesn't."""
        app = makeApp()
        self.addCleanup(app.shutdown)
        return app
