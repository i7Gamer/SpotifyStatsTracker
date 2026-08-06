"""The half-deployed instance guard (services/deploy_state.py, /admin).

A file-copy deploy that skips the restart leaves the instance running two
builds at once: Flask serves static/ from disk on every request, while route
code and compiled Jinja templates stay in memory from boot. The browser then
runs the new scripts against the old markup.

On 2026-08-06 that state ran for 20 hours here and presented as five unrelated
frontend bugs. Nothing in the app could see it, and the one thing that looked
like evidence was misleading: the version badge reported the RUNNING version
and was telling the truth.

Two signals, because either alone has a hole. The VERSION file is authoritative
but only moves on a release bump - 25 commits once shipped between two bumps,
the whole htmx migration among them. A source file newer than the process
catches those, and cannot tell you what changed.
"""
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
#< the admin page needs a dozen patches to render; its own fixture owns them
from test_admin_route import AdminRouteTestBase
from services.deploy_state import (
    DEPLOY_MTIME_GRACE_SECONDS, deployMismatch, newestSourceMtime,
)

_STARTED_AT = 1_000_000.0
_OLDER = _STARTED_AT - 60          #< copied before the process read it
_NEWER = _STARTED_AT + DEPLOY_MTIME_GRACE_SECONDS + 1


class TestDeployMismatch(unittest.TestCase):
    def test_a_matching_build_reports_nothing(self):
        self.assertIsNone(deployMismatch("1.46.4", "1.46.4", _STARTED_AT, _OLDER))

    def test_a_newer_version_on_disk_is_a_mismatch(self):
        mismatch = deployMismatch("1.46.2", "1.46.3", _STARTED_AT, _OLDER)

        self.assertEqual(mismatch["runningVersion"], "1.46.2")
        self.assertEqual(mismatch["diskVersion"], "1.46.3")

    def test_an_older_version_on_disk_counts_too(self):
        """A rollback that copied the files and left the process up is the same
        split build, and reads as the newer one still running."""
        self.assertIsNotNone(deployMismatch("1.46.4", "1.46.2", _STARTED_AT, _OLDER))

    def test_an_unreadable_version_file_is_not_a_mismatch(self):
        """The reading failed; the deploy did not. Crying wolf here would teach
        an admin to ignore the banner, which is the only thing it has."""
        self.assertIsNone(deployMismatch("1.46.4", None, _STARTED_AT, _OLDER))
        self.assertIsNone(deployMismatch("1.46.4", "", _STARTED_AT, _OLDER))

    def test_source_newer_than_the_process_is_a_mismatch_on_its_own(self):
        """The case the version check cannot see: files replaced between two
        releases, so both builds call themselves 1.46.4."""
        mismatch = deployMismatch("1.46.4", "1.46.4", _STARTED_AT, _NEWER)

        self.assertTrue(mismatch["filesChanged"])
        self.assertIsNone(mismatch["diskVersion"])   #< nothing to report there

    def test_a_file_touched_during_startup_is_not_a_mismatch(self):
        """Copy-then-start is the normal deploy, and a boot takes seconds - the
        grace is what keeps that from being reported as a stale one."""
        self.assertIsNone(deployMismatch("1.46.4", "1.46.4", _STARTED_AT,
                                         _STARTED_AT + DEPLOY_MTIME_GRACE_SECONDS - 1))

    def test_an_unscannable_tree_is_not_a_mismatch(self):
        self.assertIsNone(deployMismatch("1.46.4", "1.46.4", _STARTED_AT, None))

    def test_both_signals_are_reported_together(self):
        mismatch = deployMismatch("1.46.2", "1.46.3", _STARTED_AT, _NEWER)

        self.assertEqual(mismatch["diskVersion"], "1.46.3")
        self.assertTrue(mismatch["filesChanged"])


