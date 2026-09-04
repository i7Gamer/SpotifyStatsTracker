"""Hardening for the session/credential surface (2026-07-24 review, items 3-5):

- The stored Spotify client *secret* must never be echoed back into the profile
  HTML (view-source / extension / autofill capture).
- /profile/disconnect and /logout are state-changing and must be POST-only so a
  cross-site GET navigation can't trigger them (they carry the session cookie).
- The session cookie must be SameSite=Lax always and Secure when a TLS proxy
  fronts the app (same signal as HSTS).
- Booting with the docker-compose placeholder FLASK_SECRET_KEY must be refused -
  a public signing/encryption key is a trivial auth bypass.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
from app import (SpotifyDashboardApp, PLACEHOLDER_FLASK_SECRET_KEY,
                 SECRETS_DIR_NAME, FLASK_SECRET_KEY_FILENAME)
import Database.secret_store as secretStore
from Database.secret_store import FLASK_SECRET_KEY_ENV_VAR, KEY_FILE_MODE
from _app_factory import makeApp


class _LoggedInProfileTestCase(unittest.TestCase):
    """Feature-enabled profile scaffolding (SPOTIFY_CALLBACK_URL set, CSRF off)."""

    def _makeApp(self):
        app_inst = makeApp()
        app_inst.app.config["WTF_CSRF_ENABLED"] = False
        return app_inst

    def _login(self, dash, client):
        with client.session_transaction() as sess:
            sess["email"] = "alice@example.com"
            sess["username"] = "alice"
        dash.is_user_logged_in = MagicMock(return_value=True)
        dash.get_username_for_email = MagicMock(return_value="alice")

    def _mockDb(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.getUserSpotifyCredentials.return_value = {
            "client_id": "PUBLIC-CLIENT-ID",
            "client_secret": "SUPER-SECRET-VALUE-XYZ",
            "refresh_token": "rt",
        }
        mock_get_db.return_value = mock_db
        return mock_db


@patch.dict(os.environ, {"SPOTIFY_CALLBACK_URL": "http://localhost:5000/spotify-callback"})
class TestClientSecretNotEchoed(_LoggedInProfileTestCase):
    def test_secret_value_absent_from_profile_html(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        self._login(dash, client)
        with patch.object(dash, "get_user_db") as mock_get_db:
            self._mockDb(mock_get_db)
            resp = client.get("/profile/connections")

        self.assertEqual(resp.status_code, 200)
        # The secret must not round-trip into the page in any form.
        self.assertNotIn(b"SUPER-SECRET-VALUE-XYZ", resp.data)
        # The (non-secret) client id is still shown so the field isn't blank.
        self.assertIn(b"PUBLIC-CLIENT-ID", resp.data)

    def test_saved_secret_is_signalled_via_placeholder(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        self._login(dash, client)
        with patch.object(dash, "get_user_db") as mock_get_db:
            self._mockDb(mock_get_db)
            resp = client.get("/profile/connections")

        self.assertIn(b"A secret is saved", resp.data)


@patch.dict(os.environ, {"SPOTIFY_CALLBACK_URL": "http://localhost:5000/spotify-callback"})
class TestDisconnectIsPostOnly(_LoggedInProfileTestCase):
    def test_get_is_method_not_allowed(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        self._login(dash, client)
        with patch.object(dash, "get_user_db") as mock_get_db:
            self._mockDb(mock_get_db)
            resp = client.get("/profile/disconnect")
        self.assertEqual(resp.status_code, 405)

    def test_post_disconnects_credentials(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        self._login(dash, client)
        with patch.object(dash, "get_user_db") as mock_get_db:
            mock_db = self._mockDb(mock_get_db)
            resp = client.post("/profile/disconnect")
        self.assertEqual(resp.status_code, 302)
        mock_db.updateUserSpotifyCredentials.assert_called_once_with(None, None, None)

    def test_the_disconnect_form_asks_before_it_submits(self):
        """Disconnecting discards the stored client secret and refresh token,
        and reconnecting means typing the secret in again - one of the two
        unconfirmed actions the 2026-09-02 review (UI-06) gave a confirm, in
        the inline onsubmit shape the split and merge forms already use."""
        import bs4
        dash = self._makeApp()
        client = dash.app.test_client()
        self._login(dash, client)
        with patch.object(dash, "get_user_db") as mock_get_db:
            self._mockDb(mock_get_db)
            html = client.get("/profile/connections").get_data(as_text=True)

        form = bs4.BeautifulSoup(html, "html.parser").select_one("#disconnectApiForm")

        self.assertIsNotNone(form)
        self.assertTrue(form.get("onsubmit", "").startswith("return confirm("), form.get("onsubmit"))


class TestLogoutIsPostOnly(unittest.TestCase):
    def _makeApp(self):
        app_inst = makeApp()
        app_inst.app.config["WTF_CSRF_ENABLED"] = False
        return app_inst

    def test_get_is_method_not_allowed(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        resp = client.get("/logout")
        self.assertEqual(resp.status_code, 405)

    def test_post_clears_session_and_redirects_to_login(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        with client.session_transaction() as sess:
            sess["email"] = "alice@example.com"
            sess["username"] = "alice"
        resp = client.post("/logout")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        with client.session_transaction() as sess:
            self.assertNotIn("email", sess)
            self.assertNotIn("username", sess)


class TestSessionCookieFlags(unittest.TestCase):
    def test_samesite_lax_and_httponly_always(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(appModule.ENABLE_HSTS_ENV_VAR, None)
            dash = makeApp()
        self.assertEqual(dash.app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertTrue(dash.app.config["SESSION_COOKIE_HTTPONLY"])

    def test_secure_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(appModule.ENABLE_HSTS_ENV_VAR, None)
            dash = makeApp()
        self.assertFalse(dash.app.config["SESSION_COOKIE_SECURE"])

    def test_secure_on_when_tls_signalled(self):
        with patch.dict(os.environ, {appModule.ENABLE_HSTS_ENV_VAR: "1"}):
            dash = makeApp()
        self.assertTrue(dash.app.config["SESSION_COOKIE_SECURE"])


class TestPlaceholderSecretKeyRefused(unittest.TestCase):
    """_get_or_create_secret_key only reaches self.baseDir when the env var is
    unset, so a SimpleNamespace stand-in is enough to exercise the guard."""

    def _call(self, envValue):
        fakeSelf = SimpleNamespace(baseDir=Path("/nonexistent"))
        env = {} if envValue is None else {"FLASK_SECRET_KEY": envValue}
        with patch.dict(os.environ, env, clear=False):
            if envValue is None:
                os.environ.pop("FLASK_SECRET_KEY", None)
            return SpotifyDashboardApp._get_or_create_secret_key(fakeSelf)

    def test_exact_placeholder_raises(self):
        with self.assertRaises(RuntimeError):
            self._call(PLACEHOLDER_FLASK_SECRET_KEY)

    def test_placeholder_with_surrounding_whitespace_raises(self):
        with self.assertRaises(RuntimeError):
            self._call(f"  {PLACEHOLDER_FLASK_SECRET_KEY}  ")

    def test_real_key_is_returned(self):
        self.assertEqual(self._call("a-real-random-value"), "a-real-random-value")


class TestPlaceholderDataEncryptionKeyRefusesConstruction(unittest.TestCase):
    """F-B-1 (2026-09-04 review): _keyMaterial's placeholder/empty-file guards
    (Database/secret_store.py) used to be reached only lazily - the sole
    boot-time caller was _logIntegrityProbe's countSecretsUnderAnotherKey
    probe, itself wrapped in `except Exception: logger.debug(...)` - so the
    app booted clean, health-checked green, and 500'd on every login instead
    of refusing to start. SpotifyDashboardApp.__init__ now calls
    keyFingerprint() right after _get_or_create_secret_key(), outside any
    try/except, so this refuses exactly like TestPlaceholderSecretKeyRefused's
    Flask-key guard above.

    Deliberately NOT _app_factory.makeApp(): it patches Path.exists to always
    return False for the duration of construction, which would make an
    EXISTING (possibly empty) key file indistinguishable from a missing one -
    exactly the distinction these tests exercise. _get_or_create_secret_key and
    migrateIfNeeded are patched here instead, the same two _app_factory always
    patches, so this never touches the real secrets/flask key file or a real
    database."""

    def _construct(self):
        with patch("app.SpotifyDashboardApp._get_or_create_secret_key", return_value="test-secret-key"), \
             patch("app.migrateIfNeeded"):
            return SpotifyDashboardApp()

    def test_the_shipped_placeholder_refuses_construction(self):
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR:
                                      secretStore.PLACEHOLDER_DATA_ENCRYPTION_KEY}):
            with self.assertRaises(RuntimeError) as caught:
                self._construct()
        # The message names the env var and says "placeholder" - not the
        # placeholder string itself, same as the real RuntimeError text.
        self.assertIn(secretStore.ENCRYPTION_KEY_ENV_VAR, str(caught.exception))
        self.assertIn("placeholder", str(caught.exception))

    def test_an_empty_key_file_refuses_construction(self):
        # conftest's _isolateEncryptionKey redirects DEFAULT_KEY_PATH at a
        # per-test tmp path and delenvs both key env vars, so writing an empty
        # file there (rather than the real secrets/data_encryption_key.txt)
        # reproduces "a database restored without its matching key file".
        secretStore.DEFAULT_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        secretStore.DEFAULT_KEY_PATH.write_text("", encoding="utf-8")

        with self.assertRaises(RuntimeError) as caught:
            self._construct()
        self.assertIn("Restore it from a backup", str(caught.exception))

    def test_normal_construction_still_works_and_mints_the_key_file(self):
        """The accepted regression risk: with neither env var set and no key
        file yet, the data-encryption key is now minted at construction
        instead of at first encrypt/decrypt - same file, earlier."""
        self.assertFalse(secretStore.DEFAULT_KEY_PATH.exists())

        makeApp()

        self.assertTrue(secretStore.DEFAULT_KEY_PATH.exists())
        self.assertTrue(secretStore.DEFAULT_KEY_PATH.read_text(encoding="utf-8").strip())


class TestFlaskSecretKeyFile(unittest.TestCase):
    """With no FLASK_SECRET_KEY set, the signing key is persisted under
    secrets/ so sessions survive a restart. It is the recoverable twin of
    secrets/data_encryption_key.txt - losing it logs everyone out, it does not
    strand stored Spotify sessions - and goes through the same hardened
    readOrCreateKeyFile, so both are written owner-only and atomically."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.baseDir = Path(self._tmpdir.name)
        self.keyFile = self.baseDir / SECRETS_DIR_NAME / FLASK_SECRET_KEY_FILENAME
        envPatcher = patch.dict(os.environ, {}, clear=False)
        envPatcher.start()
        self.addCleanup(envPatcher.stop)
        os.environ.pop(FLASK_SECRET_KEY_ENV_VAR, None)

    def _call(self):
        return SpotifyDashboardApp._get_or_create_secret_key(SimpleNamespace(baseDir=self.baseDir))

    def test_a_key_is_minted_and_persisted_on_first_start(self):
        minted = self._call()

        self.assertTrue(minted)
        self.assertEqual(self.keyFile.read_text(encoding="utf-8").strip(), minted)

    def test_the_persisted_key_is_reused_on_the_next_start(self):
        """A key that changed every boot would log everyone out on every
        restart - the whole reason it is written down."""
        self.assertEqual(self._call(), self._call())

    def test_an_empty_file_is_reminted_rather_than_fatal(self):
        """Unlike the data encryption key, which refuses: a lost signing key
        costs everyone a re-login, not their stored Spotify sessions."""
        self.keyFile.parent.mkdir(parents=True)
        self.keyFile.write_text("", encoding="utf-8")

        minted = self._call()

        self.assertTrue(minted)
        self.assertEqual(self.keyFile.read_text(encoding="utf-8").strip(), minted)

    def test_nothing_is_left_behind(self):
        self._call()

        leftovers = [p.name for p in self.keyFile.parent.iterdir() if p != self.keyFile]
        self.assertEqual(leftovers, [], f"temporary artefacts left behind: {leftovers}")

    @unittest.skipIf(os.name == "nt", "os.chmod on Windows never narrows the ACL")
    def test_the_key_file_is_owner_only(self):
        self._call()

        self.assertEqual(self.keyFile.stat().st_mode & 0o777, KEY_FILE_MODE)


