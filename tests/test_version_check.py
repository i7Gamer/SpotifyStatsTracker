"""_versionCheckLoop must notify only for a published GitHub Release, not any
Database/VERSION bump merged to main - main can (and did) sit ahead of the
last actual release while work is in progress (see
.github/workflows/dockerReleaseTag.yml, which builds/pushes a Docker image
from a manually-chosen tag, not from main directly)."""
import sys
import os
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase

HOURLY_WAIT_SECONDS = 60 * 60   #< the pass's closing wait - reaching it proves the pass completed


def _runOnePass(dash):
    """Runs exactly one iteration of _versionCheckLoop - see the identical
    stop_event.is_set()/wait() stubbing pattern in test_metadata_backfiller.py."""
    dash._stop_event = MagicMock()
    dash._stop_event.is_set.side_effect = [False, True]
    dash._stop_event.wait.return_value = False
    dash._versionCheckLoop()


def _response(status_code=200, tag_name=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"tag_name": tag_name} if tag_name is not None else {}
    return resp


class TestVersionCheckLoop(AppTestCase):
    def test_hits_the_releases_api_not_the_raw_version_file(self):
        dash = self._makeApp()
        dash.currentVersion = "1.30.0"

        with patch("app.requests.get", return_value=_response(tag_name="1.31.0")) as mock_get:
            _runOnePass(dash)

        url = mock_get.call_args.args[0]
        self.assertIn("api.github.com/repos/i7Gamer/SpotifyStatsTracker/releases/latest", url)
        self.assertEqual(dash.latestVersion, "1.31.0")

    def test_ignores_a_dev_version_ahead_of_the_last_release(self):
        """Regression: main's VERSION file can be bumped well before that
        version is actually tagged/released - the badge must not fire off
        of that bump, only off a real release newer than what's running."""
        dash = self._makeApp()
        dash.currentVersion = "1.38.0"   # already ahead of the latest release below

        with patch("app.requests.get", return_value=_response(tag_name="1.31.0")):
            _runOnePass(dash)

        self.assertIsNone(dash.latestVersion)

    def test_strips_a_leading_v_from_the_release_tag(self):
        dash = self._makeApp()
        dash.currentVersion = "1.30.0"

        with patch("app.requests.get", return_value=_response(tag_name="v1.31.0")):
            _runOnePass(dash)

        self.assertEqual(dash.latestVersion, "1.31.0")

    def test_no_releases_published_yet_clears_latest_version(self):
        dash = self._makeApp()
        dash.currentVersion = "1.30.0"
        dash.latestVersion = "1.29.0"   # stale value from a previous successful check

        with patch("app.requests.get", return_value=_response(status_code=404)):
            _runOnePass(dash)

        self.assertIsNone(dash.latestVersion)

    def test_same_version_is_not_treated_as_newer(self):
        dash = self._makeApp()
        dash.currentVersion = "1.31.0"

        with patch("app.requests.get", return_value=_response(tag_name="1.31.0")):
            _runOnePass(dash)

        self.assertIsNone(dash.latestVersion)


class TestVersionCheckSurvivesFailures(AppTestCase):
    """This loop runs for the life of the process against a third-party API it
    does not control, so every failure mode has to satisfy two things at once:
    the pass must complete (an escaping exception ends the thread, and nothing
    restarts it - the instance would silently stop noticing releases until the
    next reboot), and a previously-found update must survive untouched.

    That second half is the subtle one. Only a 404 means "no release exists to
    notify about"; a timeout, a 429 or a malformed body mean "we learned
    nothing this hour". Clearing latestVersion on those would blink the update
    badge off for an hour at a time, which reads to a user as the update having
    been withdrawn.
    """

    def _dashWithAKnownUpdate(self):
        """An instance that already found 1.31.0 on an earlier successful pass -
        the state each failure below has to leave alone. Starting from None
        would let a test pass while the code cleared the value."""
        dash = self._makeApp()
        dash.currentVersion = "1.30.0"
        dash.latestVersion = "1.31.0"
        return dash

    def test_a_network_failure_leaves_the_loop_running_and_the_badge_intact(self):
        dash = self._dashWithAKnownUpdate()

        with patch("app.requests.get", side_effect=requests.exceptions.Timeout("timed out")):
            _runOnePass(dash)

        self.assertEqual(dash.latestVersion, "1.31.0")
        dash._stop_event.wait.assert_any_call(HOURLY_WAIT_SECONDS)

    def test_an_unhandled_status_does_not_clear_a_known_update(self):
        """429 (GitHub's unauthenticated hourly cap, which this endpoint is well
        within but shares with everything else on the egress IP) and 5xx are
        neither a release nor evidence that none exists."""
        for status in (429, 500, 503):
            with self.subTest(status=status):
                dash = self._dashWithAKnownUpdate()

                with patch("app.requests.get", return_value=_response(status_code=status)):
                    _runOnePass(dash)

                self.assertEqual(dash.latestVersion, "1.31.0")
                dash._stop_event.wait.assert_any_call(HOURLY_WAIT_SECONDS)

    def test_a_malformed_response_body_is_swallowed(self):
        """A 200 carrying HTML instead of JSON - a captive portal or a proxy
        error page - raises inside .json(), not at the request."""
        dash = self._dashWithAKnownUpdate()
        resp = _response(status_code=200)
        resp.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")

        with patch("app.requests.get", return_value=resp):
            _runOnePass(dash)

        self.assertEqual(dash.latestVersion, "1.31.0")
        dash._stop_event.wait.assert_any_call(HOURLY_WAIT_SECONDS)

    def test_a_malformed_release_tag_is_logged_and_ignored(self):
        """versionTuple int()s each dotted component, so a tag that isn't a
        version ("nightly", "v2-beta") raises. This one is worth a WARNING
        rather than the DEBUG the transport failures get: it means a release in
        this repo was tagged in a shape the updater can't read, which is our
        own mistake to fix and won't clear up on its own."""
        dash = self._dashWithAKnownUpdate()

        with patch("app.requests.get", return_value=_response(tag_name="nightly")), \
             self.assertLogs("app", level="WARNING") as logs:
            _runOnePass(dash)

        self.assertEqual(dash.latestVersion, "1.31.0")
        self.assertTrue(any("nightly" in line for line in logs.output),
                        f"the offending tag must be named in the log: {logs.output}")


if __name__ == "__main__":
    import unittest
    unittest.main()
