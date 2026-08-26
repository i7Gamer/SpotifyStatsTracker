"""An expired session used to dead-end the AJAX shell pages: the route answered
a 302 to /login, fetch() followed it transparently, and the loader tried to
parse the login page's HTML as JSON - so the user saw a generic "couldn't load"
with a Retry that failed identically forever, and never learned they were
logged out.

The routes now answer an ajax request with a 401 (the client turns it into a
real navigation - see AjaxStatus.redirectIfUnauthorized); non-ajax requests keep
redirecting as before.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase

# The pages whose content is loaded/refreshed by a fetch in static/js.
#
# Only detail-chart.js still does that, but the ?ajax= branch stays covered for
# every page: it is one branch in unauthenticatedResponse, and a page that grows
# a fetch tomorrow inherits it silently.
AJAX_PATHS = ("/", "/charts", "/genres", "/history", "/top-songs", "/top-artists", "/top-albums",
              #< wrapped.js has called redirectIfUnauthorized since the feature
              #  landed, but the route still answered a 302, which fetch follows
              #  transparently - so the check never saw a 401 to act on
              "/wrapped")

# Every page htmx drives. The same failure this module exists for, reachable
# again through a different client: htmx follows a 302 exactly as transparently
# as fetch() did, so an expired session mid-swap would put the login page's HTML
# inside whatever region was being refreshed.
#
# Parametrized here rather than repeated per page. Each test_*_htmx.py grew its
# own TestUnauthenticatedSwap saying this - eight copies of one app-wide rule
# that lives in ONE place (app.py's unauthenticatedResponse), and only half of
# them checked the empty body and the preserved filters. Now every path gets
# every assertion.
#
# /shared/<token> is deliberately absent: it is not behind @requiresUser, so its
# failures are a dead token (404) and the miss limiter (429), neither of which is
# a session problem. Sending an anonymous visitor to a login screen for an
# account that isn't theirs would be wrong - see test_wrapped_htmx.py's
# TestSharedWrappedHtmx.
HTMX_PATHS = AJAX_PATHS + ("/compare", "/song/t1", "/artist/a1", "/album/alb1")

#< what htmx puts on every request it makes
HX_HEADERS = {"HX-Request": "true"}


class UnauthenticatedAjaxTestCase(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()
        self.client = self.dash.app.test_client()
        # No session at all: every request below is unauthenticated.
        self.patcher = patch.object(self.dash, "is_user_logged_in", return_value=False)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)


class TestAjaxRequestsGet401(UnauthenticatedAjaxTestCase):
    def test_every_ajax_page_answers_401_json(self):
        for path in AJAX_PATHS:
            with self.subTest(path=path):
                resp = self.client.get(f"{path}?ajax=true")

                self.assertEqual(resp.status_code, 401)
                self.assertEqual(resp.mimetype, "application/json")

    def test_the_401_carries_a_login_url_pointing_back(self):
        resp = self.client.get("/charts?ajax=true&interval=week")

        payload = resp.get_json()
        self.assertIn("/login", payload["loginUrl"])
        self.assertIn("charts", payload["loginUrl"])

    def test_the_login_url_keeps_the_pages_filters(self):
        """The query string IS the page state - interval, custom dates, sortBy,
        tag, page. Built from request.path it was dropped, so logging back in
        landed the user on an unfiltered first page instead of where they were."""
        resp = self.client.get("/top-songs?ajax=true&interval=year&sortBy=skips&page=4")

        loginUrl = resp.get_json()["loginUrl"]

        for expected in ("interval%3Dyear", "sortBy%3Dskips", "page%3D4"):
            with self.subTest(param=expected):
                self.assertIn(expected, loginUrl)

    def test_a_page_with_no_query_string_does_not_gain_a_stray_marker(self):
        """request.full_path always appends "?" even with no args, and that would
        travel through the login redirect into the address bar."""
        resp = self.client.get("/?ajax=true")

        #< ajax=true itself is part of the state that got us here, but the bare
        #  path must not come back as "/?"
        self.assertNotIn("next=%2F%3F&", resp.get_json()["loginUrl"] + "&")

    def test_a_non_true_ajax_value_still_counts_as_ajax(self):
        """The marker is the presence of ?ajax=, not its value: the branch has
        to keep answering a spelling no page uses today, because the page that
        introduces one tomorrow would otherwise get the 302 silently."""
        resp = self.client.get("/song/t1?ajax=list")

        self.assertEqual(resp.status_code, 401)


class TestHtmxRequestsGetHxRedirect(UnauthenticatedAjaxTestCase):
    """The htmx half of the same contract. htmx understands HX-Redirect as "go
    here instead"; a 302 it would simply follow, and swap the result in."""

    def test_no_htmx_page_answers_a_swap_with_a_redirect(self):
        for path in HTMX_PATHS:
            with self.subTest(path=path):
                resp = self.client.get(path, headers=HX_HEADERS)

                self.assertNotIn(resp.status_code, (301, 302, 303, 307, 308))
                self.assertIn("/login", resp.headers.get("HX-Redirect", ""))

    def test_the_body_is_empty_so_nothing_is_swapped_in(self):
        """htmx swaps the body of any 2xx before the redirect happens, so
        anything here would be injected into the page on the way out."""
        for path in HTMX_PATHS:
            with self.subTest(path=path):
                resp = self.client.get(path, headers=HX_HEADERS)

                self.assertEqual(resp.get_data(as_text=True), "")

    def test_the_hx_redirect_keeps_the_pages_filters(self):
        """Same reasoning as the 401's loginUrl above - the query string IS the
        page state, and dropping it lands the user somewhere else after logging
        back in."""
        resp = self.client.get("/top-songs?interval=year&sortBy=skips&page=4", headers=HX_HEADERS)

        target = resp.headers.get("HX-Redirect", "")
        for expected in ("interval%3Dyear", "sortBy%3Dskips", "page%3D4"):
            with self.subTest(param=expected):
                self.assertIn(expected, target)

    def test_an_htmx_request_wins_over_a_stray_ajax_marker(self):
        """A crafted ?ajax= on an htmx request must not get the JSON 401 the
        swap cannot act on - the header is the stronger signal, and the branch
        order in unauthenticatedResponse is what makes that true."""
        resp = self.client.get("/history?ajax=true", headers=HX_HEADERS)

        self.assertIn("/login", resp.headers.get("HX-Redirect", ""))
        self.assertNotEqual(resp.mimetype, "application/json")


