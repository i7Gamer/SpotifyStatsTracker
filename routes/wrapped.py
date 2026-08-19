# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Wrapped routes: the authenticated /wrapped page, share-link creation, and the
public /shared/<token> view (plus its no-index image endpoints).

Extracted verbatim from app.py. App-level display constants (WRAPPED_LIMIT_OPTIONS,
SHARE_LINK_*, RATE_LIMIT_ERROR_MESSAGE) are aliased from the app module at
register() time; everything else is reached through the dashboard instance.
"""
import os
from pathlib import Path

from flask import (
    render_template, redirect, request, url_for, jsonify, make_response,
    abort,
)

from routes._htmx import isHtmxSwap
#< the same cacheable-image response the authenticated /img/ routes serve: one
#  rule for the files, wherever they are reached from
from routes.media import sendCacheableImage

from config import (
    WRAPPED_LIMIT_OPTIONS, WRAPPED_LIST_SIZE, SHARE_LINK_EXPIRY_CHOICES,
    SHARE_LINK_MAX_PER_BUCKET, RATE_LIMIT_ERROR_MESSAGE,
)
from Database.database import Database
from Database.repository import Repository
from Database.utils import msToString, now


def register(app, dashboard):

    #< the marker templates/wrapped.html's filter form puts on its own requests
    #  (hx-headers), and nothing else on the page does
    FILTER_CHANGE_HEADER = "X-Wrapped-Filter-Change"

    def _genreCardIsAlreadyCorrect():
        """True when this request cannot have moved the genre card, so its two
        year-scoped aggregations are skipped and the card on the page is left
        alone (see _wrapped_results.html's hx-preserve stub).

        The card's data depends on the YEAR and nothing else. The filter form
        cannot change the year: its only year input is the hidden field that is
        rewritten out of band after a badge click, and the year badges are
        boosted links OUTSIDE the form. So "the filter form made this request"
        is exactly "the year is the one already on screen".

        Both conditions are required. Without HX-Request there is no swap and
        therefore no card to preserve - a full render carrying a stray marker
        would otherwise produce a page whose genre card is a permanent hole.

        Deliberately not a cache keyed on (user, year): coverage grows while the
        Last.fm backfill runs and the admin's inherited-genres toggle moves it
        retroactively, which is why _buildWrappedContext computes it live and
        never from the user_wrapped cache. This keeps it live and only stops
        asking when the answer cannot have changed."""
        return bool(isHtmxSwap()
                    and request.headers.get(FILTER_CHANGE_HEADER))

    def _renderWrapped(pageArgs):
        """One render, two shapes. A plain GET is the whole recap; an htmx
        request - every year badge and filter change - gets just
        _wrapped_results.html, which htmx swaps into #wrappedResults (plus the
        handful of out-of-band regions that partial leads with).

        The marker is htmx's own HX-Request header rather than the ?ajax=true
        convention the unmigrated shell pages still use. Keying on the header
        is also what keeps the marker out of the URL bar: hx-replace-url writes
        back the URL that was requested, so a marker living in the query string
        would become part of the page's shareable address.

        Both branches get the SAME context, which is the point of the split -
        the second used to be a hand-assembled JSON envelope of six HTML chunks
        plus a dozen scalars, updated by ~200 lines of DOM surgery in
        static/js/wrapped.js, and the two drifted. See
        tests/test_wrapped_htmx.py."""
        if isHtmxSwap():
            return render_template("_wrapped_results.html", oobSwap=True, **pageArgs)
        return render_template("wrapped.html", **pageArgs)

    def wrappedPage():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            #< an htmx filter change needs HX-Redirect, not a 302: htmx follows
            #  a redirect as transparently as the old fetch() layer did, so the
            #  login page's HTML would be swapped into the recap and the user
            #  would never learn they were logged out. unauthenticatedResponse
            #  answers both shapes (see app.py)
            return dashboard.unauthenticatedResponse()

        nowLocal = now(tz=db.tz)
        currentYear = nowLocal.year
        availableYears = dashboard._computeAvailableYears(db)   #< most recent first, for the year badges

        year = dashboard._getWrappedYearParam(availableYears, currentYear)
        groupBy, limit, sortBy = dashboard._parseWrappedFilterParams()

        preserveGenres = _genreCardIsAlreadyCorrect()
        ctx = dashboard._buildWrappedContext(db, year, groupBy, limit, sortBy,
                                             includeGenres=not preserveGenres)

        creds = db.getUserSpotifyCredentials() or {}
        has_api = bool(creds.get("client_id") and creds.get("client_secret"))
        is_authenticated = bool(creds.get("refresh_token"))

        shareLinksEnabled = dashboard.repo.isShareLinksEnabled()
        # The share modal's panel is year-scoped (create-form action URL, the
        # revoke forms' hidden year, and which link counts as "current"), so it
        # is rebuilt on every render - a year switch that left it alone showed
        # the previous year's link state under the new year's heading.
        sharePanelArgs = (dashboard.shareLinkPanelArgs(username, year) if shareLinksEnabled
                          else {"yearLinks": [], "allYearsLinks": [], "otherYearLinks": []})

        return _renderWrapped(dict(
            username=username,
            section="wrapped",
            year=year,
            availableYears=availableYears,
            groupBy=groupBy,
            limit=limit,
            limitOptions=WRAPPED_LIMIT_OPTIONS,
            limitDefault=WRAPPED_LIST_SIZE,
            sortBy=sortBy,
            totalPlays=ctx["totalPlays"],
            totalTime=msToString(ctx["totalMs"]),
            topSongs=ctx["topSongs"],
            topArtists=ctx["topArtists"],
            topAlbums=ctx["topAlbums"],
            discoveredSongs=ctx["discoveredSongs"],
            discoveredArtists=ctx["discoveredArtists"],
            discoveredAlbums=ctx["discoveredAlbums"],
            timeSeries=ctx["timeSeries"],
            longestStreak=ctx["longestStreak"],
            peakListeningTime=ctx["peakListeningTime"],
            uniqueSongsCount=ctx["uniqueSongsCount"],
            uniqueArtistsCount=ctx["uniqueArtistsCount"],
            discoveredSongsCount=ctx["discoveredSongsCount"],
            discoveredArtistsCount=ctx["discoveredArtistsCount"],
            topGenres=ctx["topGenres"],
            genreCoverage=ctx["genreCoverage"],
            genreUnlocked=ctx["genreUnlocked"],
            genresPreserved=preserveGenres,
            lastfmEnabled=ctx["lastfmEnabled"],
            has_api=has_api,
            is_authenticated=is_authenticated,
            success=request.args.get("success"),
            error=request.args.get("error"),
            publicView=False,
            shareLinksEnabled=shareLinksEnabled,
            shareLinkExpiryChoices=SHARE_LINK_EXPIRY_CHOICES,
            shareLinkMaxPerBucket=SHARE_LINK_MAX_PER_BUCKET,
            #< named rather than spread: the helper also carries year and the
            #  two constants, which this render already passes as page-level
            #  context
            yearLinks=sharePanelArgs["yearLinks"],
            allYearsLinks=sharePanelArgs["allYearsLinks"],
            otherYearLinks=sharePanelArgs["otherYearLinks"],
        ))
    app.add_url_rule("/wrapped", "wrappedPage", wrappedPage, methods=["GET"])

    def createWrappedShareLink(year):
        """Creates a public, no-login share link for one year of the
        current user's own Wrapped - see sharedWrappedPage() below for
        the route that serves it. The modal on wrapped.html posts here in the
        background and swaps in the returned panel HTML instead of leaving the
        page.

        Deliberately still ?ajax=true + {"html": ...} while wrappedPage() has
        moved to htmx. This is an ACTION, not a page load: it answers with the
        share panel rather than the recap, and its 4xx bodies carry messages
        the modal shows in place ("you've reached the limit", "unknown link
        expiry"), which an htmx swap would need HX-Reswap/HX-Retarget gymnastics
        to keep. Migrating it (and its sibling profileShareLinkAction in
        routes/auth.py) is a separate, self-contained job."""
        isAjax = request.args.get("ajax") == "true"
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            if isAjax:
                return jsonify(error="Please log in again."), 401
            return redirect(url_for("login", next=url_for("wrappedPage")))
        if not dashboard.repo.isShareLinksEnabled():
            abort(404)
        if dashboard._rateLimited("share_link_create"):
            if isAjax:
                return jsonify(error=RATE_LIMIT_ERROR_MESSAGE), 429
            return redirect(url_for("wrappedPage", error=RATE_LIMIT_ERROR_MESSAGE, openShareModal=1))

        # An unrecognized value used to fall through .get() to None, i.e. a
        # permanent link - the most permissive option. A share link is a
        # standing unauthenticated access grant, so an unknown expiry is
        # rejected rather than defaulted.
        expiryChoice = request.form.get("expiry", "never")
        if expiryChoice not in SHARE_LINK_EXPIRY_CHOICES:
            errorMessage = "Unknown link expiry option."
            if isAjax:
                return jsonify(error=errorMessage), 400
            return redirect(url_for("wrappedPage", year=year, error=errorMessage, openShareModal=1))
        expiresInSeconds = SHARE_LINK_EXPIRY_CHOICES[expiryChoice]
        allYears = request.form.get("allYears") == "1"
        # <int:year> bounds nothing, and _buildWrappedContext does
        # nowLocal.replace(year=year + 1): a hand-crafted POST for year 9999
        # minted a link whose PUBLIC page 500'd on every visit (year 10000 is
        # out of datetime's range; year 0 dies on the first replace). Validate
        # against the years the user has data for - the same set the share
        # modal offers. allYears links carry no year, so they skip this.
        if not allYears and year not in dashboard._computeAvailableYears(db):
            errorMessage = "You can only share a year you have listening data for."
            if isAjax:
                return jsonify(error=errorMessage), 400
            return redirect(url_for("wrappedPage", error=errorMessage, openShareModal=1))
        linkYear = None if allYears else year

        bucketCount = dashboard.repo.countActiveShareLinksForBucket(
            username, Repository.SHARE_LINK_KIND_WRAPPED, linkYear)
        if bucketCount >= SHARE_LINK_MAX_PER_BUCKET:
            bucketLabel = "all-years" if linkYear is None else str(linkYear)
            errorMessage = (
                f"You've reached the limit of {SHARE_LINK_MAX_PER_BUCKET} {bucketLabel} share links. "
                "Revoke one to create another.")
            if isAjax:
                return jsonify(error=errorMessage), 400
            return redirect(url_for("wrappedPage", year=year, error=errorMessage, openShareModal=1))

        dashboard.repo.createShareLink(username, Repository.SHARE_LINK_KIND_WRAPPED, linkYear, expiresInSeconds)
        if isAjax:
            html = render_template("_share_link_panel.html",
                                   **dashboard.shareLinkPanelArgs(username, year))
            return jsonify(html=html)
        return redirect(url_for("wrappedPage", year=year, success="Share link created.", openShareModal=1))
    app.add_url_rule("/wrapped/share-links/<int:year>", "createWrappedShareLink", createWrappedShareLink, methods=["POST"])

    def _sharedImageBase(token):
        """Token-keyed image prefix for _track_card.html. Shared by the full
        render and the ajax list swap - they drifted apart once already, which
        left anonymous viewers with placeholder artwork after any re-sort."""
        return f"/shared/{token}/img"

    def _resolveSharedLink(token):
        """The share-link lookup every public route shares: the admin kill
        switch, then the token itself. Returns None when the caller should
        404 (feature off, or no such link), and aborts 429 once a source IP
        has burned through the miss allowance.

        Only misses count against the limit - repeat visits to a real link
        must never be throttled (see the plan's rate-limiting note), only
        someone guessing random tokens. The image routes go through this too,
        so guessing at an image URL can't sidestep either control."""
        if not dashboard.repo.isShareLinksEnabled():
            return None
        link = dashboard.repo.getShareLink(token)
        if link is None:
            if dashboard._rateLimited("shared_token"):
                abort(429)
            return None
        return link

    def sharedWrappedPage(token):
        """Public, unauthenticated view of one user's Wrapped - one fixed
        year for a per-year link, or every year the owner has data for
        (year-switchable, like the authenticated page) for an "all
        years" link (link["year"] is None). No session, no nav, no PII.
        See docs/proposal-admin-and-share-links.md Part B for the design
        this implements."""
        link = _resolveSharedLink(token)
        if link is None:
            abort(404)

        db = dashboard._getReadOnlyUserDb(link["username"])
        isMultiYearShare = link["year"] is None
        # A single-year link's availableYears has exactly one entry, so
        # any ?year= override that doesn't match it falls back to the
        # pinned year below - a per-year link can't be tampered into
        # showing a different year of the same user's data.
        availableYears = dashboard._computeAvailableYears(db) if isMultiYearShare else [link["year"]]
        year = dashboard._getWrappedYearParam(availableYears, availableYears[0])
        groupBy, limit, sortBy = dashboard._parseWrappedFilterParams()

        #< the public twin renders the same template and the same filter form,
        #  so it gets the same saving - a visitor changing Trend buckets on a
        #  shared recap pays for the genre card exactly as often as the owner
        preserveGenres = _genreCardIsAlreadyCorrect()
        ctx = dashboard._buildWrappedContext(db, year, groupBy, limit, sortBy,
                                             includeGenres=not preserveGenres)

        # This route is NOT behind @requiresUser: the two ways it fails are a
        # dead token (404, above) and the miss rate limit (429), neither of
        # which is a session problem. An htmx request therefore gets the SAME
        # status it always did - no HX-Redirect to /login, which would send an
        # anonymous visitor to a login screen for an account that isn't theirs.
        # wrapped.js reports it as a load failure with a Retry. Pinned by
        # tests/test_wrapped_htmx.py's TestSharedWrappedHtmx.
        resp = make_response(_renderWrapped(dict(
            username=link["username"],
            section="wrapped",
            year=year,
            availableYears=availableYears,
            token=token,
            isMultiYearShare=isMultiYearShare,
            groupBy=groupBy,
            limit=limit,
            limitOptions=WRAPPED_LIMIT_OPTIONS,
            limitDefault=WRAPPED_LIST_SIZE,
            sortBy=sortBy,
            totalPlays=ctx["totalPlays"],
            totalTime=msToString(ctx["totalMs"]),
            topSongs=ctx["topSongs"],
            topArtists=ctx["topArtists"],
            topAlbums=ctx["topAlbums"],
            discoveredSongs=ctx["discoveredSongs"],
            discoveredArtists=ctx["discoveredArtists"],
            discoveredAlbums=ctx["discoveredAlbums"],
            timeSeries=ctx["timeSeries"],
            longestStreak=ctx["longestStreak"],
            peakListeningTime=ctx["peakListeningTime"],
            uniqueSongsCount=ctx["uniqueSongsCount"],
            uniqueArtistsCount=ctx["uniqueArtistsCount"],
            discoveredSongsCount=ctx["discoveredSongsCount"],
            discoveredArtistsCount=ctx["discoveredArtistsCount"],
            topGenres=ctx["topGenres"],
            genreCoverage=ctx["genreCoverage"],
            genreUnlocked=ctx["genreUnlocked"],
            genresPreserved=preserveGenres,
            lastfmEnabled=ctx["lastfmEnabled"],
            has_api=False,
            is_authenticated=False,
            success=None,
            error=None,
            publicView=True,
            imageBase=_sharedImageBase(token),
            # An anonymous viewer has no session, so every /song|/artist|/album
            # detail link would bounce them to /login - cards fall through to
            # their Spotify URL instead (same mechanism the Compare page's
            # counterpart columns use).
            suppressDetailLinks=True,
            shareLinksEnabled=False,
            yearLinks=[],
            allYearsLinks=[],
            otherYearLinks=[],
            shareLinkExpiryChoices=SHARE_LINK_EXPIRY_CHOICES,
            shareLinkMaxPerBucket=SHARE_LINK_MAX_PER_BUCKET,
        )))
        resp.headers["X-Robots-Tag"] = "noindex"
        return resp
    app.add_url_rule("/shared/<token>", "sharedWrappedPage", sharedWrappedPage, methods=["GET"])

    def serveSharedTrackImage(token, filename):
        link = _resolveSharedLink(token)
        if link is None or filename != os.path.basename(filename):
            return "", 404
        resp = sendCacheableImage(Database.imgDir_tracks, filename)
        resp.headers["X-Robots-Tag"] = "noindex"
        return resp
    app.add_url_rule("/shared/<token>/img/tracks/<filename>", "serveSharedTrackImage", serveSharedTrackImage)

    def serveSharedArtistImage(token, filename):
        link = _resolveSharedLink(token)
        if link is None or filename != os.path.basename(filename):
            return "", 404

        imageDir = Database.imgDir_artists
        imagePath = os.path.join(imageDir, filename)
        if not os.path.exists(imagePath):
            parts = os.path.splitext(filename)
            # Unlike the authenticated route, the caller here is anonymous and
            # the fetch would run on the LINK OWNER's Spotify credentials, so
            # it's restricted to ids that actually appear in their listening
            # data. Otherwise walking arbitrary ids through this route would
            # insert an images row and dispatch an authenticated lookup per
            # id, with no session and no backpressure.
            if (len(parts) == 2 and parts[0].isalnum()
                    and dashboard.repo.getPlayedArtistIds(link["username"], [parts[0]])):
                db = dashboard._getReadOnlyUserDb(link["username"])
                db.lazyFetchArtistImage(parts[0], Path(imagePath))

        resp = sendCacheableImage(imageDir, filename)
        resp.headers["X-Robots-Tag"] = "noindex"
        return resp
    app.add_url_rule("/shared/<token>/img/artists/<filename>", "serveSharedArtistImage", serveSharedArtistImage)
