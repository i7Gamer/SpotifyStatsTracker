"""Static assets must be requested under a URL that changes when the file
changes.

Nothing versioned the /static URLs, and Flask serves them with a bare
``no-cache`` (revalidate) - so a browser that had answered a revalidation once
could keep serving an old copy for the rest of its heuristic window. That is
survivable while each file stands alone, but commit f068bbf made nine page
loaders depend on a NEW function in the shared ajax-status.js: a browser
holding yesterday's ajax-status.js with today's dashboard-page.js threw
"AjaxStatus.readJsonOrThrow is not a function" on the dashboard, /history,
/charts, the top lists - every AJAX page at once, with only a hard reload as
the way out.

Appending the file's mtime makes the URL itself the version, so a changed file
is a cache MISS rather than a revalidation the browser may skip.
"""
import os
import unittest

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import url_for

from _app_factory import AppTestCase


class TestStaticCacheBusting(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()

    def _url(self, filename):
        with self.dash.app.test_request_context("/"):
            return url_for("static", filename=filename)

    def test_static_url_carries_a_version_stamp(self):
        url = self._url("js/ajax-status.js")

        self.assertIn("/static/js/ajax-status.js?", url)
        self.assertRegex(url, r"\?v=\d+$")

    def test_stamp_is_the_files_mtime_so_a_changed_file_changes_the_url(self):
        """The stamp must not be memoized per process: the point is that the
        URL moves when the file does."""
        path = os.path.join(self.dash.app.static_folder, "js", "ajax-status.js")
        original = os.stat(path)
        before = self._url("js/ajax-status.js")
        try:
            os.utime(path, (original.st_atime, original.st_mtime + 60))
            after = self._url("js/ajax-status.js")
        finally:
            os.utime(path, (original.st_atime, original.st_mtime))

        self.assertNotEqual(before, after)
        self.assertEqual(before, self._url("js/ajax-status.js"))

    def test_missing_static_file_still_builds_a_url(self):
        """url_for must not raise for a filename with no file behind it - a
        typo'd asset should 404 in the browser like it always did, not blow up
        the whole page render."""
        url = self._url("js/no-such-file.js")

        self.assertIn("/static/js/no-such-file.js", url)
        self.assertNotIn("?v=", url)

    def test_non_static_endpoints_are_untouched(self):
        with self.dash.app.test_request_context("/"):
            self.assertNotIn("v=", url_for("login"))

    def test_versioned_url_still_serves_the_file(self):
        client = self.dash.app.test_client()

        resp = client.get(self._url("js/ajax-status.js"))

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"readJsonOrThrow", resp.data)

    def test_rendered_page_links_versioned_assets(self):
        """The end-to-end point: what the browser is handed carries the stamp."""
        client = self.dash.app.test_client()

        html = client.get("/login").get_data(as_text=True)

        self.assertRegex(html, r'href="/static/css/style\.css\?v=\d+"')
        self.assertRegex(html, r'src="/static/js/ajax-status\.js\?v=\d+"')


if __name__ == "__main__":
    unittest.main()