class TestDevRunDisablesTheInteractiveDebugger(unittest.TestCase):
    """F-B-2 (2026-09-04 review, LOW): Flask's own app.run() does
    `options.setdefault("use_debugger", self.debug)`, so
    self.app.run(debug=debug, ...) - with no use_debugger of its own - also
    flips on the interactive Werkzeug debugger whenever FLASK_DEBUG=1.
    README/docker-compose document FLASK_DEBUG as a logging knob ("enable
    when reporting an issue"), not as "expose a code-execution console to
    the LAN" - which is what an unhandled exception under `python app.py`
    would do, guarded only by a PIN whose MAC+machine-id derivation is a
    well-known target. use_debugger=False keeps debug= driving
    flaskDebugEnabled()'s shared logging gate while pinning the debugger
    off; the shipped path (wsgi.py/waitress, the live start.bat) never
    reaches app.run() at all, so this only protects the README's
    "Local Development" entry point.

    Every worker-start seam is stubbed the way
    test_app_worker_lifecycle.py's _appWithStubbedWorkers does, since
    run() calls startWorkers() before app.run() - this test cares only
    about the kwargs app.run() is called with, never about a real
    server or real background threads."""

    def _runWithStubs(self, env):
        with patch("app.SpotifyDashboardApp._get_or_create_secret_key", return_value="test-secret-key"), \
             patch("app.migrateIfNeeded"), \
             patch("app.Path.exists", return_value=False), \
             patch("app.BackupWorker.start"), \
             patch("app.EMAIL_WORKER"), \
             patch("app.SpotifyDashboardApp.startVersionCheck_thread"), \
             patch("app.SpotifyDashboardApp.checkLogin_thread"), \
             patch.dict(os.environ, env):
            dashboard = SpotifyDashboardApp()
            with patch.object(dashboard.app, "run") as mockRun:
                dashboard.run()
        return mockRun

    def test_flask_debug_enabled_does_not_enable_the_interactive_debugger(self):
        mockRun = self._runWithStubs({"FLASK_DEBUG": "1"})

        mockRun.assert_called_once()
        kwargs = mockRun.call_args.kwargs
        self.assertTrue(kwargs["debug"])          #< still drives the shared logging gate
        self.assertFalse(kwargs["use_debugger"])  #< the Werkzeug console must stay off

    def test_flask_debug_disabled_leaves_the_debugger_off_too(self):
        mockRun = self._runWithStubs({"FLASK_DEBUG": "0"})

        kwargs = mockRun.call_args.kwargs
        self.assertFalse(kwargs["debug"])
        self.assertFalse(kwargs["use_debugger"])


if __name__ == "__main__":
    unittest.main()
