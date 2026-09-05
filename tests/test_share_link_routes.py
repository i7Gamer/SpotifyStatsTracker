"""Public Wrapped share links: creating/revoking a link from the
authenticated /wrapped and /profile routes, and the public, unauthenticated
GET /shared/<token> page and its image routes.
"""
import datetime
import unittest
from unittest.mock import patch, MagicMock

import sys
import os
from flask import Response

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
from app import SpotifyDashboardApp, RATE_LIMIT_MAX_ATTEMPTS, RATE_LIMIT_ERROR_MESSAGE
from _app_factory import AppTestCase
import Database.utils as utilsModule
from test_charts_genres import coverageDict
from conftest import wrappedCachedRow

#< what htmx puts on every request it makes; the /wrapped and /shared/<token>
#  filter swaps are marked by it rather than by the old ?ajax=true
HX_HEADERS = {"HX-Request": "true"}

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


def _ts(year, month=6, day=1, hour=12):
    """Unix timestamp (seconds) for a UTC datetime, matching test_wrapped_route.py."""
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp()


class ShareLinkRoutesTestCase(AppTestCase):
    """Freezes now()/tz like test_wrapped_route.py, since sharedWrappedPage()
    renders wrapped.html through the same _buildWrappedContext() pipeline."""

    def setUp(self):
        tzPatcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        tzPatcher.start()
        self.addCleanup(tzPatcher.stop)

        nowPatcher = patch.object(appModule, "now",
                                   return_value=datetime.datetime(2026, 7, 11, tzinfo=datetime.timezone.utc))
        nowPatcher.start()
        self.addCleanup(nowPatcher.stop)

        self.dash = self._makeApp()

    def _makeDb(self):
        db = MagicMock()
        db.tz = datetime.timezone.utc   #< profilePage()'s dateToString() needs a real tzinfo, not a MagicMock
        db.getEntriesFromOld.return_value = []
        db.getUserSpotifyCredentials.return_value = {}
        # _buildWrappedContext's only path since R6 (2026-09-02) reads
        # everything from the cache row - set individual list/total kwargs
        # (see wrappedCachedRow) rather than db.getTopSongs etc, which are
        # never called by that path anymore.
        db.repo.getCachedWrapped.return_value = wrappedCachedRow()
        return db

    def _loginAs(self, username, email, db=None):
        self.dash.repo.upsertUser(username, email)
        db = db or self._makeDb()
        patcher_login = patch.object(self.dash, 'is_user_logged_in', return_value=True)
        patcher_email = patch.object(self.dash, 'get_username_for_email', return_value=username)
        patcher_db = patch.object(self.dash, 'get_user_db', return_value=db)
        patcher_login.start()
        patcher_email.start()
        patcher_db.start()
        self.addCleanup(patcher_login.stop)
        self.addCleanup(patcher_email.stop)
        self.addCleanup(patcher_db.stop)

        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess['email'] = email
            sess['username'] = username
        return client