class TestXhrHeaderRequestsGet401(UnauthenticatedAjaxTestCase):
    """The third client, and the one the ?ajax= branch was blind to.

    The admin console's Create-backup and Refresh-Last.fm forms POST via fetch
    and declare it in the header rather than the query string, because the
    ROUTE reads the header to decide JSON-or-redirect. The guard in front of it
    read only ?ajax=, so an expired session answered them with a 302, fetch
    followed it, resp.json() choked on the login page, and the admin was told
    "Backup failed - try again" for a session problem.

    A caller that declared itself an XHR cannot parse a login page, whichever
    way it declared it.
    """

    XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest"}

    def test_an_xhr_post_to_an_admin_route_answers_401_json(self):
        resp = self.client.post("/admin/create_backup", headers=self.XHR_HEADERS)

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.mimetype, "application/json")

    def test_the_admin_401_points_login_back_at_the_admin_page(self):
        """The admin surface's convention, unchanged: whatever sub-endpoint was
        POSTed, land back on /admin - not on the POST-only endpoint itself."""
        resp = self.client.post("/admin/create_backup", headers=self.XHR_HEADERS)

        self.assertIn("next=/admin", resp.get_json()["loginUrl"])

    def test_an_xhr_post_to_the_lastfm_refresh_answers_401_json(self):
        """Its own guard, because its login redirect targets the detail page
        the button lives on - which must not cost it the XHR contract."""
        resp = self.client.post("/admin/lastfm/refresh/artist/a1", headers=self.XHR_HEADERS)

        self.assertEqual(resp.status_code, 401)
        self.assertIn("a1", resp.get_json()["loginUrl"])

    def test_a_plain_post_to_an_admin_route_still_redirects(self):
        """The no-JS fallback: a form POSTed without fetch still gets the 302."""
        resp = self.client.post("/admin/create_backup")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_a_wrong_xhr_header_value_is_not_treated_as_ajax(self):
        """Matched exactly, the same way the routes match it - a browser that
        sends some other value has not declared itself a JSON caller."""
        resp = self.client.post("/admin/create_backup",
                                headers={"X-Requested-With": "fetch"})

        self.assertEqual(resp.status_code, 302)


class TestJsonApisAnswerOneUnauthorizedShape(UnauthenticatedAjaxTestCase):
    """One body for "you are not logged in", across every JSON endpoint.

    @requiresUser(api=True) answers {"error": "Not logged in"}. Seven endpoints
    predate it and hand-rolled the same guard with a different word -
    {"error": "unauthorized"} - so a client checking the message had to know
    which half of the API it was talking to. Worse than the inconsistency: a
    hand-rolled guard is the forgettable kind routes/_auth.py exists to abolish,
    and these are the last copies of it.
    """

    JSON_APIS = (
        ("GET", "/import-progress"),
        ("GET", "/api/tags"),
        ("POST", "/api/tags"),
        ("DELETE", "/api/tags"),
        ("POST", "/api/tags/rename"),
        ("DELETE", "/api/tags/rock"),
        ("GET", "/api/playlists/preview"),
        ("GET", "/api/listener-status"),
        ("GET", "/api/now-playing"),
    )

    def test_every_json_api_answers_the_same_401_body(self):
        for method, path in self.JSON_APIS:
            with self.subTest(method=method, path=path):
                resp = self.client.open(path, method=method)

                self.assertEqual(resp.status_code, 401)
                self.assertEqual(resp.get_json()["error"], "Not logged in")

    def test_the_guard_runs_before_the_feature_toggle(self):
        """The tag endpoints 404 when the tagging feature is off. An anonymous
        caller must still be told it is a session problem, or turning the
        feature off would silently change what "not logged in" looks like."""
        with patch.object(self.dash.repo, "isTagsEnabled", return_value=False):
            resp = self.client.get("/api/tags")

        self.assertEqual(resp.status_code, 401)

    def test_the_tag_pages_still_redirect_rather_than_answering_json(self):
        """/playlists is a page and /playlist/export is a download - neither is
        fetched, so both keep the login redirect."""
        for path in ("/playlists", "/playlist/export"):
            with self.subTest(path=path):
                resp = self.client.get(path)

                self.assertEqual(resp.status_code, 302)
                self.assertIn("/login", resp.headers["Location"])


class TestNormalRequestsStillRedirect(UnauthenticatedAjaxTestCase):
    def test_page_loads_keep_redirecting_to_login(self):
        for path in AJAX_PATHS:
            with self.subTest(path=path):
                resp = self.client.get(path)

                self.assertEqual(resp.status_code, 302)
                self.assertIn("/login", resp.headers["Location"])

    def test_the_redirect_preserves_where_the_user_was_going(self):
        resp = self.client.get("/charts")

        self.assertIn("next=", resp.headers["Location"])
        self.assertIn("charts", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
