"""Authentication, account and Spotify-connection routes.

Extracted verbatim from app.py: login/register/reset-password/logout, the
profile page and its POST actions (preferences, share requests, Last.fm key,
Spotify Developer credentials), share request/link actions, and the Spotify
OAuth authorize/callback pair. App-level constants and the shared
_passwordPolicyError are aliased from the app module at register() time; the
per-group _safeNextUrl helper lives here.
"""
import os
import secrets
import logging
from urllib.parse import urlencode

from flask import render_template, redirect, request, url_for, session, abort, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

import app as appmod
from SpotipyFree import parseCookieString
from Database.lastfm import LastfmClient
from Database.utils import dateToString

logger = logging.getLogger(__name__)


def register(app, dashboard):
    RATE_LIMIT_ERROR_MESSAGE = appmod.RATE_LIMIT_ERROR_MESSAGE
    SPOTIFY_OAUTH_STATE_NUM_BYTES = appmod.SPOTIFY_OAUTH_STATE_NUM_BYTES
    SPOTIFY_OAUTH_STATE_SESSION_KEY = appmod.SPOTIFY_OAUTH_STATE_SESSION_KEY
    SHARE_LINK_EXPIRY_CHOICES = appmod.SHARE_LINK_EXPIRY_CHOICES
    SHARE_LINK_MAX_PER_BUCKET = appmod.SHARE_LINK_MAX_PER_BUCKET
    _passwordPolicyError = appmod._passwordPolicyError

    def _safeNextUrl(nextUrl):
        """Only allow same-origin relative redirects after login - a `next`
        value like `//evil.com`, `https://evil.com`, or `/\\evil.com`
        (browsers normalize a leading "/\\" to "//", making it protocol-
        relative too) would otherwise send a freshly authenticated session
        to an attacker-controlled site."""
        if not nextUrl or nextUrl[0] != "/":
            return None
        if len(nextUrl) >= 2 and nextUrl[1] in ("/", "\\"):
            return None
        return nextUrl

    def login():
        if request.method == "GET":
            return render_template("login.html", next=_safeNextUrl(request.args.get("next")))

        email = request.form.get("email", "").strip()
        nextUrl = _safeNextUrl(request.form.get("next"))

        if dashboard._rateLimited("login"):
            return render_template(
                "login.html", email=email, next=nextUrl,
                error=RATE_LIMIT_ERROR_MESSAGE), 429

        # The password form and the (collapsed, fallback) cookies form are
        # separate <form>s on login.html - only the submitted one's fields
        # are present, so their presence tells the two branches apart.
        if "password" in request.form:
            password = request.form.get("password", "")
            if not email or not password:
                return render_template(
                    "login.html", email=email, next=nextUrl,
                    error="Email and password are both required.")

            username = dashboard.repo.getUsernameForEmail(email)
            passwordHash = dashboard.repo.getUserPasswordHash(username) if username else None
            if username and not passwordHash:
                return render_template(
                    "login.html", email=email, next=nextUrl,
                    error="This account doesn't have a password yet - register to add one, "
                          "or log in with cookies instead.")
            if not passwordHash or not check_password_hash(passwordHash, password):
                return render_template(
                    "login.html", email=email, next=nextUrl,
                    error="Invalid email or password.")

            # Password auth only ever unlocks the session while the cookies
            # stored from the last cookie-based (re-)login/register/reset
            # are still live - a lapsed Spotify session must be refreshed
            # via /reset-password or the cookies form, not just a password.
            if not dashboard.is_user_logged_in(email):
                return render_template(
                    "login.html", email=email, next=nextUrl,
                    error="Your saved Spotify session has expired. Reset your password with "
                          "fresh cookies, or log in with cookies instead.")

            session.permanent = True
            dashboard.get_user_db(username, email)
            session["email"] = email
            session["username"] = username
            return redirect(nextUrl or url_for("dashboard"))

        cookies = request.form.get("cookies", "")

        if not email or not cookies:
            return render_template(
                "login.html", email=email, next=nextUrl,
                error="Email and cookies are both required.")

        # Verification happens against a throwaway session file, so nothing
        # is persisted for this email unless the cookies really are theirs.
        parsedCookies = parseCookieString(cookies)
        if not dashboard.skipEmailVerification and not dashboard._verifyCookiesMatchEmail(parsedCookies, email):
            return render_template(
                "login.html", email=email, next=nextUrl,
                error=f"Couldn't verify that these cookies belong to {email}. "
                      "Make sure you are logged into open.spotify.com with that account and copied all cookies.")

        session.permanent = True
        # get_or_create_user/get_user_db manage their own locking and can
        # start a listener (a live network call) - kept outside the session
        # lock so logging one user in doesn't block everyone else.
        username = dashboard.get_or_create_user(email)
        dashboard.repo.setUserCookies(username, parsedCookies)
        if username in dashboard.user_databases:
            # Returning user re-logging in (e.g. after their session
            # expired) - restart their listener against the fresh cookies
            # instead of leaving the old, dead one running.
            dashboard._refresh_user_session(username, email)
        else:
            dashboard.get_user_db(username, email)
        session["email"] = email
        session["username"] = username

        return redirect(nextUrl or url_for("dashboard"))
    app.add_url_rule("/login", "login", login, methods=["GET", "POST"])

    def register_():
        if not dashboard.repo.isRegistrationEnabled():
            abort(404)
        if request.method == "GET":
            return render_template("register.html")

        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirmPassword = request.form.get("confirm_password", "")
        cookies = request.form.get("cookies", "")

        if dashboard._rateLimited("register"):
            return render_template(
                "register.html", email=email, error=RATE_LIMIT_ERROR_MESSAGE), 429

        if not email or not password or not confirmPassword or not cookies:
            return render_template(
                "register.html", email=email,
                error="Email, password, confirmed password and cookies are all required.")

        if password != confirmPassword:
            return render_template(
                "register.html", email=email, error="Passwords do not match.")

        policyError = _passwordPolicyError(password)
        if policyError:
            return render_template("register.html", email=email, error=policyError)

        # Verification happens against a throwaway session file, so nothing
        # is persisted for this email unless the cookies really are theirs.
        parsedCookies = parseCookieString(cookies)
        if not dashboard.skipEmailVerification and not dashboard._verifyCookiesMatchEmail(parsedCookies, email):
            return render_template(
                "register.html", email=email,
                error=f"Couldn't verify that these cookies belong to {email}. "
                      "Make sure you are logged into open.spotify.com with that account and copied all cookies.")

        existingUsername = dashboard.repo.getUsernameForEmail(email)
        if existingUsername and dashboard.repo.getUserPasswordHash(existingUsername):
            return render_template(
                "register.html", email=email,
                error="An account with this email already exists. Log in, or reset your "
                      "password if you forgot it.")

        # get_or_create_user returns the existing username for a legacy
        # (pre-password) account instead of erroring, so registering with
        # an email that's already logged in via cookies just adds a
        # password to that account rather than being rejected as a dupe.
        session.permanent = True
        username = dashboard.get_or_create_user(email)
        dashboard.repo.setUserCookies(username, parsedCookies)
        dashboard.repo.setUserPassword(username, generate_password_hash(password))
        if username in dashboard.user_databases:
            dashboard._refresh_user_session(username, email)
        else:
            dashboard.get_user_db(username, email)
        session["email"] = email
        session["username"] = username

        return redirect(url_for("dashboard"))
    app.add_url_rule("/register", "register", register_, methods=["GET", "POST"])

    def resetPassword():
        if request.method == "GET":
            return render_template("reset_password.html")

        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirmPassword = request.form.get("confirm_password", "")
        cookies = request.form.get("cookies", "")

        if dashboard._rateLimited("reset-password"):
            return render_template(
                "reset_password.html", email=email, error=RATE_LIMIT_ERROR_MESSAGE), 429

        if not email or not password or not confirmPassword or not cookies:
            return render_template(
                "reset_password.html", email=email,
                error="Email, new password, confirmed password and cookies are all required.")

        if password != confirmPassword:
            return render_template(
                "reset_password.html", email=email, error="Passwords do not match.")

        policyError = _passwordPolicyError(password)
        if policyError:
            return render_template("reset_password.html", email=email, error=policyError)

        username = dashboard.repo.getUsernameForEmail(email)
        if not username:
            return render_template(
                "reset_password.html", email=email, error="No account found for this email.")

        # There's no old password to check against a forgotten one - proof
        # of identity is the same as everywhere else in this app: valid,
        # matching Spotify cookies for the account's email.
        parsedCookies = parseCookieString(cookies)
        if not dashboard.skipEmailVerification and not dashboard._verifyCookiesMatchEmail(parsedCookies, email):
            return render_template(
                "reset_password.html", email=email,
                error=f"Couldn't verify that these cookies belong to {email}. "
                      "Make sure you are logged into open.spotify.com with that account and copied all cookies.")

        dashboard.repo.setUserCookies(username, parsedCookies)
        dashboard.repo.setUserPassword(username, generate_password_hash(password))
        if username in dashboard.user_databases:
            dashboard._refresh_user_session(username, email)
        else:
            dashboard.get_user_db(username, email)

        session.permanent = True
        session["email"] = email
        session["username"] = username

        return redirect(url_for("dashboard"))
    app.add_url_rule("/reset-password", "resetPassword", resetPassword, methods=["GET", "POST"])

    def logout():
        session.clear()
        return redirect(url_for("login"))
    app.add_url_rule("/logout", "logout", logout, methods=["GET"])

    def profilePage():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        success = request.args.get("success")
        error = request.args.get("error")
        responseStatus = 200   #< 429 when a POST action was rate limited

        spotify_callback_url = os.environ.get("SPOTIFY_CALLBACK_URL")
        feature_enabled = bool(spotify_callback_url)

        if request.method == "POST":
            action = request.form.get("action")
            if action == "save_preferences":
                default_window = request.form.get("default_dashboard_window")
                timezone = request.form.get("timezone")
                if timezone == "":
                    timezone = None
                try:
                    db.repo.updateUserSettings(username, default_window, timezone)
                    db.refreshSettings()
                    success = "Preferences saved successfully!"
                except Exception as e:
                    error = f"Failed to save preferences: {str(e)}"
            elif action == "request_share":
                if not dashboard.repo.isDataSharingEnabled():
                    abort(404)
                target_username = request.form.get("target_username", "").strip()
                # Throttled like login/register: declines delete the row,
                # so nothing else stops a rejected requester from re-
                # requesting (or fanning out to every user) indefinitely.
                if dashboard._rateLimited("request_share"):
                    error = RATE_LIMIT_ERROR_MESSAGE
                    responseStatus = 429
                elif not target_username:
                    error = "Please choose a user to request a share with."
                elif target_username == username:
                    error = "You cannot request a share with yourself."
                elif not dashboard.repo.usernameExists(target_username):
                    error = "That username does not exist."
                else:
                    result = dashboard.repo.createShareRequest(username, target_username)
                    if result == "accepted":
                        success = f"You and {target_username} are now sharing data with each other!"
                    elif result == "requested":
                        success = f"Share request sent to {target_username}."
                    elif result == "already_accepted":
                        success = f"You already share data with {target_username}."
                    else:   #< "already_requested"
                        success = f"A share request to {target_username} is already pending."
            elif action == "save_lastfm":
                if not dashboard.repo.isLastfmGenreBackfillEnabled():
                    abort(404)
                # Throttled like request_share: every save fires a live
                # validation request against Last.fm.
                if dashboard._rateLimited("save_lastfm"):
                    error = RATE_LIMIT_ERROR_MESSAGE
                    responseStatus = 429
                else:
                    lastfm_api_key = (request.form.get("lastfm_api_key") or "").strip()
                    if not lastfm_api_key:
                        error = "A Last.fm API key is required."
                    else:
                        validation = LastfmClient(lastfm_api_key).validateApiKey()
                        if validation["ok"]:
                            try:
                                db.updateUserLastfmApiKey(lastfm_api_key)
                                db.startLastfmGenreBackfiller()
                                db.startLastfmBiographyBackfiller()
                                db.startLastfmAlbumBiographyBackfiller()
                                success = "Last.fm API key saved! Genre and biography data are now backfilling in the background."
                            except Exception as e:
                                error = f"Failed to save the Last.fm API key: {str(e)}"
                        elif validation["error"] == "invalid_key":
                            error = "Last.fm rejected that API key - double-check it and try again."
                        elif validation["error"] == "busy":
                            error = "The Last.fm request budget is busy right now - try again in a few seconds."
                        else:
                            error = "Could not reach Last.fm to verify the key - try again later."
            elif action == "remove_lastfm":
                try:
                    db.updateUserLastfmApiKey(None)
                    db.stopLastfmGenreBackfiller()
                    db.stopLastfmBiographyBackfiller()
                    db.stopLastfmAlbumBiographyBackfiller()
                    success = "Last.fm API key removed."
                except Exception as e:
                    error = f"Failed to remove the Last.fm API key: {str(e)}"
            else:
                if not feature_enabled:
                    abort(404)
                client_id = request.form.get("client_id")
                client_secret = request.form.get("client_secret")
                if client_id and client_secret:
                    try:
                        db.updateUserSpotifyCredentials(client_id, client_secret, None)
                        success = "Spotify Developer credentials saved! Please click 'Authorize with Spotify' to connect your account."
                    except Exception as e:
                        error = f"Failed to save credentials: {str(e)}"
                else:
                    error = "Both Client ID and Client Secret are required."

        creds = db.getUserSpotifyCredentials() or {}
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")
        refresh_token = creds.get("refresh_token")
        spotify_needs_reauth = creds.get("needs_reauth", False)

        settings = db.repo.getUserSettings(username)
        default_window = settings.get("default_dashboard_window", "day")
        user_timezone = settings.get("timezone") or ""

        # Reaching this render means the Active Shares list below is
        # about to show whatever the notification was about - clears the
        # topbar's "your request was accepted" badge for next page load.
        dashboard.repo.markAcceptedSharesSeenByRequester(username)

        pendingIncoming = dashboard.repo.getPendingIncomingShares(username)
        pendingOutgoing = dashboard.repo.getPendingOutgoingShares(username)
        acceptedShares = dashboard.repo.getAcceptedShares(username)
        # Users already in a share relationship (either direction, pending
        # or accepted) are excluded from the request picker - re-requesting
        # them is always a no-op, so offering them just invites confusion.
        existingCounterparts = ({share["counterpart"] for share in acceptedShares}
                                | {r["requester_username"] for r in pendingIncoming}
                                | {r["recipient_username"] for r in pendingOutgoing})
        shareCandidates = [u for u in dashboard.repo.getAllUsernamesExcept(username)
                           if u not in existingCounterparts]

        shareLinks = [
            {**link,
             "createdText": dateToString(link["created_at"], tz=db.tz),
             "expiresText": dateToString(link["expires_at"], tz=db.tz) if link["expires_at"] else "Never"}
            for link in dashboard.repo.getShareLinksForUser(username)
        ]

        return render_template(
            "profile.html",
            username=username,
            email=email,
            client_id=client_id,
            client_secret=client_secret,
            has_api=bool(client_id and client_secret),
            has_lastfm=bool(db.getUserLastfmApiKey()),
            lastfm_enabled=dashboard.repo.isLastfmGenreBackfillEnabled(),
            artist_bio_enabled=dashboard.repo.isArtistBioEnabled(),
            album_bio_enabled=dashboard.repo.isAlbumBioEnabled(),
            sharing_enabled=dashboard.repo.isDataSharingEnabled(),
            is_authenticated=bool(refresh_token),
            spotify_needs_reauth=spotify_needs_reauth,
            redirect_uri=spotify_callback_url,
            success=success,
            error=error,
            section="profile",
            feature_enabled=feature_enabled,
            default_window=default_window,
            user_timezone=user_timezone,
            pendingIncoming=pendingIncoming,
            pendingOutgoing=pendingOutgoing,
            acceptedShares=acceptedShares,
            shareCandidates=shareCandidates,
            shareLinks=shareLinks,
        ), responseStatus
    app.add_url_rule("/profile", "profilePage", profilePage, methods=["GET", "POST"])

    def profileDisconnect():
        if not os.environ.get("SPOTIFY_CALLBACK_URL"):
            abort(404)
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login"))

        try:
            db.updateUserSpotifyCredentials(None, None, None)
            return redirect(url_for("profilePage", success="Successfully disconnected Spotify API credentials."))
        except Exception as e:
            return redirect(url_for("profilePage", error=f"Failed to disconnect: {str(e)}"))
    app.add_url_rule("/profile/disconnect", "profileDisconnect", profileDisconnect, methods=["GET"])

    def profileShareAction(share_id):
        if not dashboard.repo.isDataSharingEnabled():
            abort(404)
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login"))

        action = request.form.get("action")
        # Each branch's repo call is itself ownership-checked (only the
        # recipient can accept/decline, only the requester can cancel,
        # either party can revoke) - `ok=False` here covers both "no such
        # share_id" and "share_id exists but isn't yours", identically.
        if action == "accept":
            ok = dashboard.repo.respondToShareRequest(share_id, username, accept=True)
            successMsg, errorMsg = "Share accepted - you're now comparing data with each other!", "Could not accept that request."
        elif action == "decline":
            ok = dashboard.repo.respondToShareRequest(share_id, username, accept=False)
            successMsg, errorMsg = "Share request declined.", "Could not decline that request."
        elif action == "cancel":
            ok = dashboard.repo.cancelShareRequest(share_id, username)
            successMsg, errorMsg = "Share request canceled.", "Could not cancel that request."
        elif action == "revoke":
            ok = dashboard.repo.revokeShare(share_id, username)
            successMsg, errorMsg = "Share revoked.", "Could not revoke that share."
        else:
            ok, errorMsg = False, "Unknown action."

        if ok:
            return redirect(url_for("profilePage", success=successMsg))
        return redirect(url_for("profilePage", error=errorMsg))
    app.add_url_rule("/profile/shares/<int:share_id>", "profileShareAction", profileShareAction, methods=["POST"])

    def spotifyAuthorize():
        spotify_callback_url = os.environ.get("SPOTIFY_CALLBACK_URL")
        if not spotify_callback_url:
            abort(404)
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login"))

        creds = db.getUserSpotifyCredentials() or {}
        client_id = creds.get("client_id")
        if not client_id:
            return redirect(url_for("profilePage", error="API Credentials not configured."))

        scope = "user-read-recently-played"
        # One-shot CSRF state - see SPOTIFY_OAUTH_STATE_SESSION_KEY's
        # comment. token_urlsafe output needs no URL-encoding.
        state = secrets.token_urlsafe(SPOTIFY_OAUTH_STATE_NUM_BYTES)
        session[SPOTIFY_OAUTH_STATE_SESSION_KEY] = state

        query = urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": spotify_callback_url,
            "scope": scope,
            "state": state,
        })
        return redirect(f"https://accounts.spotify.com/authorize?{query}")
    app.add_url_rule("/spotify-authorize", "spotifyAuthorize", spotifyAuthorize, methods=["GET"])

    def spotifyCallback():
        spotify_callback_url = os.environ.get("SPOTIFY_CALLBACK_URL")
        if not spotify_callback_url:
            abort(404)
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login"))

        # CSRF guard - see SPOTIFY_OAUTH_STATE_SESSION_KEY's comment.
        # pop(): a state value is single-use, so a replayed callback URL
        # is rejected even after a successful exchange.
        expectedState = session.pop(SPOTIFY_OAUTH_STATE_SESSION_KEY, None)
        if not expectedState or request.args.get("state") != expectedState:
            return redirect(url_for(
                "profilePage",
                error="Spotify authorization failed: missing or mismatched state - "
                      "please start over with 'Authorize with Spotify'."))

        code = request.args.get("code")
        error = request.args.get("error")

        if error or not code:
            return redirect(url_for("profilePage", error=f"Spotify authorization failed: {error or 'No authorization code returned'}"))

        creds = db.getUserSpotifyCredentials() or {}
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")

        if not client_id or not client_secret:
            return redirect(url_for("profilePage", error="API Credentials missing."))

        import base64
        import requests
        url = "https://accounts.spotify.com/api/token"
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": spotify_callback_url,
        }
        auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
        headers = {
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded"
        }

        try:
            resp = requests.post(url, data=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                resp_data = resp.json()
                refresh_token = resp_data.get("refresh_token")
                db.updateUserSpotifyCredentials(client_id, client_secret, refresh_token)
                # A fresh authorization always grants the scope /spotify-authorize
                # requested - clear any stale "needs reauth" flag immediately
                # rather than waiting up to WEB_API_POLL_INTERVAL_SECONDS for
                # the next backfill poll to prove it.
                db.setSpotifyNeedsReauth(False)

                # Restart listener thread to pick up the credentials immediately
                db.startListener()

                return redirect(url_for("profilePage", success="Spotify account successfully authorized and connected!"))
            else:
                # Full response body only server-side - the redirect param ends up
                # in browser history/access logs and may echo credential details.
                logger.warning("Spotify token exchange failed for %s (HTTP %s): %s",
                               username, resp.status_code, resp.text)
                return redirect(url_for("profilePage", error="Failed to exchange token with Spotify - check your API credentials and try again."))
        except Exception as e:
            logger.warning("Exception during Spotify token exchange for %s: %s", username, e)
            return redirect(url_for("profilePage", error="Something went wrong during the token exchange - please try again."))
    app.add_url_rule("/spotify-callback", "spotifyCallback", spotifyCallback, methods=["GET"])

    def profileShareLinkAction(link_id):
        """Owner-only revoke for a public Wrapped share link - accept/
        decline don't apply here (unlike profileShareAction's mutual
        shares), there's only ever one action. ajax=true is the wrapped.html
        modal's revoke form (see createWrappedShareLink); profile.html's
        own revoke form never sets it and keeps the classic redirect."""
        isAjax = request.args.get("ajax") == "true"
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            if isAjax:
                return jsonify(error="Please log in again."), 401
            return redirect(url_for("login"))

        if dashboard.repo.revokeShareLink(link_id, username):
            if isAjax:
                year = request.form.get("year", type=int)
                yearLinks, allYearsLinks = dashboard._resolveShareLinksForYear(username, year)
                html = render_template(
                    "_share_link_panel.html", year=year, yearLinks=yearLinks,
                    allYearsLinks=allYearsLinks, shareLinkExpiryChoices=SHARE_LINK_EXPIRY_CHOICES,
                    shareLinkMaxPerBucket=SHARE_LINK_MAX_PER_BUCKET)
                return jsonify(html=html)
            return redirect(url_for("profilePage", success="Share link revoked."))
        if isAjax:
            return jsonify(error="Could not revoke that share link."), 403
        return redirect(url_for("profilePage", error="Could not revoke that share link."))
    app.add_url_rule("/profile/share-links/<int:link_id>", "profileShareLinkAction", profileShareLinkAction, methods=["POST"])
