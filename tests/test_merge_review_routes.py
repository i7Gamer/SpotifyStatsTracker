"""The /admin/merge-review page: the review queue's buttons, guards and flashes.

Repo-level semantics (what is proposed, what the verdicts write) live in
test_merge_review.py; this file covers the surface - the page renders the real
planner's groups, the two POST verbs write through the real repo, and none of
it is reachable without admin."""
import os
import re
import sys
import unittest
from unittest.mock import patch, MagicMock
from urllib.parse import unquote_plus

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase

DURATION_MS = 200000

_RADIO_TAG = re.compile(r"<input[^>]*data-merge-main-radio[^>]*>")
_ACTIONS_TAG = re.compile(r"<div[^>]*data-merge-actions[^>]*>")
_BADGE_TAG = re.compile(r"<span[^>]*data-merge-main-badge[^>]*>")
_VALUE_ATTR = re.compile(r'value="([^"]*)"')


class MergeReviewRouteTestCase(AppTestCase):
    def _seedPair(self, dash, originalPlays=70, remasterPlays=13):
        """One review-worthy pair: an original and its remaster."""
        conn = dash.repo._conn()
        with conn:
            conn.execute("INSERT INTO users (username, created_at, is_admin) "
                         "VALUES ('alice', 0, 1)")
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'Talking Heads: 77', '')")
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb2', 'The Best Of', '')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('art1', 'Talking Heads', '')")
            for trackId, name, albumId, plays in (
                    ("A" * 22, "Psycho Killer", "alb1", originalPlays),
                    ("B" * 22, "Psycho Killer - 2005 Remaster", "alb2", remasterPlays)):
                conn.execute(
                    "INSERT INTO tracks (id, name, url, album_id, duration_ms, created_at) "
                    "VALUES (?, ?, '', ?, ?, 1000.0)", (trackId, name, albumId, DURATION_MS))
                conn.execute("INSERT INTO track_artists (track_id, artist_id, position) "
                             "VALUES (?, 'art1', 0)", (trackId,))
                for i in range(plays):
                    conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                                 "VALUES ('alice', ?, ?, 200000)", (trackId, 1e9 + i))

    def _seedTrio(self, dash):
        """Three releases of one song, so a group survives its first merge.

        Sized for the re-election trap: with the plain release gone, neither
        survivor is plain, so the shortest name wins - the Mono cut, not the
        remaster a person would have picked."""
        conn = dash.repo._conn()
        with conn:
            conn.execute("INSERT INTO users (username, created_at, is_admin) "
                         "VALUES ('alice', 0, 1)")
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'Talking Heads: 77', '')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('art1', 'Talking Heads', '')")
            for trackId, name, plays in (
                    ("P" * 22, "Psycho Killer", 1),
                    ("R" * 22, "Psycho Killer - 2005 Remaster", 50),
                    ("M" * 22, "Psycho Killer - Mono", 5)):
                conn.execute(
                    "INSERT INTO tracks (id, name, url, album_id, duration_ms, created_at) "
                    "VALUES (?, ?, '', 'alb1', ?, 1000.0)", (trackId, name, DURATION_MS))
                conn.execute("INSERT INTO track_artists (track_id, artist_id, position) "
                             "VALUES (?, 'art1', 0)", (trackId,))
                for i in range(plays):
                    conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                                 "VALUES ('alice', ?, ?, 200000)", (trackId, 1e9 + i))

    def _request(self, dash, method, path, data=None, isAdmin=True):
        with patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=MagicMock()):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
                sess['username'] = 'alice'
            if method == "GET":
                return client.get(path)
            return client.post(path, data=data or {})

    def _canonical(self, dash, trackId):
        return dash.repo._conn().execute(
            "SELECT canonical_id FROM tracks WHERE id=?", (trackId,)).fetchone()[0]