class TestNewestSourceMtime(unittest.TestCase):
    """What counts as source: the files this process loads ONCE. static/ is
    deliberately out - it is read from disk per request, so a change there is
    already live rather than stale."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _write(self, relative, mtime):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_it_finds_the_newest_template(self):
        self._write("templates/tracks.html", 1000)
        self._write("templates/admin.html", 5000)

        self.assertEqual(newestSourceMtime(self.root), 5000)

    def test_it_covers_the_python_packages_and_the_root_modules(self):
        self._write("routes/charts.py", 2000)
        self._write("Database/database.py", 3000)
        self._write("app.py", 4000)

        self.assertEqual(newestSourceMtime(self.root), 4000)

    # Every file below deliberately carries a scanned SUFFIX. Writing a .db, a
    # .pyc and a .js here instead - the obvious choice - made all three of
    # these pass with the directory rules deleted outright, because the suffix
    # filter alone rejected them. They were three tests of one thing.

    def test_a_users_own_data_never_counts_as_a_deploy(self):
        """Database/Data holds the live databases, the media tree and the logs
        - 20,919 files on the instance this was written against, written to
        constantly. Descending into it costs 5x the walk (measured: 5.0ms ->
        24.4ms per /admin load) and would eventually make the banner permanent,
        which is the same as not having one."""
        self._write("templates/tracks.html", 1000)
        self._write("Database/Data/leftover.py", 9999)

        self.assertEqual(newestSourceMtime(self.root), 1000)

    def test_it_does_not_even_walk_into_the_data_directory(self):
        """The cost is the point, and it is invisible to the assertion above:
        the exclusion has to skip the directory, not just ignore its files."""
        self._write("templates/tracks.html", 1000)
        self._write("Database/Data/Media/cover.py", 9999)
        visited = []
        realWalk = os.walk

        def spy(top, *args, **kwargs):
            for entry in realWalk(top, *args, **kwargs):
                visited.append(entry[0])
                yield entry

        with patch("services.deploy_state.os.walk", spy):
            newestSourceMtime(self.root)

        self.assertTrue(visited, "the walk never ran")
        self.assertFalse([path for path in visited if "Data" in Path(path).parts])

    def test_stale_bytecode_does_not_count(self):
        self._write("routes/charts.py", 1000)
        self._write("routes/__pycache__/charts.py", 9999)

        self.assertEqual(newestSourceMtime(self.root), 1000)

    def test_static_assets_are_out_of_scope(self):
        """They are served from disk per request. A change there is not a
        stale-process symptom - it is the half that IS live, and the reason
        this whole failure mode is so confusing."""
        self._write("templates/tracks.html", 1000)
        self._write("static/offline.html", 9999)

        self.assertEqual(newestSourceMtime(self.root), 1000)

    def test_a_tree_with_nothing_in_it_answers_none(self):
        self.assertIsNone(newestSourceMtime(self.root / "nope"))


class TestTheAppReportsItsOwnState(AppTestCase):
    def test_a_freshly_started_app_reports_no_mismatch(self):
        """The suite's own app is built from the checkout it runs against, so
        anything but None here means the guard is measuring the wrong thing."""
        self.assertIsNone(self._makeApp().getDeployMismatch())

    def test_it_reads_the_version_file_now_rather_than_at_boot(self):
        """The whole point: currentVersion is read once, on purpose (the app
        cannot update without a restart), so the comparison needs a fresh read
        of the same file - not a second look at the same variable."""
        dash = self._makeApp()
        dash.currentVersion = "0.0.1"   #< as if this process booted on an older build

        mismatch = dash.getDeployMismatch()

        self.assertIsNotNone(mismatch)
        self.assertEqual(mismatch["runningVersion"], "0.0.1")
        self.assertNotEqual(mismatch["diskVersion"], "0.0.1")


class TestTheAdminBanner(AdminRouteTestBase):
    """Where an admin finds out. Built on the admin route's own fixture: the
    page needs a dozen patches to render at all, and this is about the one
    section they all sit under."""

    def _adminHtml(self, mismatch):
        dash = self._makeApp()
        patches = self._patches(dash, isAdmin=True)
        patches.append(patch.object(dash, "getDeployMismatch", return_value=mismatch))
        return self._getAdmin(dash, patches=patches).data.decode()

    def test_a_matching_build_shows_no_banner(self):
        self.assertNotIn("deploy-mismatch", self._adminHtml(None))

    def test_the_banner_names_both_versions_and_says_to_restart(self):
        html = self._adminHtml({"runningVersion": "1.46.2", "diskVersion": "1.46.3",
                                "filesChanged": True})

        self.assertIn("deploy-mismatch", html)
        self.assertIn("1.46.2", html)
        self.assertIn("1.46.3", html)
        self.assertIn("Restart", html)

    def test_the_banner_stands_alone_when_only_the_files_moved(self):
        """No version to name, and rendering "1.46.4 -> None" would read as a
        broken banner rather than as the unbumped deploy it is."""
        html = self._adminHtml({"runningVersion": "1.46.4", "diskVersion": None,
                                "filesChanged": True})

        self.assertIn("deploy-mismatch", html)
        self.assertIn("The application files have changed", html)
        self.assertNotIn("the files on disk are", html)   #< the two-version wording


if __name__ == "__main__":
    unittest.main()
