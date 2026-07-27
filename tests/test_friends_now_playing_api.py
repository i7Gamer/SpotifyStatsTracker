"""The friends half of /api/now-playing.

This is a disclosure surface: it reports, live, what other people are doing.
The tests that matter most are the negative ones - who must NOT appear - so
they come first. The two toggles guarding it are covered in
test_friends_now_playing_settings.py.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from config import FRIENDS_NOW_PLAYING_LIMIT
from _app_factory import AppTestCase


def _nowPlaying(name="Nightcall", trackId="t1", isPaused=False):
    """A getNowPlaying() return value, in the shape listener.py produces."""
    return {
        "trackId": trackId, "name": name, "artistsText": "Kavinsky",
        "artists": [{"id": "art1", "name": "Kavinsky", "played": True}],
        "trackPlayed": True, "imageId": "img1", "isPaused": isPaused,
        "positionMs": 42000, "durationMs": 250000,
    }


class FriendsNowPlayingTestCase(AppTestCase):
    VIEWER = "alice"
    VIEWER_EMAIL = "alice@example.com"

    def setUp(self):
        self.dash = self._makeApp()
        self.dash.repo.upsertUser(self.VIEWER, self.VIEWER_EMAIL)

    def _addUser(self, username, playing=None, hideNowPlaying=False, live=True):
        """A second account, optionally sharing with the viewer and optionally
        with a live Database in this process."""
        self.dash.repo.upsertUser(username, f"{username}@example.com")
        self.dash.repo.setUserCookies(username, {"sp_dc": "cookie"})
        if hideNowPlaying:
            self.dash.repo.updateUserSettings(username, "day", None, hide_now_playing=True)
        if live:
            db = MagicMock()
            db.getNowPlaying.return_value = playing
            self.dash.user_databases[username] = db
            return db
        return None

    def _share(self, username):
        """An accepted (mutual) share between the viewer and `username`: the
        counterpart requests, the viewer accepts."""
        self.dash.repo.createShareRequest(username, self.VIEWER)
        pending = self.dash.repo.getPendingIncomingShares(self.VIEWER)
        shareId = next(row["id"] for row in pending if row["requester_username"] == username)
        self.dash.repo.respondToShareRequest(shareId, self.VIEWER, accept=True)

    def _payload(self):
        return self.dash.getFriendsNowPlaying(self.VIEWER)


class TestWhoIsExcluded(FriendsNowPlayingTestCase):
    def test_a_user_with_no_share_never_appears(self):
        """The whole authorization boundary in one test: bob is actively
        playing and has a live session, but has never shared with alice."""
        self._addUser("bob", playing=_nowPlaying())

        self.assertEqual(self._payload()["friends"], [])

    def test_a_pending_share_is_not_enough(self):
        self._addUser("bob", playing=_nowPlaying())
        self.dash.repo.createShareRequest(self.VIEWER, "bob")   #< never accepted

        self.assertEqual(self._payload()["friends"], [])

    def test_a_friend_who_opted_out_is_excluded(self):
        self._addUser("bob", playing=_nowPlaying(), hideNowPlaying=True)
        self._share("bob")

        self.assertEqual(self._payload()["friends"], [])

    def test_a_paused_friend_is_excluded(self):
        self._addUser("bob", playing=_nowPlaying(isPaused=True))
        self._share("bob")

        self.assertEqual(self._payload()["friends"], [])

    def test_a_friend_playing_nothing_is_excluded(self):
        self._addUser("bob", playing=None)
        self._share("bob")

        self.assertEqual(self._payload()["friends"], [])

    def test_a_friend_with_no_live_session_is_skipped_not_started(self):
        """Constructing a Database here would start another user's listener
        from this request thread."""
        self._addUser("bob", live=False)
        self._share("bob")

        with patch.object(self.dash, "get_user_db") as getUserDb:
            payload = self._payload()

        self.assertEqual(payload["friends"], [])
        getUserDb.assert_not_called()

    def test_a_failing_lookup_drops_only_that_friend(self):
        broken = self._addUser("bob", playing=None)
        broken.getNowPlaying.side_effect = RuntimeError("listener exploded")
        self._share("bob")
        self._addUser("carol", playing=_nowPlaying(name="Teardrop"))
        self._share("carol")

        friends = self._payload()["friends"]

        self.assertEqual([f["username"] for f in friends], ["carol"])


class TestWhatIsReported(FriendsNowPlayingTestCase):
    def test_a_sharing_friend_who_is_playing_appears(self):
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")

        friends = self._payload()["friends"]

        self.assertEqual(len(friends), 1)
        self.assertEqual(friends[0]["username"], "bob")
        self.assertEqual(friends[0]["name"], "Nightcall")
        self.assertEqual(friends[0]["artistsText"], "Kavinsky")
        self.assertEqual(friends[0]["imageId"], "img1")

    def test_a_chip_carries_the_display_name_alongside_the_username(self):
        """The chips are built client-side, so they can't go through the Jinja
        filter - the name people go by has to ride in the payload. `username`
        stays too: dashboard-page.js falls back to it."""
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")
        self.dash.repo.setDisplayName("bob", "Bob Builder")

        entry = self._payload()["friends"][0]

        self.assertEqual(entry["displayName"], "Bob Builder")
        self.assertEqual(entry["username"], "bob")

    def test_a_friend_without_one_reports_their_username_as_the_display_name(self):
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")

        self.assertEqual(self._payload()["friends"][0]["displayName"], "bob")

    def test_playback_position_and_history_flags_are_not_disclosed(self):
        """Narrower than the viewer's own payload on purpose: progress is noise
        at chip size, and the played flags describe the friend's own history."""
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")

        entry = self._payload()["friends"][0]

        for leaked in ("positionMs", "durationMs", "trackPlayed", "artists", "isPaused"):
            with self.subTest(field=leaked):
                self.assertNotIn(leaked, entry)

    def test_friends_are_ordered_stably(self):
        """Chips must not reshuffle between 15s polls."""
        for name in ("carol", "bob", "dave"):
            self._addUser(name, playing=_nowPlaying())
            self._share(name)

        self.assertEqual([f["username"] for f in self._payload()["friends"]],
                         ["bob", "carol", "dave"])

    def test_the_strip_is_capped_and_reports_the_overflow(self):
        for index in range(FRIENDS_NOW_PLAYING_LIMIT + 3):
            name = f"friend{index:02d}"
            self._addUser(name, playing=_nowPlaying())
            self._share(name)

        payload = self._payload()

        self.assertEqual(len(payload["friends"]), FRIENDS_NOW_PLAYING_LIMIT)
        self.assertEqual(payload["moreCount"], 3)

    def test_no_overflow_count_when_under_the_cap(self):
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")

        self.assertEqual(self._payload()["moreCount"], 0)


