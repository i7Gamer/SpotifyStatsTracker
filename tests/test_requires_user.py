"""The @requiresUser decorator (routes/_auth.py).

It replaced a three-line preamble that every page repeated. That preamble is a
guard you can forget, and forgetting it doesn't fail loudly - it serves a
logged-in page shape to an anonymous visitor - so the decorator's own contract
is worth pinning rather than only testing through the routes that use it.

Two flavours, because the routes have two real contracts: a page redirects to
login (unless it's an ?ajax= fetch, which cannot follow a redirect usefully),
and a JSON-only endpoint always answers 401 JSON.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from routes._auth import makeRequiresUser


class TestRequiresUser(AppTestCase):
    def setUp(self):
        super().setUp()
        self.dash = self._makeApp()
        self.requiresUser = makeRequiresUser(self.dash)
        self.db = MagicMock()

    def _loggedIn(self):
        return patch.object(self.dash, "get_current_user_or_redirect",
                            return_value=("alice@example.com", "alice", self.db))

    def _loggedOut(self):
        return patch.object(self.dash, "get_current_user_or_redirect",
                            return_value=(None, None, None))

    def test_it_hands_the_view_the_username_and_db_but_not_the_email(self):
        """The email is only ever used for the guard, so it stops here."""
        seen = {}

        @self.requiresUser
        def view(username, db):
            seen.update(username=username, db=db)
            return "ok"

        with self.dash.app.test_request_context("/x"), self._loggedIn():
            self.assertEqual(view(), "ok")

        self.assertEqual(seen["username"], "alice")
        self.assertIs(seen["db"], self.db)

    def test_url_parameters_ride_through(self):
        """Flask passes URL converters as keyword arguments, so a detail page's
        <track_id> has to survive the wrapper."""
        @self.requiresUser
        def view(username, db, track_id):
            return f"{username}:{track_id}"

        with self.dash.app.test_request_context("/song/t1"), self._loggedIn():
            self.assertEqual(view(track_id="t1"), "alice:t1")

    def test_a_page_never_runs_its_body_when_logged_out(self):
        ran = []

        @self.requiresUser
        def view(username, db):
            ran.append(True)
            return "leaked"

        with self.dash.app.test_request_context("/x"), self._loggedOut():
            response = view()

        self.assertEqual(ran, [])
        self.assertEqual(response.status_code, 302)   #< redirect to login

    def test_an_ajax_page_request_gets_401_json_not_a_redirect(self):
        """fetch() follows a redirect transparently, so the page would end up
        parsing the login HTML as JSON."""
        @self.requiresUser
        def view(username, db):
            return "never"

        with self.dash.app.test_request_context("/x?ajax=true"), self._loggedOut():
            response, status = view()

        self.assertEqual(status, 401)
        self.assertEqual(response.get_json()["error"], "Not logged in")

    def test_a_json_endpoint_answers_401_without_any_ajax_marker(self):
        """The JSON APIs are fetched without ?ajax=, so the page contract's
        redirect would hand them a login page to parse."""
        @self.requiresUser(api=True)
        def view(username, db):
            return "never"

        with self.dash.app.test_request_context("/api/thing"), self._loggedOut():
            response, status = view()

        self.assertEqual(status, 401)
        self.assertEqual(response.get_json()["error"], "Not logged in")

    def test_the_api_flavour_still_passes_the_user_through(self):
        @self.requiresUser(api=True)
        def view(username, db):
            return username

        with self.dash.app.test_request_context("/api/thing"), self._loggedIn():
            self.assertEqual(view(), "alice")

    def test_the_admin_flavour_passes_an_admin_through(self):
        @self.requiresUser(admin=True)
        def view(username, db):
            return username

        with self.dash.app.test_request_context("/admin"), self._loggedIn(), \
                patch.object(self.dash.repo, "isAdmin", return_value=True):
            self.assertEqual(view(), "alice")

    def test_the_admin_flavour_403s_a_non_admin(self):
        """The 13 hand-rolled copies of this guard in routes/admin.py are what
        the flavour replaces - the invariant the file was written to enforce
        (a view can't be registered without its guard) now covers admin too."""
        from werkzeug.exceptions import Forbidden

        ran = []

        @self.requiresUser(admin=True)
        def view(username, db):
            ran.append(True)   # pragma: no cover - the guard must stop us

        with self.dash.app.test_request_context("/admin"), self._loggedIn(), \
                patch.object(self.dash.repo, "isAdmin", return_value=False):
            with self.assertRaises(Forbidden):
                view()
        self.assertEqual(ran, [])

    def test_the_admin_flavour_redirects_anonymous_to_the_admin_next(self):
        """The admin surface's own convention: whatever sub-endpoint was hit,
        land back on /admin after logging in."""
        @self.requiresUser(admin=True)
        def view(username, db):
            return ""   # pragma: no cover

        with self.dash.app.test_request_context("/admin/skip_settings"), self._loggedOut():
            resp = view()

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        self.assertIn("next=/admin", resp.headers["Location"])

    def test_the_wrapped_view_keeps_its_name(self):
        """Flask registers endpoints by function name in places, and a wrapper
        that renamed every view to `wrapped` would collide."""
        @self.requiresUser
        def somePage(username, db):
            return ""

        @self.requiresUser(api=True)
        def someApi(username, db):
            return ""

        self.assertEqual(somePage.__name__, "somePage")
        self.assertEqual(someApi.__name__, "someApi")


class TestEveryChartsRouteIsGuarded(AppTestCase):
    """The point of the decorator: a view can't be registered without a guard.

    /overview is the deliberate exception - it is a public "is this instance
    alive" summary and gates only its own per-user widget."""

    PUBLIC = {"overviewPage"}

    def test_no_charts_route_answers_an_anonymous_request(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        paths = ["/", "/history", "/top-songs", "/top-albums", "/top-artists", "/charts",
                 "/api/dashboard-discover", "/api/dashboard-trends"]

        with patch.object(dash, "is_user_logged_in", return_value=False):
            for path in paths:
                with self.subTest(path=path):
                    self.assertIn(client.get(path).status_code, (301, 302, 401))


if __name__ == "__main__":
    unittest.main()