class TestCreateShareLink(ShareLinkRoutesTestCase):
    def test_creates_a_link_and_redirects_with_success(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wrapped", resp.headers["Location"])
        self.assertIn("openShareModal=1", resp.headers["Location"])
        links = self.dash.repo.getShareLinksForUser("alice")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["year"], 2026)
        self.assertIsNone(links[0]["expires_at"])

    def test_expiry_choice_is_stored(self):
        client = self._loginAs("alice", "alice@example.com")

        client.post("/wrapped/share-links/2026", data={"expiry": "7d"})

        link = self.dash.repo.getShareLinksForUser("alice")[0]
        self.assertIsNotNone(link["expires_at"])

    def test_unrecognized_expiry_value_is_rejected_instead_of_never_expiring(self):
        """An unknown expiry used to map to None, i.e. the most permissive
        option (a permanent, unauthenticated public link) - a typo'd or
        crafted POST must not silently produce one."""
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/wrapped/share-links/2026", data={"expiry": "forever-and-ever"})

        self.assertEqual(self.dash.repo.getShareLinksForUser("alice"), [])
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])

    def test_a_year_without_listening_data_is_rejected(self):
        """<int:year> bounds nothing and _buildWrappedContext does
        nowLocal.replace(year=year + 1): a hand-crafted POST for year 9999
        used to mint a link whose PUBLIC page 500'd on every visit (year
        10000 is out of datetime's range). The route now accepts only years
        the user has data for - the same set the share modal offers."""
        client = self._loginAs("alice", "alice@example.com")   #< _makeDb: data in 2026 only

        for badYear in (9999, 1, 1999):
            resp = client.post(f"/wrapped/share-links/{badYear}", data={"expiry": "never"})
            self.assertEqual(resp.status_code, 302)
            self.assertIn("error=", resp.headers["Location"])

        self.assertEqual(self.dash.repo.getShareLinksForUser("alice"), [])

    def test_a_year_without_listening_data_is_rejected_over_ajax_too(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/wrapped/share-links/9999?ajax=true", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())
        self.assertEqual(self.dash.repo.getShareLinksForUser("alice"), [])

    def test_an_all_years_link_needs_no_year_validation(self):
        """The path year is ignored for allYears links (linkYear is None), so
        the has-data check must not block them."""
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/wrapped/share-links/9999", data={"expiry": "never", "allYears": "1"})

        self.assertEqual(resp.status_code, 302)
        links = self.dash.repo.getShareLinksForUser("alice")
        self.assertEqual(len(links), 1)
        self.assertIsNone(links[0]["year"])

    def test_missing_expiry_field_still_defaults_to_never(self):
        """The form always posts one of the choices; an absent field is the
        existing default and must keep working."""
        client = self._loginAs("alice", "alice@example.com")

        client.post("/wrapped/share-links/2026", data={})

        links = self.dash.repo.getShareLinksForUser("alice")
        self.assertEqual(len(links), 1)
        self.assertIsNone(links[0]["expires_at"])

    def test_all_years_checkbox_creates_a_year_none_link(self):
        client = self._loginAs("alice", "alice@example.com")

        client.post("/wrapped/share-links/2026", data={"expiry": "never", "allYears": "1"})

        links = self.dash.repo.getShareLinksForUser("alice")
        self.assertEqual(len(links), 1)
        self.assertIsNone(links[0]["year"])

    def test_allyears_field_absent_still_creates_a_per_year_link(self):
        """Regression gate for existing per-year callers - the field is
        simply absent from their POSTs, so allYears must default false."""
        client = self._loginAs("alice", "alice@example.com")

        client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        links = self.dash.repo.getShareLinksForUser("alice")
        self.assertEqual(links[0]["year"], 2026)

    def test_disabled_feature_returns_404(self):
        self.dash.repo.setShareLinksEnabled(False)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.dash.repo.getShareLinksForUser("alice"), [])

    def test_anonymous_redirects_to_login(self):
        client = self.dash.app.test_client()

        resp = client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_rate_limited_after_max_attempts(self):
        """Spread across distinct years so the per-bucket cap (5) never
        blocks a create before the rate limiter (per source IP, independent
        of bucket) has a chance to trip at RATE_LIMIT_MAX_ATTEMPTS. The db
        declares data back to 2000 so every posted year passes the
        has-data-for-that-year validation."""
        db = self._makeDb()
        db.getEntriesFromOld.return_value = [{"playedAt": _ts(2000)}]
        client = self._loginAs("alice", "alice@example.com", db=db)
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            client.post(f"/wrapped/share-links/{2000 + i}", data={"expiry": "never"})

        resp = client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])
        self.assertIn("openShareModal=1", resp.headers["Location"])
        self.assertEqual(len(self.dash.repo.getShareLinksForUser("alice")), RATE_LIMIT_MAX_ATTEMPTS)

    def test_creating_a_second_link_for_the_same_year_succeeds(self):
        client = self._loginAs("alice", "alice@example.com")

        client.post("/wrapped/share-links/2026", data={"expiry": "never"})
        resp = client.post("/wrapped/share-links/2026", data={"expiry": "7d"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("success=", resp.headers["Location"])
        links = self.dash.repo.getShareLinksForUser("alice")
        self.assertEqual(len(links), 2)
        self.assertNotEqual(links[0]["token"], links[1]["token"])

    def test_sixth_link_for_the_same_year_is_rejected(self):
        client = self._loginAs("alice", "alice@example.com")
        for _ in range(5):
            client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        resp = client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])
        self.assertIn("openShareModal=1", resp.headers["Location"])
        self.assertEqual(len(self.dash.repo.getShareLinksForUser("alice")), 5)

    def test_cap_is_per_bucket_not_global(self):
        """5 links for one year plus 1 all-years link is 6 links total for
        the same user - the cap must be scoped per-bucket, not a global
        per-user total."""
        client = self._loginAs("alice", "alice@example.com")
        for _ in range(5):
            client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        resp = client.post("/wrapped/share-links/2026", data={"expiry": "never", "allYears": "1"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("success=", resp.headers["Location"])
        self.assertEqual(len(self.dash.repo.getShareLinksForUser("alice")), 6)


class TestCreateShareLinkAjax(ShareLinkRoutesTestCase):
    """The wrapped.html popup posts here with ?ajax=true so it can swap in
    the new link without leaving the page - see createWrappedShareLink()."""

    def test_ajax_create_returns_link_panel_html(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/wrapped/share-links/2026?ajax=true", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 200)
        html = resp.get_json()["html"]
        self.assertIn("Revoke", html)
        self.assertIn("Create Share Link", html)   #< always shown now, so a second link can be added
        token = self.dash.repo.getShareLinksForUser("alice")[0]["token"]
        self.assertIn(f"/shared/{token}", html)

    def test_ajax_rate_limited_returns_json_error(self):
        client = self._loginAs("alice", "alice@example.com")
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        resp = client.post("/wrapped/share-links/2026?ajax=true", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.get_json()["error"], RATE_LIMIT_ERROR_MESSAGE)

    def test_ajax_anonymous_returns_401(self):
        client = self.dash.app.test_client()

        resp = client.post("/wrapped/share-links/2026?ajax=true", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 401)
        self.assertIn("error", resp.get_json())

    def test_ajax_sixth_link_for_the_same_year_returns_400_with_error(self):
        client = self._loginAs("alice", "alice@example.com")
        for _ in range(5):
            client.post("/wrapped/share-links/2026?ajax=true", data={"expiry": "never"})

        resp = client.post("/wrapped/share-links/2026?ajax=true", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 400)
        self.assertIn("2026", resp.get_json()["error"])
        self.assertEqual(len(self.dash.repo.getShareLinksForUser("alice")), 5)


class TestRevokeShareLink(ShareLinkRoutesTestCase):
    def test_owner_can_revoke_and_redirects_with_success(self):
        client = self._loginAs("alice", "alice@example.com")
        token = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        linkId = self.dash.repo.getShareLink(token)["id"]

        resp = client.post(f"/profile/share-links/{linkId}")

        self.assertEqual(resp.status_code, 302)
        #< back to the panel that owns them now, not the profile page they
        #  used to be listed on
        self.assertIn("/wrapped", resp.headers["Location"])
        self.assertIsNone(self.dash.repo.getShareLink(token))

    def test_non_owner_cannot_revoke(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        linkId = self.dash.repo.getShareLink(token)["id"]
        client = self._loginAs("bob", "bob@example.com")

        resp = client.post(f"/profile/share-links/{linkId}")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])
        self.assertIsNotNone(self.dash.repo.getShareLink(token))

    def test_anonymous_redirects_to_login(self):
        client = self.dash.app.test_client()

        resp = client.post("/profile/share-links/1")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


class TestRevokeShareLinkAjax(ShareLinkRoutesTestCase):
    """The wrapped.html popup's Revoke form posts here with ?ajax=true and a
    year field so the response can render that year's create-form panel back
    - see profileShareLinkAction(). profile.html's own revoke form never sets
    ajax=true and is covered by TestRevokeShareLink above."""

    def test_ajax_revoke_returns_create_form_panel_html(self):
        client = self._loginAs("alice", "alice@example.com")
        token = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        linkId = self.dash.repo.getShareLink(token)["id"]

        resp = client.post(f"/profile/share-links/{linkId}?ajax=true", data={"year": "2026"})

        self.assertEqual(resp.status_code, 200)
        html = resp.get_json()["html"]
        self.assertIn("Create Share Link", html)
        self.assertNotIn("Revoke", html)
        self.assertIsNone(self.dash.repo.getShareLink(token))

    def test_ajax_non_owner_revoke_returns_403(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        linkId = self.dash.repo.getShareLink(token)["id"]
        client = self._loginAs("bob", "bob@example.com")

        resp = client.post(f"/profile/share-links/{linkId}?ajax=true", data={"year": "2026"})

        self.assertEqual(resp.status_code, 403)
        self.assertIn("error", resp.get_json())
        self.assertIsNotNone(self.dash.repo.getShareLink(token))

    def test_ajax_anonymous_returns_401(self):
        client = self.dash.app.test_client()

        resp = client.post("/profile/share-links/1?ajax=true", data={"year": "2026"})

        self.assertEqual(resp.status_code, 401)
        self.assertIn("error", resp.get_json())

    def test_revoking_the_all_years_link_falls_back_to_the_per_year_link(self):
        """Exercises the fix to profileShareLinkAction's ajax branch - it
        used to hardcode currentLink=None after any revoke, which was only
        correct while exactly one link type could exist per user."""
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        allYearsToken = self.dash.repo.createShareLink(
            "alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, None, None)
        allYearsId = self.dash.repo.getShareLink(allYearsToken)["id"]

        resp = client.post(f"/profile/share-links/{allYearsId}?ajax=true", data={"year": "2026"})

        self.assertEqual(resp.status_code, 200)
        html = resp.get_json()["html"]
        self.assertIn("Revoke", html)   #< falls back to showing the still-live 2026 link
        self.assertIsNone(self.dash.repo.getShareLink(allYearsToken))

    def test_revoking_one_of_several_links_for_the_same_year_removes_only_that_one(self):
        client = self._loginAs("alice", "alice@example.com")
        tokenA = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        tokenB = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        tokenC = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        middleId = self.dash.repo.getShareLink(tokenB)["id"]

        resp = client.post(f"/profile/share-links/{middleId}?ajax=true", data={"year": "2026"})

        self.assertEqual(resp.status_code, 200)
        html = resp.get_json()["html"]
        self.assertIn(f"/shared/{tokenA}", html)
        self.assertIn(f"/shared/{tokenC}", html)
        self.assertNotIn(f"/shared/{tokenB}", html)
        self.assertIn("Create Share Link", html)   #< 2 links remain, still under the cap
        self.assertIsNone(self.dash.repo.getShareLink(tokenB))
        self.assertIsNotNone(self.dash.repo.getShareLink(tokenA))
        self.assertIsNotNone(self.dash.repo.getShareLink(tokenC))


class TestOtherYearLinksInThePanel(ShareLinkRoutesTestCase):
    """Links scoped to a year other than the one on screen.

    The panel used to show only the current year's bucket plus the all-years
    bucket, so managing a 2024 link meant navigating to 2024 - which is why
    /profile carried a second, parallel list of every link. That list is gone;
    this group is what replaced it."""

    def _panel(self, body):
        start = body.index("data-other-year-links")
        return body[start:body.index("</details>", start)]

    def test_a_link_from_another_year_is_listed(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink(
            "alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2024, None)
        client = self._loginAs("alice", "alice@example.com")

        body = client.get("/wrapped?year=2026").data.decode()

        group = self._panel(body)
        self.assertIn("2024", group)
        self.assertRegex(group, rf'data-url="https?://[^"]*/shared/{token}"')

    def test_each_other_year_link_is_revocable(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink(
            "alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2024, None)
        linkId = self.dash.repo.getShareLink(token)["id"]
        client = self._loginAs("alice", "alice@example.com")

        group = self._panel(client.get("/wrapped?year=2026").data.decode())

        self.assertIn(f'action="/profile/share-links/{linkId}"', group)

    def test_group_is_absent_when_every_link_is_current_or_all_years(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, None, None)
        client = self._loginAs("alice", "alice@example.com")

        body = client.get("/wrapped?year=2026").data.decode()

        self.assertNotIn("data-other-year-links", body)

    def test_an_all_years_link_is_not_counted_as_another_year(self):
        """It has its own bucket and its own cap - listing it twice would
        imply revoking it there was a different action."""
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, None, None)
        client = self._loginAs("alice", "alice@example.com")

        body = client.get("/wrapped?year=2026").data.decode()

        self.assertIn('<span class="badge badge-secondary">All years</span>', body)
        self.assertNotIn("data-other-year-links", body)
        self.assertNotIn(">None<", body)

    def test_other_year_links_do_not_count_toward_this_years_cap(self):
        """The cap is per bucket. Five 2024 links must not block a 2026 one."""
        self.dash.repo.upsertUser("alice", "alice@example.com")
        for _ in range(appModule.SHARE_LINK_MAX_PER_BUCKET):
            self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2024, None)
        client = self._loginAs("alice", "alice@example.com")

        body = client.get("/wrapped?year=2026").data.decode()

        self.assertIn("Create Share Link", body)
        self.assertNotIn("reached the limit", body)

    def test_expired_other_year_link_does_not_appear(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareLink(
            "alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2024, expiresInSeconds=-10)
        client = self._loginAs("alice", "alice@example.com")

        body = client.get("/wrapped?year=2026").data.decode()

        self.assertNotIn("data-other-year-links", body)

    def test_several_other_years_are_all_listed(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        tokenA = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2023, None)
        tokenB = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2024, None)
        client = self._loginAs("alice", "alice@example.com")

        group = self._panel(client.get("/wrapped?year=2026").data.decode())

        self.assertRegex(group, rf'data-url="https?://[^"]*/shared/{tokenA}"')
        self.assertRegex(group, rf'data-url="https?://[^"]*/shared/{tokenB}"')

    def test_group_survives_an_htmx_year_switch(self):
        """The panel is re-rendered server-side on a year change (as an
        out-of-band swap keyed on #shareLinkPanelBody), so the group has to
        come through that path too - not just the full render."""
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2024, None)
        client = self._loginAs("alice", "alice@example.com")

        body = client.get("/wrapped?year=2026", headers=HX_HEADERS).get_data(as_text=True)

        self.assertIn("data-other-year-links", body)


class TestProfileNoLongerListsShareLinks(ShareLinkRoutesTestCase):
    def test_profile_has_no_share_links_section(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink(
            "alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        client = self._loginAs("alice", "alice@example.com")

        body = client.get("/profile").data.decode()

        self.assertNotIn("Wrapped Share Links", body)
        self.assertNotIn(token, body)

    def test_ajax_revoke_without_a_year_is_rejected_before_it_deletes(self):
        """The panel this branch re-renders builds a create-form action from
        `year` against an <int:year> rule, so a missing field used to raise a
        BuildError - a 500 for an action that had already destroyed the link."""
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink(
            "alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        linkId = self.dash.repo.getShareLink(token)["id"]
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post(f"/profile/share-links/{linkId}?ajax=true")

        self.assertEqual(resp.status_code, 400)
        self.assertIsNotNone(self.dash.repo.getShareLink(token))   #< still there

    def test_ajax_revoke_with_a_junk_year_is_rejected_too(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink(
            "alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        linkId = self.dash.repo.getShareLink(token)["id"]
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post(f"/profile/share-links/{linkId}?ajax=true", data={"year": "nonsense"})

        self.assertEqual(resp.status_code, 400)
        self.assertIsNotNone(self.dash.repo.getShareLink(token))

    def test_non_ajax_revoke_returns_to_wrapped(self):
        """The redirect used to land on /profile, which no longer shows them."""
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink(
            "alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        linkId = self.dash.repo.getShareLink(token)["id"]
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post(f"/profile/share-links/{linkId}")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wrapped", resp.headers["Location"])
        self.assertNotIn("/profile", resp.headers["Location"])


class PublicSharedWrappedTestCase(ShareLinkRoutesTestCase):
    def _createLink(self, username="alice", email="alice@example.com", year=2026, expiresInSeconds=None):
        self.dash.repo.upsertUser(username, email)
        return self.dash.repo.createShareLink(
            username, self.dash.repo.SHARE_LINK_KIND_WRAPPED, year, expiresInSeconds)

    def _getShared(self, token, db=None):
        client = self.dash.app.test_client()
        with patch.object(self.dash, '_getReadOnlyUserDb', return_value=db or self._makeDb()):
            return client.get(f"/shared/{token}")


class TestPublicSharedWrappedPage(PublicSharedWrappedTestCase):
    def test_valid_token_renders_200(self):
        token = self._createLink()

        resp = self._getShared(token)

        self.assertEqual(resp.status_code, 200)

    def test_unknown_token_404s(self):
        resp = self._getShared("does-not-exist")

        self.assertEqual(resp.status_code, 404)

    def test_disabled_feature_404s_even_for_a_valid_token(self):
        token = self._createLink()
        self.dash.repo.setShareLinksEnabled(False)

        resp = self._getShared(token)

        self.assertEqual(resp.status_code, 404)

    def test_expired_token_404s(self):
        # Negative expiresInSeconds -> already in the past at creation, no
        # time mocking needed (see test_repository_share_links.py for the
        # dedicated lazy-deletion coverage of getShareLink itself).
        token = self._createLink(expiresInSeconds=-10)

        resp = self._getShared(token)

        self.assertEqual(resp.status_code, 404)

    def test_revoked_token_404s(self):
        token = self._createLink()
        linkId = self.dash.repo.getShareLink(token)["id"]
        self.dash.repo.revokeShareLink(linkId, "alice")

        resp = self._getShared(token)

        self.assertEqual(resp.status_code, 404)

    def test_no_pii_in_public_response(self):
        token = self._createLink()

        resp = self._getShared(token)

        self.assertNotIn(b"alice@example.com", resp.data)

    def test_hero_title_uses_the_owners_username_not_your(self):
        token = self._createLink()

        resp = self._getShared(token)
        body = resp.data.decode()

        self.assertIn("<h1>alice&#39;s 2026 Wrapped</h1>", body)
        self.assertNotIn("Your 2026 Wrapped", body)

    def test_hero_subtitle_uses_the_owners_username(self):
        token = self._createLink()

        resp = self._getShared(token)
        body = resp.data.decode()

        self.assertIn("A look back at what alice listened to in 2026.", body)
        self.assertNotIn("what you listened to", body)

    def test_discovered_item_subtitles_use_the_owners_username(self):
        token = self._createLink()

        resp = self._getShared(token)
        body = resp.data.decode()

        self.assertIn("Songs alice first listened to in 2026.", body)
        self.assertIn("Artists alice first listened to in 2026.", body)
        self.assertIn("Albums alice first listened to in 2026.", body)
        self.assertNotIn("you first listened to", body)

    def test_a_history_of_only_future_plays_does_not_crash_a_multi_year_link(self):
        """_computeAvailableYears' range is empty when the earliest play is
        future-dated (imports can carry clock-skewed timestamps), and
        sharedWrappedPage indexes availableYears[0] - which was an
        IndexError -> 500 on a public URL. The helper now falls back to the
        current year and renders an empty Wrapped instead."""
        token = self._createLink(year=None)   #< multi-year: years come from the data
        db = self._makeDb()
        db.getEntriesFromOld.return_value = [{"playedAt": _ts(2027)}]   #< after the frozen 2026-07-11 'now'

        resp = self._getShared(token, db=db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"2026 Wrapped", resp.data)   #< the current-year fallback

    def test_track_card_you_played_line_uses_the_owners_username(self):
        token = self._createLink()
        db = self._makeDb()
        db.repo.getCachedWrapped.return_value = wrappedCachedRow(topArtists=[
            {"id": "a1", "name": "TestArtist", "plays": 5, "totalTimeListened": 5000, "uniqueSongCount": 3}
        ])

        resp = self._getShared(token, db=db)
        body = resp.data.decode()

        #< "songs": the artist count merges since 429f148, so the artist card
        #  says songs on Wrapped too; only the (per-release) album card still
        #  says "song releases" - see test_track_card_wrapped_release_caption.py
        self.assertIn("alice played 3 different songs by TestArtist", body)
        self.assertNotIn("You played", body)

    def test_track_card_played_lines_use_the_owners_display_name(self):
        """The artist/album card lines were the one public-page string still
        printing the raw username - which is the owner's email local-part
        (get_or_create_user derives it from email.split('@')[0]) - on a
        deliberately public URL, against sharedWrappedPage's 'No session, no
        nav, no PII' contract. Only the label changes: the raw username stays
        the /img/ cover path key (see wrapped.html's ownerLabel note)."""
        token = self._createLink()   #< upserts alice first; the name needs the row
        self.assertTrue(self.dash.repo.setDisplayName("alice", "Wonderland"))
        db = self._makeDb()
        db.repo.getCachedWrapped.return_value = wrappedCachedRow(
            topArtists=[
                {"id": "a1", "name": "TestArtist", "plays": 5, "totalTimeListened": 5000, "uniqueSongCount": 3}
            ],
            topAlbums=[
                {"id": "al1", "name": "TestAlbum", "plays": 4, "totalTimeListened": 4000, "uniqueSongCount": 2}
            ])

        resp = self._getShared(token, db=db)
        body = resp.data.decode()

        self.assertIn("Wonderland played 3 different songs by TestArtist", body)
        self.assertIn("Wonderland played 2 song releases from TestAlbum", body)
        self.assertNotIn("alice played", body)

    def test_the_filter_form_swaps_against_this_share_url(self):
        """The JS used to branch on isPublicView/shareOwnerName/fetchUrl from
        the data island to keep the hero text and the fetch target correct
        after a filter or year change. htmx takes the target from the form,
        and the hero comes back server-rendered - so what has to be pinned is
        that the form asks THIS token's page, not /wrapped (which an anonymous
        visitor cannot reach)."""
        token = self._createLink()

        body = self._getShared(token).data.decode()

        self.assertIn(f'hx-get="/shared/{token}"', body)
        self.assertIn("alice&#39;s 2026 Wrapped", body)

    def test_genre_locked_progress_uses_the_owners_username(self):
        token = self._createLink()
        self.dash.repo.setLastfmGenreBackfillEnabled(True)
        db = self._makeDb()
        db.getGenreCoverage.return_value = coverageDict(10, 10, 10)

        resp = self._getShared(token, db=db)
        body = resp.data.decode()

        self.assertIn("alice&#39;s listening history", body)
        self.assertNotIn("your listening history", body)

    def test_genre_locked_progress_pitches_a_key_for_the_owners_keyless_account(self):
        """2026-09-02 review, UT-12 follow-up: sharedWrappedPage renders the
        OWNER's db (patched in via _getReadOnlyUserDb, same as the test
        above), so the "add a key"/"no plays" split must follow the OWNER's
        Last.fm key, not the viewer (who has none - this route needs no
        session at all)."""
        token = self._createLink()
        self.dash.repo.setLastfmGenreBackfillEnabled(True)
        db = self._makeDb()   #< getGenreCoverage AND getLastfmWorkerStatus both bare MagicMocks

        resp = self._getShared(token, db=db)
        body = resp.data.decode()

        #< the public wording of that same branch: a visitor cannot add the
        #  owner's key, so it states the gap instead of pitching (L4, below)
        self.assertIn("has not connected a Last.fm API key", body)
        self.assertNotIn("No plays in this period yet.", body)

    def test_genre_locked_progress_shows_no_plays_for_the_owners_keyed_account(self):
        token = self._createLink()
        self.dash.repo.setLastfmGenreBackfillEnabled(True)
        db = self._makeDb()
        db.getLastfmWorkerStatus.return_value = {"configured": True, "running": True}

        resp = self._getShared(token, db=db)
        body = resp.data.decode()

        self.assertIn("No plays in this period yet.", body)
        self.assertNotIn("Add a Last.fm API key", body)

    def test_genre_locked_progress_names_the_owner_by_display_name(self):
        """Same rule as the artist/album card lines above, and the hero: the
        raw username is the owner's email local-part, and the public page names
        people by what they go by. One render otherwise showed both spellings -
        "Wonderland's 2026 Wrapped" in the hero and "enough of alice's listening
        history" two cards down."""
        token = self._createLink()
        self.assertTrue(self.dash.repo.setDisplayName("alice", "Wonderland"))
        self.dash.repo.setLastfmGenreBackfillEnabled(True)
        db = self._makeDb()
        db.getGenreCoverage.return_value = coverageDict(10, 10, 10)

        body = self._getShared(token, db=db).data.decode()

        self.assertIn("Wonderland&#39;s listening history", body)
        self.assertNotIn("alice&#39;s listening history", body)

    def test_the_keyless_pitch_never_links_a_visitor_to_the_owners_profile(self):
        """sharedWrappedPage passes suppressDetailLinks=True for exactly this
        rule, and this was the one internal link it did not cover: an anonymous
        visitor was invited to "add a Last.fm API key on alice's profile" -
        advice only the owner can act on, pointing at a login-gated page."""
        token = self._createLink()
        self.assertTrue(self.dash.repo.setDisplayName("alice", "Wonderland"))
        self.dash.repo.setLastfmGenreBackfillEnabled(True)
        db = self._makeDb()   #< no Last.fm key, so the pitch branch is the one taken

        body = self._getShared(token, db=db).data.decode()

        self.assertNotIn("/profile/connections", body)
        #< still SAYS what is missing, just without acting as if the visitor
        #  could fix it - and names the owner the way the rest of the page does
        self.assertIn("Wonderland", body)
        self.assertNotIn("alice&#39;s", body)

    def test_the_empty_genre_card_names_the_owner_by_display_name(self):
        """The third public-view branch that interpolated the raw key: the
        unlocked-but-empty state of the Top Genres card itself."""
        token = self._createLink()
        self.assertTrue(self.dash.repo.setDisplayName("alice", "Wonderland"))
        self.dash.repo.setLastfmGenreBackfillEnabled(True)
        db = self._makeDb()
        #< over the gate, so the card unlocks - but with no genres to show
        db.getGenreCoverage.return_value = coverageDict(100, 100, 100)
        db.getTopGenres.return_value = {}

        body = self._getShared(token, db=db).data.decode()

        self.assertIn("No genre data for Wonderland&#39;s", body)
        self.assertNotIn("No genre data for alice&#39;s", body)

    def test_track_card_images_use_the_token_keyed_image_route(self):
        """_track_card.html's imageBase override must actually take effect on
        the public page - otherwise cards would request /img/alice/... ,
        which 404s for an anonymous viewer (see serveTrackImage's own
        session-authorization check)."""
        token = self._createLink()
        db = self._makeDb()
        db.repo.getCachedWrapped.return_value = wrappedCachedRow(topSongs=[{
            "id": "song1", "name": "Song", "url": "u", "imageId": "img1", "duration": 0,
            "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album", "url": "u", "imageId": "img1", "imageUrl": "",
                       "totalTracks": 1, "releaseDate": 0},
            "artists": [], "plays": 5, "totalTimeListened": 5000, "firstListenedAt": 0,
        }])

        resp = self._getShared(token, db=db)
        body = resp.data.decode()

        self.assertIn(f'src="/shared/{token}/img/tracks/img1.jpeg"', body)
        self.assertNotIn('src="/img/alice/', body)

    def test_track_cards_link_to_spotify_not_authenticated_detail_pages(self):
        """An anonymous viewer has no session, so a /song/<id> link would just
        bounce them to /login - the public page has to fall through to the
        card's Spotify URL, the same way the Compare page's counterpart
        columns do (see _track_card.html's suppressDetailLinks)."""
        token = self._createLink()
        db = self._makeDb()
        db.repo.getCachedWrapped.return_value = wrappedCachedRow(topSongs=[{
            "id": "song1", "name": "Song", "url": "https://open.spotify.com/track/song1",
            "imageId": "img1", "duration": 0, "explicit": False, "isrc": "",
            "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album", "url": "u", "imageId": "img1", "imageUrl": "",
                       "totalTracks": 1, "releaseDate": 0},
            "artists": [], "plays": 5, "totalTimeListened": 5000, "firstListenedAt": 0,
        }])

        resp = self._getShared(token, db=db)
        body = resp.data.decode()

        self.assertNotIn('href="/song/song1"', body)
        self.assertIn('href="https://open.spotify.com/track/song1"', body)

    def test_artist_cards_link_to_spotify_not_authenticated_detail_pages(self):
        token = self._createLink()
        db = self._makeDb()
        db.repo.getCachedWrapped.return_value = wrappedCachedRow(topArtists=[{
            "id": "a1", "name": "TestArtist", "url": "https://open.spotify.com/artist/a1",
            "imageId": "img1", "imageUrl": "", "plays": 5, "totalTimeListened": 5000,
            "uniqueSongCount": 3, "firstListenedAt": 0,
        }])

        resp = self._getShared(token, db=db)
        body = resp.data.decode()

        self.assertNotIn('href="/artist/a1"', body)
        self.assertIn('href="https://open.spotify.com/artist/a1"', body)

    def test_noindex_header_present(self):
        token = self._createLink()

        resp = self._getShared(token)

        self.assertEqual(resp.headers.get("X-Robots-Tag"), "noindex")

    def test_no_authenticated_nav_export_or_share_controls(self):
        """The public page must use layout_public.html (no topbar/nav) and
        must never show the Export or Share button/modal - an anonymous
        visitor can't export the owner's summary card or create share links
        for someone else's data. Group by/Items per category/Sort by ARE
        shown (see TestPublicSharedWrappedPageFilters) - only year badges
        stay conditional on being a multi-year link."""
        token = self._createLink()

        resp = self._getShared(token)
        body = resp.data.decode()

        self.assertNotIn('id="nav-toggle"', body)
        self.assertNotIn('id="exportWrappedBtn"', body)
        self.assertNotIn('id="shareWrappedBtn"', body)
        self.assertNotIn('id="shareLinkModal"', body)

    def test_filter_controls_shown_on_a_single_year_shared_page(self):
        token = self._createLink()

        resp = self._getShared(token)
        body = resp.data.decode()

        self.assertIn('id="groupBy"', body)
        self.assertIn('id="limit"', body)
        self.assertIn('id="sortBy"', body)

    def test_year_badges_hidden_on_a_single_year_shared_page(self):
        token = self._createLink()

        resp = self._getShared(token)
        body = resp.data.decode()

        self.assertNotIn('class="wrapped-year-badges"', body)

    def test_no_share_panel_on_the_public_page_itself(self):
        token = self._createLink()

        resp = self._getShared(token)

        self.assertNotIn(b"Share this Wrapped", resp.data)

    def test_repeated_valid_visits_are_not_rate_limited(self):
        token = self._createLink()

        for _ in range(RATE_LIMIT_MAX_ATTEMPTS + 5):
            resp = self._getShared(token)
            self.assertEqual(resp.status_code, 200)

    def test_unknown_token_misses_are_rate_limited(self):
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            self._getShared(f"unknown-{i}")

        resp = self._getShared("one-more-unknown")

        self.assertEqual(resp.status_code, 429)


class TestPublicSharedWrappedPageMultiYear(PublicSharedWrappedTestCase):
    """An "all years" link (year=None) is year-switchable, like the
    authenticated page - a per-year link stays pinned (see
    test_year_query_param_is_ignored_for_a_single_year_link below)."""

    def test_defaults_to_the_most_recent_available_year(self):
        token = self._createLink(year=None)
        db = self._makeDb()
        db.getEntriesFromOld.return_value = [{"playedAt": _ts(2023)}]

        resp = self._getShared(token, db=db)
        body = resp.data.decode()

        self.assertIn("alice&#39;s 2026 Wrapped", body)   #< now() is frozen at 2026-07-11 in setUp

    def test_year_query_param_switches_the_shown_year(self):
        token = self._createLink(year=None)
        db = self._makeDb()
        db.getEntriesFromOld.return_value = [{"playedAt": _ts(2023)}]

        resp = self._getShared(f"{token}?year=2024", db=db)
        body = resp.data.decode()

        self.assertIn("alice&#39;s 2024 Wrapped", body)

    def test_year_query_param_outside_available_range_falls_back_to_default(self):
        token = self._createLink(year=None)
        db = self._makeDb()
        db.getEntriesFromOld.return_value = [{"playedAt": _ts(2023)}]

        resp = self._getShared(f"{token}?year=1999", db=db)
        body = resp.data.decode()

        self.assertIn("alice&#39;s 2026 Wrapped", body)

    def test_year_badges_visible_for_a_multi_year_link(self):
        token = self._createLink(year=None)
        db = self._makeDb()
        db.getEntriesFromOld.return_value = [{"playedAt": _ts(2023)}]

        resp = self._getShared(token, db=db)
        body = resp.data.decode()

        self.assertIn('class="wrapped-year-badges"', body)
        for year in (2023, 2024, 2025, 2026):
            self.assertIn(f">{year}<", body)

    def test_year_query_param_is_ignored_for_a_single_year_link(self):
        """The tamper regression: a per-year link must not let a visitor
        browse a different year of the same user's data via the URL."""
        token = self._createLink(year=2025)
        db = self._makeDb()
        db.getEntriesFromOld.return_value = [{"playedAt": _ts(2023)}]

        resp = self._getShared(f"{token}?year=2026", db=db)
        body = resp.data.decode()

        self.assertIn("alice&#39;s 2025 Wrapped", body)
        self.assertNotIn('class="wrapped-year-badges"', body)


class TestSharedWrappedPageSwap(PublicSharedWrappedTestCase):
    """The public page's filter/year swap. It used to be ?ajax=true answering
    with {"topSongsHtml": ...}; it is now the HX-Request fragment
    (_wrapped_results.html). The transport itself is pinned by
    tests/test_wrapped_htmx.py - what stays here is that the swapped CARDS
    still speak to an anonymous viewer the way the full render does."""

    def _getSharedSwap(self, token, query="", db=None):
        client = self.dash.app.test_client()
        with patch.object(self.dash, '_getReadOnlyUserDb', return_value=db or self._makeDb()):
            return client.get(f"/shared/{token}{query}", headers=HX_HEADERS)

    def test_a_swap_returns_the_recap_fragment(self):
        token = self._createLink(year=2026)

        resp = self._getSharedSwap(token, query="?groupBy=month&limit=25&sortBy=name")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('data-category="top-songs"', body)
        self.assertIn('id="timeSeriesChart"', body)

    def test_invalid_groupby_limit_sortby_fall_back_same_as_wrappedpage(self):
        token = self._createLink(year=2026)

        resp = self._getSharedSwap(token, query="?groupBy=bogus&limit=999999&sortBy=bogus")

        self.assertEqual(resp.status_code, 200)   #< falls back to defaults rather than erroring

    def test_swapped_cards_name_the_owner_instead_of_saying_you(self):
        token = self._createLink(year=2026)
        db = self._makeDb()
        db.repo.getCachedWrapped.return_value = wrappedCachedRow(topArtists=[
            {"id": "a1", "name": "TestArtist", "plays": 5, "totalTimeListened": 5000, "uniqueSongCount": 3}
        ])

        body = self._getSharedSwap(token, db=db).get_data(as_text=True)

        self.assertIn("alice played 3 different songs by TestArtist", body)
        self.assertNotIn("You played", body)

    def test_swapped_cards_use_the_owners_display_name(self):
        """The fragment renders the same _track_card.html lines, so a
        year/filter swap must not regress the full render's display-name fix
        back to the raw username."""
        token = self._createLink(year=2026)   #< upserts alice first; the name needs the row
        self.assertTrue(self.dash.repo.setDisplayName("alice", "Wonderland"))
        db = self._makeDb()
        db.repo.getCachedWrapped.return_value = wrappedCachedRow(topArtists=[
            {"id": "a1", "name": "TestArtist", "plays": 5, "totalTimeListened": 5000, "uniqueSongCount": 3}
        ])

        body = self._getSharedSwap(token, db=db).get_data(as_text=True)

        self.assertIn("Wonderland played 3 different songs by TestArtist", body)
        self.assertNotIn("alice played", body)

    def test_year_switch_on_a_multi_year_link_stays_within_available_years(self):
        token = self._createLink(year=None)
        db = self._makeDb()
        db.getEntriesFromOld.return_value = [{"playedAt": _ts(2023)}]

        resp = self._getSharedSwap(token, query="?year=2024", db=db)

        self.assertEqual(resp.status_code, 200)

    def test_year_tampering_is_ignored_for_a_single_year_link(self):
        token = self._createLink(year=2025)
        db = self._makeDb()
        db.repo.getCachedWrapped.return_value = wrappedCachedRow(topSongs=[
            {"id": "s1", "name": "OnlyIn2025", "plays": 1, "duration": 0, "artists": []}
        ])

        body = self._getSharedSwap(token, query="?year=2026", db=db).get_data(as_text=True)

        self.assertIn("OnlyIn2025", body)

    def test_swapped_cards_use_the_token_keyed_image_route(self):
        """The full render passes imageBase=/shared/<token>/img; the fragment
        has to pass it too, or every cover in a re-sorted/resized list falls
        back to /img/<owner>/... , which 404s without a session."""
        token = self._createLink(year=2026)
        db = self._makeDb()
        db.repo.getCachedWrapped.return_value = wrappedCachedRow(topSongs=[{
            "id": "song1", "name": "Song", "url": "u", "imageId": "img1", "duration": 0,
            "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album", "url": "u", "imageId": "img1", "imageUrl": "",
                       "totalTracks": 1, "releaseDate": 0},
            "artists": [], "plays": 5, "totalTimeListened": 5000, "firstListenedAt": 0,
        }])

        body = self._getSharedSwap(token, db=db).get_data(as_text=True)

        self.assertIn(f'src="/shared/{token}/img/tracks/img1.jpeg"', body)
        self.assertNotIn('src="/img/alice/', body)

    def test_swapped_cards_link_to_spotify_not_authenticated_detail_pages(self):
        token = self._createLink(year=2026)
        db = self._makeDb()
        db.repo.getCachedWrapped.return_value = wrappedCachedRow(topArtists=[{
            "id": "a1", "name": "TestArtist", "url": "https://open.spotify.com/artist/a1",
            "imageId": "img1", "imageUrl": "", "plays": 5, "totalTimeListened": 5000,
            "uniqueSongCount": 3, "firstListenedAt": 0,
        }])

        body = self._getSharedSwap(token, db=db).get_data(as_text=True)

        self.assertNotIn('href="/artist/a1"', body)
        self.assertIn('href="https://open.spotify.com/artist/a1"', body)

    def test_a_swap_never_includes_a_share_panel(self):
        """Safety regression: an anonymous visitor must never receive
        share-panel data (create-link forms, existing tokens) for the
        owner's account, even though the authenticated page's swap refreshes
        exactly that region out of band."""
        token = self._createLink(year=2026)

        body = self._getSharedSwap(token).get_data(as_text=True)

        self.assertNotIn("shareLinkPanelBody", body)
        self.assertNotIn("Share this Wrapped", body)


class TestShareLinkPanelOnWrappedPage(ShareLinkRoutesTestCase):
    """The owner-only 'Share this Wrapped' panel on the authenticated
    /wrapped page - not to be confused with the public page itself."""

    def test_shows_create_form_when_no_link_exists_for_the_year(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")
        body = resp.data.decode()

        self.assertIn("Share this Wrapped", body)
        self.assertIn('action="/wrapped/share-links/2026"', body)
        self.assertNotIn("Revoke", body)

    def test_shows_existing_link_and_revoke_when_one_exists_for_the_year(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")
        body = resp.data.decode()

        self.assertIn(f"/shared/{token}", body)
        self.assertIn("Revoke", body)
        self.assertIn("Create Share Link", body)   #< always shown now, so a second link can be added

    def test_a_different_years_link_does_not_show_as_the_current_one(self):
        """It is still listed - in the other-years group below (see
        TestOtherYearLinksInThePanel) - but must not be mistaken for a link
        covering the year on screen, and must not suppress the create form."""
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2025, None)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")
        body = resp.data.decode()

        self.assertIn("Create Share Link", body)
        #< the current-year list is what precedes the other-years group
        currentSection = body[:body.index("data-other-year-links")]
        self.assertNotIn("Revoke", currentSection)

    def test_all_years_link_and_a_per_year_link_both_show_together(self):
        """An all-years link no longer hides a same-year link in the panel -
        both are still-active, independently useful grants, so both are
        listed."""
        self.dash.repo.upsertUser("alice", "alice@example.com")
        perYearToken = self.dash.repo.createShareLink(
            "alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        allYearsToken = self.dash.repo.createShareLink(
            "alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, None, None)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")
        body = resp.data.decode()

        self.assertIn(f"/shared/{allYearsToken}", body)
        self.assertIn(f"/shared/{perYearToken}", body)

    def test_creating_a_redundant_per_year_link_still_shows_the_all_years_link(self):
        """Exercises why createWrappedShareLink's ajax branch needs the full
        _resolveShareLinksForYear re-scan rather than echoing back the row it
        just inserted."""
        self.dash.repo.upsertUser("alice", "alice@example.com")
        allYearsToken = self.dash.repo.createShareLink(
            "alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, None, None)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/wrapped/share-links/2026?ajax=true", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 200)
        html = resp.get_json()["html"]
        self.assertIn(f"/shared/{allYearsToken}", html)

    def test_panel_hidden_when_feature_disabled(self):
        self.dash.repo.setShareLinksEnabled(False)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")

        self.assertNotIn(b"Share this Wrapped", resp.data)
        self.assertNotIn(b'id="shareWrappedBtn"', resp.data)

    def test_share_button_sits_next_to_the_export_button(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")
        body = resp.data.decode()

        self.assertIn('id="shareWrappedBtn"', body)
        self.assertIn('id="exportWrappedBtn"', body)

    def test_modal_closed_by_default(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")
        body = resp.data.decode()

        self.assertIn('id="shareLinkModal" class="share-modal-overlay" style="display: none;"', body)

    def test_modal_open_when_openShareModal_param_present(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026&openShareModal=1")
        body = resp.data.decode()

        self.assertIn('id="shareLinkModal" class="share-modal-overlay" style="display: flex;"', body)

    def test_a_year_switch_refreshes_the_share_panel_for_the_new_year(self):
        """Regression: the share modal's panel used to stay keyed to
        whichever year the page last fully rendered with, since the
        AJAX year-switch never touched #shareLinkPanelBody - reported after
        switching years and finding the modal still offered/showed the
        previous year's link state. It now rides the swap out of band."""
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2025, None)
        db = self._makeDb()
        db.getEntriesFromOld.return_value = [{"playedAt": 0, "timePlayed": 1}]   #< makes 2025 a valid available year
        client = self._loginAs("alice", "alice@example.com", db=db)

        resp = client.get("/wrapped?year=2025", headers=HX_HEADERS)

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        panelHtml = body[body.index('id="shareLinkPanelBody"'):]
        self.assertIn(f"/shared/{token}", panelHtml)
        self.assertIn("Revoke", panelHtml)

    def test_the_swapped_share_panel_reflects_a_year_with_no_link_yet(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2025, None)
        client = self._loginAs("alice", "alice@example.com")

        body = client.get("/wrapped?year=2026", headers=HX_HEADERS).get_data(as_text=True)

        panelHtml = body[body.index('id="shareLinkPanelBody"'):]
        self.assertIn("Create Share Link", panelHtml)
        #< the 2025 link is still listed, but in the other-years group - what
        #  must not appear is a revoke for the year on screen
        currentSection = panelHtml[panelHtml.index("</details>"):]
        self.assertNotIn("Revoke", currentSection)

    def test_the_swapped_share_panel_is_absent_when_the_feature_is_disabled(self):
        """With share links off there is no #shareLinkPanelBody on the page,
        and an out-of-band swap for an element that isn't there is an
        htmx:oobErrorNoTarget rather than a no-op."""
        self.dash.repo.setShareLinksEnabled(False)
        client = self._loginAs("alice", "alice@example.com")

        body = client.get("/wrapped?year=2026", headers=HX_HEADERS).get_data(as_text=True)

        self.assertNotIn("shareLinkPanelBody", body)

    def test_panel_lists_multiple_links_for_the_same_year(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        tokenA = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        tokenB = self.dash.repo.createShareLink(
            "alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, 7 * 24 * 3600)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")
        body = resp.data.decode()

        self.assertIn(f"/shared/{tokenA}", body)
        self.assertIn(f"/shared/{tokenB}", body)
        self.assertEqual(body.count('class="share-link-revoke-form"'), 2)
        self.assertIn("Never expires", body)
        self.assertIn("Expires in 7 days", body)

    def test_create_form_still_shown_with_warning_when_only_one_bucket_at_cap(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        for _ in range(5):
            self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")
        body = resp.data.decode()

        self.assertIn("Create Share Link", body)
        self.assertIn("already have 5 active links for 2026", body)

    def test_create_form_hidden_when_both_buckets_at_cap(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        for _ in range(5):
            self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
            self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, None, None)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")
        body = resp.data.decode()

        self.assertNotIn("Create Share Link", body)
        self.assertIn("reached the limit of 5 links for 2026 and 5 for all years", body)


class TestSharedImageRoutes(ShareLinkRoutesTestCase):
    def _createLink(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        return self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)

    def _seedPlayedArtist(self, artistId, username="alice"):
        """One real play crediting `artistId`, so the owner-scoping check on the
        lazy artist-image fetch treats it as an id this owner actually played."""
        self.dash.repo.upsertTrack({
            "id": f"track-{artistId}", "name": "Song", "url": "u",
            "artists": [{"id": artistId, "name": "Artist", "url": "u", "imageUrl": "", "imageId": artistId}],
            "album": {"id": "alb1", "name": "Album", "url": "u", "imageId": "alb1", "imageUrl": "",
                       "totalTracks": 1, "releaseDate": 0},
            "imageUrl": "", "imageId": "alb1", "duration": 200000, "explicit": False,
            "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
        })
        self.dash.repo.insertPlay(username, f"track-{artistId}", 1000.0, 200000)
        self.dash.repo.commit()

    @patch('routes.wrapped.sendCacheableImage')
    def test_valid_token_serves_track_image(self, mock_send):
        mock_send.return_value = Response("OK")
        token = self._createLink()
        client = self.dash.app.test_client()

        resp = client.get(f"/shared/{token}/img/tracks/abc.jpeg")

        self.assertEqual(resp.status_code, 200)
        mock_send.assert_called_once()
        self.assertEqual(resp.headers.get("X-Robots-Tag"), "noindex")

    def test_unknown_token_404s_for_track_image(self):
        client = self.dash.app.test_client()

        resp = client.get("/shared/does-not-exist/img/tracks/abc.jpeg")

        self.assertEqual(resp.status_code, 404)

    def test_path_traversal_filename_404s(self):
        token = self._createLink()
        client = self.dash.app.test_client()

        resp = client.get(f"/shared/{token}/img/tracks/..%5C..%5Csecret.txt")

        self.assertEqual(resp.status_code, 404)

    @patch('routes.wrapped.sendCacheableImage')
    @patch('routes.wrapped.os.path.exists', return_value=False)
    def test_valid_token_lazily_fetches_missing_artist_image(self, mock_exists, mock_send):
        mock_send.return_value = Response("OK")
        token = self._createLink()
        self._seedPlayedArtist("art1")
        readOnlyDb = self._makeDb()
        client = self.dash.app.test_client()

        with patch.object(self.dash, '_getReadOnlyUserDb', return_value=readOnlyDb):
            resp = client.get(f"/shared/{token}/img/artists/art1.jpeg")

        self.assertEqual(resp.status_code, 200)
        readOnlyDb.lazyFetchArtistImage.assert_called_once()
        self.assertEqual(readOnlyDb.lazyFetchArtistImage.call_args.args[0], "art1")

    @patch('routes.wrapped.sendCacheableImage')
    @patch('routes.wrapped.os.path.exists', return_value=False)
    def test_artist_image_is_not_fetched_for_an_id_the_owner_never_played(self, mock_exists, mock_send):
        """A share token must not become an open proxy for Spotify lookups on
        the owner's credentials: walking arbitrary artist ids through this
        route would insert an images row and dispatch an authenticated fetch
        per id, unauthenticated and unthrottled."""
        mock_send.return_value = Response("OK")
        token = self._createLink()
        self._seedPlayedArtist("art1")
        readOnlyDb = self._makeDb()
        client = self.dash.app.test_client()

        with patch.object(self.dash, '_getReadOnlyUserDb', return_value=readOnlyDb):
            resp = client.get(f"/shared/{token}/img/artists/notplayed99.jpeg")

        self.assertEqual(resp.status_code, 200)   #< still serves/404s the file itself, just never fetches
        readOnlyDb.lazyFetchArtistImage.assert_not_called()

    @patch('routes.wrapped.sendCacheableImage')
    def test_disabled_feature_404s_image_routes(self, mock_send):
        """The kill switch has to cover the image routes too - otherwise an
        admin turning share links off leaves the artwork of every shared page
        still fetchable by token."""
        mock_send.return_value = Response("OK")
        token = self._createLink()
        self.dash.repo.setShareLinksEnabled(False)
        client = self.dash.app.test_client()

        trackResp = client.get(f"/shared/{token}/img/tracks/abc.jpeg")
        artistResp = client.get(f"/shared/{token}/img/artists/art1.jpeg")

        self.assertEqual(trackResp.status_code, 404)
        self.assertEqual(artistResp.status_code, 404)

    def test_unknown_token_image_misses_are_rate_limited(self):
        """Same anti-guessing throttle the page route applies - aiming the
        guesses at an image URL instead must not bypass it."""
        client = self.dash.app.test_client()

        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.assertEqual(client.get("/shared/guess/img/tracks/abc.jpeg").status_code, 404)

        resp = client.get("/shared/guess/img/tracks/abc.jpeg")

        self.assertEqual(resp.status_code, 429)

    @patch('routes.wrapped.sendCacheableImage')
    def test_repeated_valid_image_requests_are_not_rate_limited(self, mock_send):
        """A single shared page pulls dozens of images through these routes -
        only unknown-token misses may count against the limit."""
        mock_send.return_value = Response("OK")
        token = self._createLink()
        client = self.dash.app.test_client()

        for _ in range(RATE_LIMIT_MAX_ATTEMPTS + 5):
            resp = client.get(f"/shared/{token}/img/tracks/abc.jpeg")

        self.assertEqual(resp.status_code, 200)


class TestActivationGuardViaPublicRoute(ShareLinkRoutesTestCase):
    """The end-to-end version of the activation-guard tests in
    test_read_only_user_db.py, driven through the actual HTTP routes: a
    public share-link view for a "cold" username must never activate the
    listener, but the owner's next real login must activate that exact same
    cached instance rather than being silently skipped."""

    def _makeMockDb(self, *a, **k):
        return self._makeDb()

    def test_cold_username_view_then_real_login_activates_once(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")

        with patch('dashboard.user_registry.Database', side_effect=self._makeMockDb):
            token = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
            client = self.dash.app.test_client()

            sharedResp = client.get(f"/shared/{token}")
            self.assertEqual(sharedResp.status_code, 200)

            coldDb = self.dash.user_databases["alice"]
            coldDb.startAutoImporter.assert_not_called()
            coldDb.resetProgress.assert_not_called()
            coldDb.startListener.assert_not_called()

            with patch.object(self.dash, 'is_user_logged_in', return_value=True), \
                 patch.object(self.dash, 'get_username_for_email', return_value='alice'):
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
                loginResp = client.get("/wrapped")

        self.assertEqual(loginResp.status_code, 200)
        activatedDb = self.dash.user_databases["alice"]
        self.assertIs(activatedDb, coldDb)   #< same object, never reconstructed
        coldDb.startAutoImporter.assert_called_once()
        coldDb.resetProgress.assert_called_once()
        coldDb.startListener.assert_called_once_with(email="alice@example.com")
        self.assertIn("alice", self.dash._activatedUsers)


if __name__ == "__main__":
    unittest.main()