class TestMergeReviewPage(MergeReviewRouteTestCase):
    def test_non_admin_gets_403(self):
        dash = self._makeApp()
        self.assertEqual(
            self._request(dash, "GET", "/admin/merge-review", isAdmin=False).status_code, 403)

    def test_the_page_shows_the_group_with_both_verdict_buttons(self):
        dash = self._makeApp()
        self._seedPair(dash)

        resp = self._request(dash, "GET", "/admin/merge-review")
        body = resp.data.decode()

        self.assertEqual(resp.status_code, 200)
        self.assertIn("Psycho Killer - 2005 Remaster", body)
        #< the album is what tells two same-name releases apart on sight
        self.assertIn("Talking Heads: 77", body)
        self.assertIn("The Best Of", body)
        self.assertIn("/admin/merge_review/merge", body)
        self.assertIn("/admin/merge_review/reject", body)
        self.assertIn(str(70), body)

    def test_an_empty_queue_says_so_instead_of_rendering_nothing(self):
        dash = self._makeApp()
        body = self._request(dash, "GET", "/admin/merge-review").data.decode()

        self.assertIn("Nothing to review", body)


class TestMainVersionPicker(MergeReviewRouteTestCase):
    """Which release stays as the song's page is a suggestion, not a verdict -
    so every release in a group carries a control to become it."""

    def _radios(self, dash, main=None):
        path = "/admin/merge-review" + (("?main=" + main) if main else "")
        body = self._request(dash, "GET", path).data.decode()
        return [(_VALUE_ATTR.search(tag).group(1), "checked" in tag)
                for tag in _RADIO_TAG.findall(body)]

    def test_every_release_offers_itself_as_the_main_version(self):
        dash = self._makeApp()
        self._seedPair(dash)

        self.assertEqual(self._radios(dash), [("A" * 22, True), ("B" * 22, False)])

    def test_the_plain_title_is_suggested_even_when_the_remaster_wins_on_plays(self):
        """The suggestion the page leads with, and the reason the picker is
        not the only half of this: a remaster seeded better than its original
        used to take the song's page on play count alone."""
        dash = self._makeApp()
        self._seedPair(dash, originalPlays=13, remasterPlays=70)

        self.assertEqual(self._radios(dash), [("A" * 22, True), ("B" * 22, False)])

    def test_the_page_loads_the_script_that_makes_the_picker_do_anything(self):
        """A radio nothing listens to is a control that lies. The rest of the
        page works without it - the server renders the suggested main badged
        and the others with their buttons - so only the OVERRULE is lost, and
        silently, which is exactly why this is asserted here."""
        dash = self._makeApp()
        self._seedPair(dash)
        body = self._request(dash, "GET", "/admin/merge-review").data.decode()

        self.assertIn("js/merge-review.js", body)

    def test_the_suggested_main_carries_no_verdict_buttons_of_its_own(self):
        """It cannot merge into itself, and "not the same" aimed at the
        release the group is built around answers a question nobody asked.

        Both halves are rendered on every row and hidden the other way round,
        which is the whole reason the picker can swap them client-side - so
        the badge is asserted alongside, not instead."""
        dash = self._makeApp()
        self._seedPair(dash)
        body = self._request(dash, "GET", "/admin/merge-review").data.decode()

        self.assertEqual([("hidden" in tag) for tag in _ACTIONS_TAG.findall(body)],
                         [True, False])
        self.assertEqual([("hidden" in tag) for tag in _BADGE_TAG.findall(body)],
                         [False, True])

    def test_picking_the_other_release_merges_the_other_way(self):
        """What the picker posts once a person overrules the suggestion: the
        member and canonical swap, and the remaster keeps the song's page."""
        dash = self._makeApp()
        self._seedPair(dash)

        resp = self._request(dash, "POST", "/admin/merge_review/merge",
                             data={"member": "A" * 22, "canonical": "B" * 22})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._canonical(dash, "A" * 22), "B" * 22)
        self.assertIsNone(self._canonical(dash, "B" * 22))

    def test_the_pick_survives_the_reload_a_merge_causes(self):
        """A group of three takes more than one click, and every click is a
        POST-redirect-GET. The pick lived only in the page it was made on, so
        the second render re-elected from the survivors and suggested a THIRD
        release - one more click on the default and the song's page landed on
        a release nobody chose."""
        dash = self._makeApp()
        self._seedTrio(dash)

        resp = self._request(dash, "POST", "/admin/merge_review/merge",
                             data={"member": "P" * 22, "canonical": "R" * 22})

        self.assertIn("main=" + "R" * 22, unquote_plus(resp.headers["Location"]))
        self.assertEqual(self._radios(dash, main="R" * 22),
                         [("M" * 22, False), ("R" * 22, True)])

    def test_the_pick_survives_a_reject_too(self):
        dash = self._makeApp()
        self._seedTrio(dash)

        resp = self._request(dash, "POST", "/admin/merge_review/reject",
                             data={"member": "P" * 22, "canonical": "R" * 22})

        self.assertIn("main=" + "R" * 22, unquote_plus(resp.headers["Location"]))

    def test_a_pick_naming_no_release_in_the_group_falls_back_to_the_suggestion(self):
        """Same rule the picker's own decision function uses, so the server's
        render and the script's first pass cannot disagree: a main that is not
        on offer is not a main. It is also never echoed into the page."""
        dash = self._makeApp()
        self._seedPair(dash)

        self.assertEqual(self._radios(dash, main="Z" * 22),
                         [("A" * 22, True), ("B" * 22, False)])
        body = self._request(dash, "GET",
                             "/admin/merge-review?main=" + "Z" * 22).data.decode()
        self.assertNotIn("Z" * 22, body)


