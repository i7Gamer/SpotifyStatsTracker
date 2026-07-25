"""Client helper for the song/artist/album detail pages' two-phase load.

Those routes serve a shell on the plain GET and everything below the toolbar
from a second ``?ajax=page`` request (see routes/charts.py's DETAIL_BODY_AJAX
and static/js/detail-page.js). A test asserting on the page's markup wants what
the browser ends up showing, so ``_getPath`` performs both requests and returns
the shell response with the deferred body appended to its data: substring and
ordering assertions then read the same sequence the visitor gets, and the
response's own status and headers stay the shell's.

The mock db is reset between the two, so call assertions describe the request
that did the work - which, for everything the split moved, is the second one.
A path that already carries an ``ajax=`` parameter, and any non-200 (an unknown
entity redirects), is passed straight through: that is one request by
definition.

Tests about the split itself - what the shell does and doesn't render, what the
payload carries - call ``_getRaw`` and drive the two requests themselves.

Three test files carried byte-identical copies of the login-patching client
this replaces. Imported by bare module name (``from _detail_client import
DetailPageClientMixin``) like the suite's other shared helpers, since tests/ is
on sys.path with no package __init__.
"""
from unittest.mock import patch

from routes.charts import DETAIL_BODY_AJAX


class DetailPageClientMixin:
    """``_getRaw`` (one request) and ``_getPath`` (the load as a browser does
    it) for a test case that drives a detail route with a MagicMock db."""

    def _getRaw(self, dash, db, path):
        """One request, as a logged-in user.

        Every detail route unconditionally fetches a page of the item's play
        history (see _detailHistoryContext), so those three lookups default to
        "no history" and a test that cares stubs them itself beforehand.
        """
        if not isinstance(db.getEntriesCount.return_value, int):
            db.getEntriesCount.return_value = 0
        if not isinstance(db.getEntriesFromNew.return_value, list):
            db.getEntriesFromNew.return_value = []
        if not isinstance(db.getEntriesFromOld.return_value, list):
            db.getEntriesFromOld.return_value = []
        client = dash.app.test_client()
        with patch.object(dash, "is_user_logged_in", return_value=True), \
             patch.object(dash, "get_username_for_email", return_value="alice"), \
             patch.object(dash, "get_user_db", return_value=db):
            with client.session_transaction() as sess:
                sess["email"] = "alice@example.com"
            return client.get(path)

    def _getPath(self, dash, db, path):
        """The shell plus its deferred body, as one response to assert on."""
        shell = self._getRaw(dash, db, path)
        if "ajax=" in path or shell.status_code != 200:
            return shell
        db.reset_mock()
        separator = "&" if "?" in path else "?"
        body = self._getRaw(dash, db, f"{path}{separator}ajax={DETAIL_BODY_AJAX}")
        # Appended rather than spliced into #detailBody: the assertions this
        # serves are substring and relative-order ones, which the concatenation
        # answers identically, and splicing would need a marker in the shell's
        # markup that exists only for the tests.
        shell.set_data(shell.get_data() + body.get_json()["bodyHtml"].encode())
        return shell
