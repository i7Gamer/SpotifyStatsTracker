"""dashboard/context_processors.py owns every context processor.

They used to sit inside app.py's registerRoutes - 14 closures, 192 lines,
the middle 60% of a function that also installs the request hooks, a template
filter, the 413 handler and eight route modules. Moving them out is
navigational only: what each one reads, and how often, is a settled decision
(see the memo helpers' docstrings) and is pinned here unchanged.
"""
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import session as flaskSession

from _app_factory import AppTestCase
from config import SPOTIFY_CALLBACK_URL_ENV_VAR

APP_SOURCE = Path(__file__).resolve().parent.parent / "app.py"

#< the decorator (`.context_processor`), not the module app.py imports the
#  processors from (`.context_processors`) nor prose saying "context processors"
_CONTEXT_PROCESSOR_DECORATOR = re.compile(r"\.context_processor\b")

#< Flask's default processor and flask_wtf's CSRFProtect contribute these; not ours to pin
THIRD_PARTY_CONTEXT_KEYS = frozenset({"g", "request", "session", "csrf_token", "csrf_meta_tag"})

#< every name the app's processors put into template context
EXPECTED_CONTEXT_KEYS = frozenset({
    "minPasswordLength",
    "SYNTHETIC_FALLBACK_REASON", "RESTRICTED_FALLBACK_REASON",
    "MAX_INLINE_ARTISTS", "MIN_HIDDEN_ARTISTS",
    "isAdmin",
    "registration_enabled", "share_links_enabled", "artist_bio_enabled",
    "album_bio_enabled", "lastfm_genre_enabled", "tags_enabled",
    "hide_tags_panel",
    "hasAcceptedShares", "pendingIncomingSharesCount", "unseenAcceptedShareCount",
    "spotifyNeedsReauthBadge",
    "unseenMilestoneCount", "milestones_enabled",
})

#< the per-user reads that were hand-rolled `if "x" not in g` memos
PER_USER_READS = ("isAdmin", "getHideTagsPanel", "getSpotifyNeedsReauth")

RENDERS_PER_REQUEST = 2   #< two simulated render_template calls in one request


class TestContextProcessorsLiveInTheirModule(unittest.TestCase):
    def test_app_registers_none_itself(self):
        """The decorator, so app.py may still import the module and its prose
        may still say "context processors" when it explains why a hook exists."""
        registrations = len(_CONTEXT_PROCESSOR_DECORATOR.findall(APP_SOURCE.read_text(encoding="utf-8")))

        self.assertEqual(registrations, 0,
                         "app.py registers context processors itself; they belong "
                         "in dashboard/context_processors.py")


class TestContextProcessorsInstalled(AppTestCase):
    def _runProcessors(self, dash, username=None):
        """The context every processor contributes over one request that
        renders twice, minus Flask's own defaults."""
        context = {}
        with dash.app.test_request_context("/"):
            if username:
                flaskSession["username"] = username
            for _ in range(RENDERS_PER_REQUEST):
                for processor in dash.app.template_context_processors[None]:
                    context.update(processor())
        return {key: value for key, value in context.items() if key not in THIRD_PARTY_CONTEXT_KEYS}

    def test_every_expected_key_reaches_the_template(self):
        dash = self._makeApp()

        self.assertEqual(sorted(self._runProcessors(dash)), sorted(EXPECTED_CONTEXT_KEYS))

    def test_per_user_reads_happen_once_per_request(self):
        """One request can render several templates and each re-runs every
        processor; the per-user reads must not repeat per partial."""
        dash = self._makeApp()
        spies = {name: patch.object(dash.repo, name, wraps=getattr(dash.repo, name))
                 for name in PER_USER_READS}
        mocks = {name: spy.start() for name, spy in spies.items()}
        for spy in spies.values():
            self.addCleanup(spy.stop)

        #< the reauth badge is gated on the callback URL; without it the read never happens
        with patch.dict(os.environ, {SPOTIFY_CALLBACK_URL_ENV_VAR: "http://example.test/cb"}):
            self._runProcessors(dash, username="alice")

        for name, mock in mocks.items():
            with self.subTest(read=name):
                self.assertEqual(mock.call_count, 1)

    def test_an_anonymous_render_reads_no_per_user_state(self):
        """No username in the session means no lookup at all - not a lookup
        for None."""
        dash = self._makeApp()
        spies = {name: patch.object(dash.repo, name, wraps=getattr(dash.repo, name))
                 for name in PER_USER_READS}
        mocks = {name: spy.start() for name, spy in spies.items()}
        for spy in spies.values():
            self.addCleanup(spy.stop)

        with patch.dict(os.environ, {SPOTIFY_CALLBACK_URL_ENV_VAR: "http://example.test/cb"}):
            context = self._runProcessors(dash)

        for name, mock in mocks.items():
            with self.subTest(read=name):
                self.assertEqual(mock.call_count, 0)
        self.assertFalse(context["isAdmin"])
        self.assertFalse(context["hide_tags_panel"])
        self.assertFalse(context["spotifyNeedsReauthBadge"])


if __name__ == "__main__":
    unittest.main()