class TestMergeReviewVerdicts(MergeReviewRouteTestCase):
    def test_merge_writes_through_and_reports_back(self):
        dash = self._makeApp()
        self._seedPair(dash)

        resp = self._request(dash, "POST", "/admin/merge_review/merge",
                             data={"member": "B" * 22, "canonical": "A" * 22})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/merge-review", resp.headers["Location"])
        self.assertIn("Merged 1 release(s)", unquote_plus(resp.headers["Location"]))
        self.assertEqual(self._canonical(dash, "B" * 22), "A" * 22)

    def test_reject_pins_and_reports_back(self):
        dash = self._makeApp()
        self._seedPair(dash)

        resp = self._request(dash, "POST", "/admin/merge_review/reject",
                             data={"member": "B" * 22})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("not be suggested again", unquote_plus(resp.headers["Location"]))
        row = dash.repo._conn().execute(
            "SELECT reason, decided_by FROM track_merge_decisions WHERE track_id=?",
            ("B" * 22,)).fetchone()
        self.assertEqual(row["reason"], "manual-reject")
        self.assertEqual(row["decided_by"], "alice")
        #< and the queue is now empty for the next GET
        body = self._request(dash, "GET", "/admin/merge-review").data.decode()
        self.assertIn("Nothing to review", body)

    def test_a_stale_reject_lands_back_on_the_queue_instead_of_recording(self):
        """The realistic race: the queue open in two tabs, or the matcher
        merging the pair between render and click. Unlike crafted junk this is
        not a 400 - the admin did nothing wrong - but nothing is written
        either: they land back on the queue (which no longer proposes the
        merged member) with an explanation, and the merge stands."""
        dash = self._makeApp()
        self._seedPair(dash)
        dash.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="alice")

        resp = self._request(dash, "POST", "/admin/merge_review/reject",
                             data={"member": "B" * 22})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("merged after this page loaded", unquote_plus(resp.headers["Location"]))
        self.assertEqual(self._canonical(dash, "B" * 22), "A" * 22)
        row = dash.repo._conn().execute(
            "SELECT reason FROM track_merge_decisions WHERE track_id=?",
            ("B" * 22,)).fetchone()
        self.assertEqual(row["reason"], "manual-merge")

    def test_an_unknown_track_is_a_400_not_a_crash(self):
        dash = self._makeApp()
        self._seedPair(dash)

        for path, data in (("/admin/merge_review/merge",
                            {"member": "Z" * 22, "canonical": "A" * 22}),
                           ("/admin/merge_review/reject", {"member": "Z" * 22})):
            self.assertEqual(self._request(dash, "POST", path, data=data).status_code, 400)

    def test_verdicts_are_admin_only(self):
        dash = self._makeApp()
        self._seedPair(dash)

        for path, data in (("/admin/merge_review/merge",
                            {"member": "B" * 22, "canonical": "A" * 22}),
                           ("/admin/merge_review/reject", {"member": "B" * 22})):
            self.assertEqual(
                self._request(dash, "POST", path, data=data, isAdmin=False).status_code, 403)
        self.assertIsNone(self._canonical(dash, "B" * 22))


