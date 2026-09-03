# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The template context every render gets: the topbar badges, the nav gates,
the instance-wide toggles and a few constants the templates compare against.

Registered through `register(app, dashboard)` in the routes/<domain> shape, so
app.py's registerRoutes reads as the list of things it installs rather than
carrying these fourteen closures in its middle.

Every processor re-runs on every render, and one request can render a dozen
templates (the Wrapped AJAX endpoint alone renders six partials), so anything
that reads the database is memoized on flask.g for the request - through
_memoizedSetting for the instance-wide toggles and _memoized for the per-user
reads. The per-render toggle reads themselves are a settled decision
(documented cheap single-row reads); this module only moves them.
"""
import os

from flask import g, session

from Database.db import SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON
from config import (
    PASSWORD_MIN_LENGTH, MAX_INLINE_ARTISTS, MIN_HIDDEN_ARTISTS,
    PLACEHOLDER_IMG_DATA_URI, SPOTIFY_CALLBACK_URL_ENV_VAR,
)

#< where _memoizedSetting parks a toggle on g, kept apart from the bare
#  per-user keys (isAdmin, unseenMilestoneCount, ...) that a route may prime
SETTING_MEMO_PREFIX = "_setting_"


def _memoized(key: str, read):
    """One `read()` per request, parked on g under `key`.

    The key is the bare attribute name on purpose: a route that needs the
    value BEFORE the render can set it itself (primeMilestoneBadge in app.py
    freezes unseenMilestoneCount before the dashboard clears it), and this
    then replaces the read rather than adding one."""
    if key not in g:
        setattr(g, key, read())
    return getattr(g, key)


def _memoizedSetting(name: str, read):
    """An instance-wide settings read, memoized on g for the current request.

    An un-memoized "cheap settings read" is really one app_settings SELECT per
    setting per partial - see the module docstring for the render fan-out."""
    return _memoized(f"{SETTING_MEMO_PREFIX}{name}", read)


def register(app, dashboard) -> None:
    """Install every context processor on `app`. `dashboard` is the
    SpotifyDashboardApp; its repo is looked up at call time, never captured."""

    @app.context_processor
    def _injectPasswordPolicy():
        # Lets register.html/reset_password.html show the actual configured
        # minimum instead of a hardcoded number that could drift from
        # PASSWORD_MIN_LENGTH.
        return {"minPasswordLength": PASSWORD_MIN_LENGTH}

    @app.context_processor
    def _injectFallbackMarkers():
        # Single-sources the created_reason marker values (Database.db) so
        # templates compare against the constants instead of duplicating the
        # string literals.
        return {
            "SYNTHETIC_FALLBACK_REASON": SYNTHETIC_FALLBACK_REASON,
            "RESTRICTED_FALLBACK_REASON": RESTRICTED_FALLBACK_REASON,
        }

    @app.context_processor
    def _injectArtistListLimits():
        # _artist_links.html collapses long artist lists behind a
        # "+N more" toggle - single-sources the thresholds so the macro
        # compares against the constants instead of magic numbers.
        return {
            "MAX_INLINE_ARTISTS": MAX_INLINE_ARTISTS,
            "MIN_HIDDEN_ARTISTS": MIN_HIDDEN_ARTISTS,
        }

    @app.context_processor
    def _injectPlaceholderImage():
        # Single-sources the placeholder cover/artist-image data URI (see
        # config.py's PLACEHOLDER_IMG_DATA_URI) so layout.html/
        # layout_public.html's window.PLACEHOLDER_IMG and _track_card.html's
        # inline fallback src read the same literal instead of three
        # hand-kept copies.
        return {"placeholderImgDataUri": PLACEHOLDER_IMG_DATA_URI}

    @app.context_processor
    def _injectAdminStatus():
        # Lets templates show admin-only affordances (the profile page's
        # ADMIN chip).
        username = session.get("username")
        return {"isAdmin": _memoized(
            "isAdmin", lambda: dashboard.repo.isAdmin(username) if username else False)}

    @app.context_processor
    def _injectRegistrationStatus():
        # Lets login.html hide its "Create an account" link when the
        # admin has disabled new registrations.
        return {"registration_enabled": _memoizedSetting(
            "registration_enabled", dashboard.repo.isRegistrationEnabled)}

    @app.context_processor
    def _injectShareLinksStatus():
        # Lets wrapped.html hide its "Share this Wrapped" panel and
        # profile.html hide its share-link list when the admin has
        # disabled public share links.
        return {"share_links_enabled": _memoizedSetting(
            "share_links_enabled", dashboard.repo.isShareLinksEnabled)}

    @app.context_processor
    def _injectArtistBioStatus():
        # Lets artist_detail.html hide its Biography section (even for
        # an artist whose bio was already fetched and stored) and
        # overview.html's admin panel show the toggle's current state.
        return {"artist_bio_enabled": _memoizedSetting(
            "artist_bio_enabled", dashboard.repo.isArtistBioEnabled)}

    @app.context_processor
    def _injectAlbumBioStatus():
        # Mirrors _injectArtistBioStatus, for album_detail.html's
        # Biography section and the album_bio toggle's current state.
        return {"album_bio_enabled": _memoizedSetting(
            "album_bio_enabled", dashboard.repo.isAlbumBioEnabled)}

    @app.context_processor
    def _injectLastfmGenreStatus():
        # Lets layout.html's nav show the "Genres" link only when the
        # admin's instance-wide Last.fm genre backfill is enabled - the
        # same kill switch the Charts genre section already respects, so
        # the nav never advertises a page whose entire content is off.
        return {"lastfm_genre_enabled": _memoizedSetting(
            "lastfm_genre_enabled", dashboard.repo.isLastfmGenreBackfillEnabled)}

    @app.context_processor
    def _injectTagsStatus():
        # Lets layout.html hide the "Playlists" nav link, _page_card.html
        # hide the Top Songs/Artists/Albums tag filter, and the detail
        # pages hide the tag panel when the admin's instance-wide tags
        # kill switch is off.
        return {"tags_enabled": _memoizedSetting("tags_enabled", dashboard.repo.isTagsEnabled)}

    @app.context_processor
    def _injectTagsPanelStatus():
        # Per-user "hide the tag panel" preference (set on /profile),
        # independent of the admin-wide tags_enabled switch above - lets
        # song/artist/album detail pages skip _tag_widget.html for a user
        # who just doesn't want to see it.
        username = session.get("username")
        return {"hide_tags_panel": _memoized(
            "hideTagsPanel", lambda: dashboard.repo.getHideTagsPanel(username) if username else False)}

    @app.context_processor
    def _injectShareStatus():
        # Lets layout.html's nav show a "Compare" link only for users who
        # have at least one usable accepted share, and the topbar badges
        # show a count of share requests waiting on them plus a count of
        # their own requests that were just accepted - computed here so
        # every template gets all three without every route remembering
        # to pass them. Three values under one guard, so this stays a
        # hand-rolled memo rather than three _memoized calls that would
        # each re-check the kill switch. No is_user_logged_in check: that
        # can cost a live Spotify round-trip, far too heavy per render, and
        # a stale session's worst case is a nav link/badge that 302s to
        # login like every other nav item would.
        if "hasAcceptedShares" not in g:
            username = session.get("username")
            # The admin's instance-wide kill switch zeroes all three
            # instead of skipping the queries below it - disabled means
            # the nav link and both badges hide, not that a real pending/
            # accepted share stops existing in the DB.
            if dashboard.repo.isDataSharingEnabled():
                g.hasAcceptedShares = dashboard.repo.hasAnyAcceptedShare(username) if username else False
                g.pendingIncomingSharesCount = dashboard.repo.getPendingIncomingSharesCount(username) if username else 0
                g.unseenAcceptedShareCount = dashboard.repo.getUnseenAcceptedShareCount(username) if username else 0
            else:
                g.hasAcceptedShares = False
                g.pendingIncomingSharesCount = 0
                g.unseenAcceptedShareCount = 0
        return {
            "hasAcceptedShares": g.hasAcceptedShares,
            "pendingIncomingSharesCount": g.pendingIncomingSharesCount,
            "unseenAcceptedShareCount": g.unseenAcceptedShareCount,
        }

    @app.context_processor
    def _injectSpotifyReauthStatus():
        # Topbar badge for "Web API backfill is stuck because the stored
        # Spotify authorization is missing a scope" (see
        # Listener.on_scope_status_change) - otherwise the only place
        # this ever surfaces is the Connection Status card on /profile,
        # which nothing prompts a user to go check. Gated on
        # SPOTIFY_CALLBACK_URL like every other Spotify Developer API
        # route/link: with it unset, /spotify-authorize 404s, so a badge
        # pointing there would be a dead end.
        username = session.get("username")
        return {"spotifyNeedsReauthBadge": _memoized("spotifyNeedsReauthBadge", lambda: (
            bool(os.environ.get(SPOTIFY_CALLBACK_URL_ENV_VAR))
            and bool(username)
            and dashboard.repo.getSpotifyNeedsReauth(username)
        ))}

    @app.context_processor
    def _injectMilestoneStatus():
        # Topbar badge for unacknowledged achievement milestones (new
        # play/listen-time/streak thresholds or a new #1 artist), cleared
        # when the dashboard renders the Milestones card (markMilestonesSeen
        # in routes/charts.py's dashboardIndex). Memoized on g by hand:
        # two values under one guard, and that memo is also what lets the
        # dashboard prime the count before clearing it - see
        # primeMilestoneBadge in app.py, which sets these same two keys and
        # without which the badge would never render on the page that
        # acknowledges it. No is_user_logged_in check, for the same reason
        # _injectShareStatus skips it - the worst case is a badge that 302s
        # to login like every other nav item. The admin kill switch
        # (milestones_enabled) zeroes the count and hides the dashboard
        # milestones row rather than deleting rows, mirroring how the
        # data-sharing switch zeroes the share badges.
        if "unseenMilestoneCount" not in g:
            g.milestonesEnabled = dashboard.repo.isMilestonesEnabled()
            username = session.get("username")
            g.unseenMilestoneCount = (
                dashboard.repo.getUnseenMilestoneCount(username)
                if g.milestonesEnabled and username else 0
            )
        return {"unseenMilestoneCount": g.unseenMilestoneCount,
                "milestones_enabled": g.milestonesEnabled}
