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
    abort, send_from_directory,
)

import app as appmod
from Database.database import Database
from Database.repository import Repository
from Database.utils import msToString, now


def register(app, dashboard):
    WRAPPED_LIMIT_OPTIONS = appmod.WRAPPED_LIMIT_OPTIONS
    SHARE_LINK_EXPIRY_CHOICES = appmod.SHARE_LINK_EXPIRY_CHOICES
    SHARE_LINK_MAX_PER_BUCKET = appmod.SHARE_LINK_MAX_PER_BUCKET
    RATE_LIMIT_ERROR_MESSAGE = appmod.RATE_LIMIT_ERROR_MESSAGE

    def wrappedPage():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        nowLocal = now(tz=db.tz)
        currentYear = nowLocal.year
        availableYears = dashboard._computeAvailableYears(db)   #< most recent first, for the year badges

        year = dashboard._getWrappedYearParam(availableYears, currentYear)
        groupBy, limit, sortBy, isAjaxRequest, ajaxUpdateType, includeGenres = dashboard._parseWrappedFilterParams()

        ctx = dashboard._buildWrappedContext(db, year, groupBy, limit, sortBy, includeGenres=includeGenres)

        if isAjaxRequest:
            res = dashboard._buildWrappedAjaxResponse(ctx, username, year, ajaxUpdateType, publicView=False)
            # The share modal's panel is keyed to whatever year the page
            # last fully rendered with - without this, switching years
            # via the AJAX badges leaves it showing the previous year's
            # create-link form/action-URL/existing-link state even
            # though the rest of the page has moved on.
            if ajaxUpdateType == "all" and dashboard.repo.isShareLinksEnabled():
                yearLinks, allYearsLinks = dashboard._resolveShareLinksForYear(username, year)
                res["sharePanelHtml"] = render_template(
                    "_share_link_panel.html", year=year, yearLinks=yearLinks,
                    allYearsLinks=allYearsLinks, shareLinkExpiryChoices=SHARE_LINK_EXPIRY_CHOICES,
                    shareLinkMaxPerBucket=SHARE_LINK_MAX_PER_BUCKET)
            return jsonify(res)

        totalPlays, totalMs = ctx["totalPlays"], ctx["totalMs"]
        topSongs, topArtists, topAlbums = ctx["topSongs"], ctx["topArtists"], ctx["topAlbums"]
        discoveredSongs, discoveredArtists, discoveredAlbums = (
            ctx["discoveredSongs"], ctx["discoveredArtists"], ctx["discoveredAlbums"])
        timeSeries = ctx["timeSeries"]
        longestStreak, peakListeningTime = ctx["longestStreak"], ctx["peakListeningTime"]
        uniqueSongsCount, uniqueArtistsCount = ctx["uniqueSongsCount"], ctx["uniqueArtistsCount"]
        discoveredSongsCount, discoveredArtistsCount = ctx["discoveredSongsCount"], ctx["discoveredArtistsCount"]
        topGenres, genreCoverage, genreUnlocked = ctx["topGenres"], ctx["genreCoverage"], ctx["genreUnlocked"]
        lastfmEnabled = ctx["lastfmEnabled"]

        creds = db.getUserSpotifyCredentials() or {}
        has_api = bool(creds.get("client_id") and creds.get("client_secret"))
        is_authenticated = bool(creds.get("refresh_token"))

        success = request.args.get("success")
        error = request.args.get("error")

        shareLinksEnabled = dashboard.repo.isShareLinksEnabled()
        yearLinks, allYearsLinks = (
            dashboard._resolveShareLinksForYear(username, year) if shareLinksEnabled else ([], []))

        return render_template(
            "wrapped.html",
            username=username,
            section="wrapped",
            year=year,
            availableYears=availableYears,
            groupBy=groupBy,
            limit=limit,
            limitOptions=WRAPPED_LIMIT_OPTIONS,
            sortBy=sortBy,
            totalPlays=totalPlays,
            totalTime=msToString(totalMs),
            topSongs=topSongs,
            topArtists=topArtists,
            topAlbums=topAlbums,
            discoveredSongs=discoveredSongs,
            discoveredArtists=discoveredArtists,
            discoveredAlbums=discoveredAlbums,
            timeSeries=timeSeries,
            longestStreak=longestStreak,
            peakListeningTime=peakListeningTime,
            uniqueSongsCount=uniqueSongsCount,
            uniqueArtistsCount=uniqueArtistsCount,
            discoveredSongsCount=discoveredSongsCount,
            discoveredArtistsCount=discoveredArtistsCount,
            topGenres=topGenres,
            genreCoverage=genreCoverage,
            genreUnlocked=genreUnlocked,
            lastfmEnabled=lastfmEnabled,
            has_api=has_api,
            is_authenticated=is_authenticated,
            success=success,
            error=error,
            publicView=False,
            shareLinksEnabled=shareLinksEnabled,
            yearLinks=yearLinks,
            allYearsLinks=allYearsLinks,
            shareLinkExpiryChoices=SHARE_LINK_EXPIRY_CHOICES,
            shareLinkMaxPerBucket=SHARE_LINK_MAX_PER_BUCKET,
        )
    app.add_url_rule("/wrapped", "wrappedPage", wrappedPage, methods=["GET"])

    def createWrappedShareLink(year):
        """Creates a public, no-login share link for one year of the
        current user's own Wrapped - see sharedWrappedPage() below for
        the route that serves it. ajax=true mirrors wrappedPage()'s own
        AJAX convention: the modal on wrapped.html posts here in the
        background and swaps in the returned panel HTML instead of
        leaving the page."""
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

        expiresInSeconds = SHARE_LINK_EXPIRY_CHOICES.get(request.form.get("expiry", "never"))
        allYears = request.form.get("allYears") == "1"
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
            yearLinks, allYearsLinks = dashboard._resolveShareLinksForYear(username, year)
            html = render_template(
                "_share_link_panel.html", year=year, yearLinks=yearLinks,
                allYearsLinks=allYearsLinks, shareLinkExpiryChoices=SHARE_LINK_EXPIRY_CHOICES,
                shareLinkMaxPerBucket=SHARE_LINK_MAX_PER_BUCKET)
            return jsonify(html=html)
        return redirect(url_for("wrappedPage", year=year, success="Share link created.", openShareModal=1))
    app.add_url_rule("/wrapped/share-links/<int:year>", "createWrappedShareLink", createWrappedShareLink, methods=["POST"])

    def sharedWrappedPage(token):
        """Public, unauthenticated view of one user's Wrapped - one fixed
        year for a per-year link, or every year the owner has data for
        (year-switchable, like the authenticated page) for an "all
        years" link (link["year"] is None). No session, no nav, no PII.
        See docs/proposal-admin-and-share-links.md Part B for the design
        this implements."""
        if not dashboard.repo.isShareLinksEnabled():
            abort(404)

        link = dashboard.repo.getShareLink(token)
        if link is None:
            # Only misses count against the limit - repeat visits to a
            # real link must never be throttled (see the plan's rate-
            # limiting note), only someone guessing random tokens.
            if dashboard._rateLimited("shared_token"):
                abort(429)
            abort(404)

        db = dashboard._getReadOnlyUserDb(link["username"])
        isMultiYearShare = link["year"] is None
        # A single-year link's availableYears has exactly one entry, so
        # any ?year= override that doesn't match it falls back to the
        # pinned year below - a per-year link can't be tampered into
        # showing a different year of the same user's data.
        availableYears = dashboard._computeAvailableYears(db) if isMultiYearShare else [link["year"]]
        year = dashboard._getWrappedYearParam(availableYears, availableYears[0])
        groupBy, limit, sortBy, isAjaxRequest, ajaxUpdateType, includeGenres = dashboard._parseWrappedFilterParams()

        ctx = dashboard._buildWrappedContext(db, year, groupBy, limit, sortBy, includeGenres=includeGenres)

        if isAjaxRequest:
            return jsonify(dashboard._buildWrappedAjaxResponse(ctx, link["username"], year, ajaxUpdateType, publicView=True))

        resp = make_response(render_template(
            "wrapped.html",
            username=link["username"],
            section="wrapped",
            year=year,
            availableYears=availableYears,
            token=token,
            isMultiYearShare=isMultiYearShare,
            groupBy=groupBy,
            limit=limit,
            limitOptions=WRAPPED_LIMIT_OPTIONS,
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
            lastfmEnabled=ctx["lastfmEnabled"],
            has_api=False,
            is_authenticated=False,
            success=None,
            error=None,
            publicView=True,
            imageBase=f"/shared/{token}/img",
            shareLinksEnabled=False,
            yearLinks=[],
            allYearsLinks=[],
            shareLinkExpiryChoices=SHARE_LINK_EXPIRY_CHOICES,
            shareLinkMaxPerBucket=SHARE_LINK_MAX_PER_BUCKET,
        ))
        resp.headers["X-Robots-Tag"] = "noindex"
        return resp
    app.add_url_rule("/shared/<token>", "sharedWrappedPage", sharedWrappedPage, methods=["GET"])

    def serveSharedTrackImage(token, filename):
        link = dashboard.repo.getShareLink(token)
        if link is None or filename != os.path.basename(filename):
            return "", 404
        resp = make_response(send_from_directory(Database.imgDir_tracks, filename))
        resp.headers["X-Robots-Tag"] = "noindex"
        return resp
    app.add_url_rule("/shared/<token>/img/tracks/<filename>", "serveSharedTrackImage", serveSharedTrackImage)

    def serveSharedArtistImage(token, filename):
        link = dashboard.repo.getShareLink(token)
        if link is None or filename != os.path.basename(filename):
            return "", 404

        imageDir = Database.imgDir_artists
        imagePath = os.path.join(imageDir, filename)
        if not os.path.exists(imagePath):
            parts = os.path.splitext(filename)
            if len(parts) == 2 and parts[0].isalnum():
                db = dashboard._getReadOnlyUserDb(link["username"])
                db.lazyFetchArtistImage(parts[0], Path(imagePath))

        resp = make_response(send_from_directory(imageDir, filename))
        resp.headers["X-Robots-Tag"] = "noindex"
        return resp
    app.add_url_rule("/shared/<token>/img/artists/<filename>", "serveSharedArtistImage", serveSharedArtistImage)