class TestTheDismissalLog(MergeReviewRouteTestCase):
    """"Both answers are remembered" was only half true on the page: the merge
    is visible on the song and splittable there, while the "no" removed a pair
    from the queue with nothing left to look at."""

    def test_a_dismissal_is_listed_with_a_way_to_take_it_back(self):
        dash = self._makeApp()
        self._seedPair(dash)
        dash.repo.dismissMergeCandidate("B" * 22, decidedBy="alice")

        body = self._request(dash, "GET", "/admin/merge-review").data.decode()

        self.assertIn("Kept separate", body)
        self.assertIn("Psycho Killer - 2005 Remaster", body)
        self.assertIn("/admin/merge_review/undismiss", body)
        #< who ruled and when, so a shared instance can tell two admins apart
        self.assertIn("alice", body)

    def test_the_reject_button_records_what_the_no_was_ruled_against(self):
        """The form carries the same `canonical` the merge verb does, so the
        log can say not the same as WHAT."""
        dash = self._makeApp()
        self._seedPair(dash)

        self._request(dash, "POST", "/admin/merge_review/reject",
                      data={"member": "B" * 22, "canonical": "A" * 22})

        entry = dash.repo.getDismissedMergeCandidates()["entries"][0]
        self.assertEqual(entry["against"]["trackId"], "A" * 22)
        body = self._request(dash, "GET", "/admin/merge-review").data.decode()
        self.assertIn("not the same as Psycho Killer", body)

    def test_a_reject_naming_a_junk_counterpart_is_a_400_not_a_crash(self):
        """Unchecked it reaches the FOREIGN KEY instead, which is a 500."""
        dash = self._makeApp()
        self._seedPair(dash)

        resp = self._request(dash, "POST", "/admin/merge_review/reject",
                             data={"member": "B" * 22, "canonical": "Z" * 22})

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(dash.repo.getDismissedMergeCandidates()["total"], 0)

    def test_the_log_stays_out_of_the_way_until_there_is_something_in_it(self):
        dash = self._makeApp()
        self._seedPair(dash)

        body = self._request(dash, "GET", "/admin/merge-review").data.decode()

        self.assertNotIn("Kept separate", body)

    def test_an_undismissal_writes_through_and_reports_back(self):
        dash = self._makeApp()
        self._seedPair(dash)
        dash.repo.dismissMergeCandidate("B" * 22, decidedBy="alice")

        resp = self._request(dash, "POST", "/admin/merge_review/undismiss",
                             data={"member": "B" * 22})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/merge-review", resp.headers["Location"])
        self.assertIn("suggested again", unquote_plus(resp.headers["Location"]))
        #< and the pair is back in the queue the redirect lands on
        body = self._request(dash, "GET", "/admin/merge-review").data.decode()
        self.assertIn("Same recording", body)
        self.assertNotIn("Kept separate", body)

    def test_undismissing_something_nobody_dismissed_is_a_400(self):
        dash = self._makeApp()
        self._seedPair(dash)

        for member in ("B" * 22, "Z" * 22):
            self.assertEqual(
                self._request(dash, "POST", "/admin/merge_review/undismiss",
                              data={"member": member}).status_code, 400)

    def test_undismissing_is_admin_only(self):
        dash = self._makeApp()
        self._seedPair(dash)
        dash.repo.dismissMergeCandidate("B" * 22, decidedBy="alice")

        resp = self._request(dash, "POST", "/admin/merge_review/undismiss",
                             data={"member": "B" * 22}, isAdmin=False)

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(dash.repo.getDismissedMergeCandidates()["total"], 1)


if __name__ == "__main__":
    unittest.main()
