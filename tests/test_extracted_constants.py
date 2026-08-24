"""Pins the values extracted into config.py to the places that consume them.

A constant nobody is pinned to is only half an extraction: the literal it
replaced can come back at the call site while the constant sits there agreeing
with itself. Deliberately NOT a test that "the constant exists" - that passes
whether or not anything uses it, which is the failure mode this file exists for.

Two of these read source text instead of behaviour, and say so where they do.
That is a weaker assertion and is used only where the number is genuinely
unobservable from the outside (a `timeout=` handed to requests, a divisor inside
a formatted size string); everything else goes through a real call.
"""
import sys
import os
import pathlib
import unittest
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from _app_factory import AppTestCase

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class TestSessionAndUploadLimits(AppTestCase):
    def test_the_app_takes_its_session_lifetime_from_config(self):
        app = self._makeApp()
        self.assertEqual(app.app.permanent_session_lifetime,
                         timedelta(days=config.PERMANENT_SESSION_LIFETIME_DAYS))

    def test_the_upload_cap_is_the_configured_megabytes(self):
        """MAX_CONTENT_LENGTH is bytes and MAX_UPLOAD_MB is megabytes, so the
        conversion between them is the part that can drift - and it drifts
        silently, as a cap that is 1000x off in either direction."""
        app = self._makeApp()
        self.assertEqual(app.app.config["MAX_CONTENT_LENGTH"],
                         config.MAX_UPLOAD_MB * config.BYTES_PER_MB)


class TestByteUnits(unittest.TestCase):
    def test_the_units_are_binary_and_each_a_kilobyte_apart(self):
        """Computed from the rung below rather than restated, so a decimal-1000
        edit fails here instead of quietly agreeing with itself."""
        self.assertEqual(config.BYTES_PER_KB, 1024)
        self.assertEqual(config.BYTES_PER_MB, config.BYTES_PER_KB * 1024)
        self.assertEqual(config.BYTES_PER_GB, config.BYTES_PER_MB * 1024)

    def test_an_hour_total_splits_on_the_configured_day_length(self):
        self.assertEqual(config.HOURS_PER_DAY, 24)


class TestOverviewSizeFormatting(AppTestCase):
    """The /overview size ladder: three thresholds and three divisions by the
    same values. A partial edit - threshold moved, divisor left behind - is how
    it silently mislabels a size, so both the rendered output and the absence of
    the old literals are pinned."""

    def _sizeText(self, sizeBytes):
        app = self._makeApp()
        client = app.app.test_client()
        #< every other key comes from the real query, so only the one value
        #  under test is fabricated and the stub cannot rot out of shape
        stats = app.repo.getGlobalDatabaseStats()
        stats["db_size_bytes"] = sizeBytes
        with patch.object(type(app.repo), "getGlobalDatabaseStats", return_value=stats):
            return client.get("/overview").get_data(as_text=True)

    def test_a_gigabyte_scale_database_is_labelled_in_gigabytes(self):
        self.assertIn("2.00 GB", self._sizeText(2 * config.BYTES_PER_GB))

    def test_a_megabyte_scale_database_is_labelled_in_megabytes(self):
        self.assertIn("3.00 MB", self._sizeText(3 * config.BYTES_PER_MB))

    def test_a_small_database_is_labelled_in_kilobytes(self):
        self.assertIn("4.0 KB", self._sizeText(4 * config.BYTES_PER_KB))

    def test_the_ladder_carries_no_bare_powers_of_two(self):
        #< source, because the divisors are unobservable from the output above:
        #  a wrong one shows up as a plausible number, not as a missing label
        text = (REPO_ROOT / "routes" / "charts.py").read_text(encoding="utf-8")
        self.assertNotIn("1024 * 1024", text)

    def test_the_overview_page_reads_the_shared_units(self):
        import routes.charts as chartsRoutes
        for name in ("HOURS_PER_DAY", "BYTES_PER_KB", "BYTES_PER_MB", "BYTES_PER_GB"):
            with self.subTest(name):
                self.assertIs(getattr(chartsRoutes, name), getattr(config, name))


class TestSpotifyAuthTimeout(unittest.TestCase):
    def test_the_oauth_code_exchange_carries_no_bare_timeout(self):
        """The exchange sits behind the full OAuth state round-trip, so what it
        hands requests is not cheaply reachable from a test. Asserting on the
        ABSENCE of the literal is the part that still catches it coming back."""
        text = (REPO_ROOT / "routes" / "auth.py").read_text(encoding="utf-8")
        self.assertNotIn("timeout=10", text)
        self.assertIn("timeout=SPOTIFY_AUTH_TIMEOUT_SECONDS", text)

    def test_the_timeout_is_bounded_rather_than_absent(self):
        """The value matters less than it existing at all - requests' default is
        no timeout, which pins a Waitress thread for good."""
        self.assertGreater(config.SPOTIFY_AUTH_TIMEOUT_SECONDS, 0)


class TestWrappedExportLimit(unittest.TestCase):
    def test_the_playlist_export_reaches_deeper_than_the_page_ever_shows(self):
        """The export takes a fixed top-100 rather than whatever the page's
        ?limit= happened to be (see the constant's comment). If it were ever
        reduced to a page size, this is the invariant that breaks."""
        self.assertGreaterEqual(config.WRAPPED_TOP_SONGS_EXPORT_LIMIT,
                                max(config.WRAPPED_LIMIT_OPTIONS))


class TestWrappedExportDepthIsRequested(AppTestCase):
    def test_the_year_export_asks_for_the_configured_depth(self):
        """Behavioural: the route is driven for real and the limit it hands
        _buildWrappedContext is read off the call."""
        app = self._makeApp()
        client = app.app.test_client()
        username, email = "testuser", "testuser@example.com"
        app.repo.upsertUser(username, email)
        app.repo.commit()
        with client.session_transaction() as sess:
            sess["email"] = email
            sess["username"] = username

        with patch.object(app, "is_user_logged_in", return_value=True), \
             patch("Database.database.Database.startListener"), \
             patch.object(type(app), "_buildWrappedContext",
                          return_value={"topSongs": []}) as buildContext:
            client.get("/playlist/export?year=2026&format=csv")

        self.assertTrue(buildContext.called, "the year branch was never reached")
        self.assertEqual(buildContext.call_args.kwargs["limit"],
                         config.WRAPPED_TOP_SONGS_EXPORT_LIMIT)


class TestDeadImportsStayGone(unittest.TestCase):
    def test_app_does_not_import_math_or_json(self):
        """Both were unused. app.py re-exports its namespace via `from config
        import *`, so a dead import there is reachable as `app.math` and reads
        as something the module needs."""
        import app as appModule
        self.assertFalse(hasattr(appModule, "math"))
        self.assertFalse(hasattr(appModule, "json"))