class TestToggleGating(FriendsNowPlayingTestCase):
    def test_the_admin_switch_empties_the_strip(self):
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")
        self.dash.repo.setFriendsNowPlayingEnabled(False)

        self.assertEqual(self._payload(), {"friends": [], "moreCount": 0})

    def test_turning_off_data_sharing_also_empties_it(self):
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")
        self.dash.repo.setDataSharingEnabled(False)

        self.assertEqual(self._payload(), {"friends": [], "moreCount": 0})

    def test_a_disabled_switch_skips_the_lookups_entirely(self):
        db = self._addUser("bob", playing=_nowPlaying())
        self._share("bob")
        self.dash.repo.setFriendsNowPlayingEnabled(False)

        self._payload()

        db.getNowPlaying.assert_not_called()


class TestEndpoint(FriendsNowPlayingTestCase):
    def _client(self):
        viewerDb = MagicMock()
        viewerDb.getNowPlaying.return_value = _nowPlaying(name="My Song", trackId="mine")
        for patcher in (
            patch.object(self.dash, "is_user_logged_in", return_value=True),
            patch.object(self.dash, "get_username_for_email", return_value=self.VIEWER),
            patch.object(self.dash, "get_user_db", return_value=viewerDb),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess["email"] = self.VIEWER_EMAIL
            sess["username"] = self.VIEWER
        return client

    def test_the_poll_returns_both_halves_in_one_request(self):
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")

        payload = self._client().get("/api/now-playing").get_json()

        self.assertEqual(payload["nowPlaying"]["name"], "My Song")
        self.assertEqual([f["username"] for f in payload["friends"]], ["bob"])
        self.assertEqual(payload["friendsMoreCount"], 0)

    def test_the_friends_key_is_always_present(self):
        """The client renders off this key every poll; an absent key would
        make it fall back to whatever was on screen."""
        payload = self._client().get("/api/now-playing").get_json()

        self.assertEqual(payload["friends"], [])

    def test_an_anonymous_request_is_rejected(self):
        with patch.object(self.dash, "is_user_logged_in", return_value=False):
            resp = self.dash.app.test_client().get("/api/now-playing")

        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
