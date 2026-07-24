"""_versionCheckLoop must notify only for a published GitHub Release, not any
Database/VERSION bump merged to main - main can (and did) sit ahead of the
last actual release while work is in progress (see
.github/workflows/dockerReleaseTag.yml, which builds/pushes a Docker image
from a manually-chosen tag, not from main directly)."""
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase


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


if __name__ == "__main__":
    import unittest
    unittest.main()
