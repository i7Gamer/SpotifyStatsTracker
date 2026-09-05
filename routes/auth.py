# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Authentication, account and Spotify-connection routes.

Extracted verbatim from app.py: login/register/reset-password/logout, the three
profile pages and their POST actions (/profile for the display name and
preferences, /profile/sharing for share requests, /profile/connections for the
Last.fm key and Spotify Developer credentials), share request/link actions, and
the Spotify OAuth authorize/callback pair. Constants come from config; the
per-group _safeNextUrl and the shared _passwordPolicyError live here.
"""
import os
import re
import secrets
import logging
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import render_template, redirect, request, url_for, session, abort, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

from config import (
    PASSWORD_MIN_LENGTH, RATE_LIMIT_ERROR_MESSAGE, SPOTIFY_OAUTH_STATE_NUM_BYTES,
    SPOTIFY_OAUTH_STATE_SESSION_KEY, DISPLAY_NAME_MIN_LENGTH,
    DISPLAY_NAME_MAX_LENGTH, DISPLAY_NAME_ALLOWED_PATTERN, TOP_LIST_DEFAULT_WINDOW,
    SPOTIFY_AUTH_TIMEOUT_SECONDS, SPOTIFY_CALLBACK_URL_ENV_VAR,
)
from dashboard.date_ranges import SETTABLE_INTERVALS
from Database.Spotify.cookies import parseCookieString
from Database.utils import truncateForLog
from Database.lastfm import LastfmClient
from services.email_worker import queue_email_notification
from Database.queries.email_queries import (
    EVENT_SHARE_REQUEST,
    VALID_NOTIFICATION_EVENTS,
)

logger = logging.getLogger(__name__)

# The paths a `next` may never name, checked in _safeNextUrl. Not a security
# list - every one of them is same-origin, which is why the open-redirect guard
# above it lets them through - but a usability one: /logout is POST-only and
# answers 405, and the other three are the very forms the user just completed.
# Lower-cased and slash-stripped before the comparison, so "/Logout/" is the
# same entry.
AUTH_ENDPOINT_PATHS = frozenset({"/login", "/logout", "/register", "/reset-password"})

#< the one rejection all three cookie forms give, formatted with the email the
#  cookies had to match - naming it is what makes the message actionable
COOKIE_OWNERSHIP_ERROR = (
    "Couldn't verify that these cookies belong to {email}. "
    "Make sure you are logged into open.spotify.com with that account and copied all cookies."
)

# Which profile section a redirect's success/error message belongs to. The value
# is both the `flash_for` query param and the section's element id, so one string
# anchors the scroll and picks the block that renders the message (see
# _profile_macros.html's flash macro). A value the landing page has no section
# for - absent, unknown, or naming a section that lives on one of the other two
# pages - falls through to that page's flashFallback slot rather than vanishing.
PROFILE_FLASH_DISPLAY_NAME = "display-name"
PROFILE_FLASH_PREFERENCES = "preferences"
PROFILE_FLASH_SHARING = "data-sharing"
PROFILE_FLASH_SPOTIFY = "spotify"
PROFILE_FLASH_LASTFM = "lastfm"
PROFILE_FLASH_NOTIFICATIONS = "notifications"
PROFILE_FLASH_SESSIONS = "sessions"

HTTP_BAD_REQUEST = 400
# Rate-limited actions render this status instead of redirecting - a 302 would
# drop the code and land the browser on a page that never mentions the throttle.
HTTP_TOO_MANY_REQUESTS = 429


def _passwordPolicyError(password: str) -> str | None:
    """None if `password` satisfies the account password policy, otherwise a
    user-facing message naming the first unmet rule."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters long."
    if not any(c.isupper() for c in password):
        return "Password must contain at least one uppercase letter."
    if not any(c.islower() for c in password):
        return "Password must contain at least one lowercase letter."
    if not any(c.isdigit() or not c.isalnum() for c in password):
        return "Password must contain at least one number or special character."
    return None


def _isConstructableTimezone(timezone: str) -> bool:
    """Whether `timezone` is a name ZoneInfo can actually build.

    Only the malformed/unknown case matters here - Database.refreshSettings
    already catches the SAME construction failure at read time (see its
    comment), but silently, so the write side must refuse it instead of
    storing a value that only fails later with no signal to the user."""
    try:
        ZoneInfo(timezone)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def register(app, dashboard):
    def _displayNameError(value):
        """Why this display name can't be used, or None if it's fine.

        Only shape is checked here - whether the name is already TAKEN is
        decided by setDisplayName's guarded UPDATE, because a check made here
        would be a separate read that two simultaneous saves could both pass."""
        if len(value) < DISPLAY_NAME_MIN_LENGTH:
            return f"A display name needs at least {DISPLAY_NAME_MIN_LENGTH} characters."
        if len(value) > DISPLAY_NAME_MAX_LENGTH:
            return f"A display name can be at most {DISPLAY_NAME_MAX_LENGTH} characters."
        if not re.match(DISPLAY_NAME_ALLOWED_PATTERN, value):
            return "A display name can only contain letters, digits, spaces, - and _."
        return None

    def _verifiedCookies(cookies, email):
        """The parsed cookies, or None when they don't provably belong to
        `email`.

        This is the whole of how identity is proved in this app - /login,
        /register and /reset-password all ask exactly this question - and each
        of them used to carry its own copy of the four lines that ask it, right
        down to a character-identical error string. Three copies of one rule is
        how one of them drifts.

        Verification runs against a THROWAWAY session file (see
        _verifyCookiesMatchEmail), so nothing is persisted for an email until
        the cookies are shown to be that account's. None rather than {} for the
        rejection: every caller has already refused an empty submission, so the
        two can't be confused."""
        parsed = parseCookieString(cookies)
        if (not dashboard.skipEmailVerification
                and dashboard.repo.isEmailVerificationEnabled()
                and not dashboard._verifyCookiesMatchEmail(parsed, email)):
            return None
        return parsed

    def _safeNextUrl(nextUrl):
        """Only allow same-origin relative redirects after login - a `next`
        value like `//evil.com`, `https://evil.com`, or `/\\evil.com`
        (browsers normalize a leading "/\\" to "//", making it protocol-
        relative too) would otherwise send a freshly authenticated session
        to an attacker-controlled site.

        Control characters are rejected before the "//" check because they are
        stripped during URL normalization rather than carried through: `/\\t//`
        reaches the browser as `//`, sliding an attacker's host past a guard
        that only ever inspects index 1. Werkzeug refuses CR/LF in a header
        value on its own, but a tab survives all the way into `Location`, so
        the whole C0/DEL class is refused here instead of relying on that.

        The auth endpoints are refused last, and for a different reason: they
        ARE same-origin, so nothing above rejects them, and they are the one
        set of paths a just-authenticated session has no business landing on.
        /logout is POST-only, so it answers 405 - a blank browser error page
        the moment the login succeeds - and the other three are the forms the
        user has only just finished with, which reads as the login having
        silently failed. Matched as whole paths rather than by prefix, or
        /login-history would stop working the day someone adds it."""
        if not nextUrl or nextUrl[0] != "/":
            return None
        if any(char < " " or char == "\x7f" for char in nextUrl):
            return None
        if len(nextUrl) >= 2 and nextUrl[1] in ("/", "\\"):
            return None
        if nextUrl.split("?", 1)[0].rstrip("/").lower() in AUTH_ENDPOINT_PATHS:
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

            # A login is a user switch, so the previous session's leftovers must
            # not cross into this one: on a shared browser that meant an
            # abandoned one-shot OAuth state (SPOTIFY_OAUTH_STATE_SESSION_KEY -
            # the CSRF guard for binding a refresh token to an account) and any
            # flash queued for someone else. Both branches here only ever
            # ASSIGNED over whatever was in the cookie.
            # Before `permanent`, never after: permanent IS a session key
            # (_permanent), so clearing afterwards silently downgrades the cookie
            # to a browser-session one. Flask-WTF has already validated this
            # request's CSRF token by now, and the page we redirect to mints a
            # fresh one, so dropping it here costs nothing.
            session.clear()
            session.permanent = True
            dashboard.get_user_db(username, email)
            session["email"] = email
            session["username"] = username
            dashboard.stampSessionVersion(username)
            return redirect(nextUrl or url_for("dashboard"))

        cookies = request.form.get("cookies", "")

        if not email or not cookies:
            #< cookieError=True: this is the (collapsed) cookies form's own
            #  failure, not the password form's - see login.html's `if
            #  cookieError` branch, which opens the <details> and moves the
            #  error inside it instead of leaving it above the password
            #  fields that were never submitted (2026-09-02 review, UT-21).
            return render_template(
                "login.html", email=email, next=nextUrl, cookieError=True,
                error="Email and cookies are both required.")

        parsedCookies = _verifiedCookies(cookies, email)
        if parsedCookies is None:
            return render_template(
                "login.html", email=email, next=nextUrl, cookieError=True,
                error=COOKIE_OWNERSHIP_ERROR.format(email=email))

        session.clear()   #< a login is a user switch - see the password branch above
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
        dashboard.stampSessionVersion(username)

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

        parsedCookies = _verifiedCookies(cookies, email)
        if parsedCookies is None:
            return render_template(
                "register.html", email=email,
                error=COOKIE_OWNERSHIP_ERROR.format(email=email))

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
        session.clear()   #< registering logs this account in - see /login's password branch
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
        dashboard.stampSessionVersion(username)

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
        parsedCookies = _verifiedCookies(cookies, email)
        if parsedCookies is None:
            return render_template(
                "reset_password.html", email=email,
                error=COOKIE_OWNERSHIP_ERROR.format(email=email))

        dashboard.repo.setUserCookies(username, parsedCookies)
        dashboard.repo.setUserPassword(username, generate_password_hash(password))
        if username in dashboard.user_databases:
            dashboard._refresh_user_session(username, email)
        else:
            dashboard.get_user_db(username, email)

        # The point of a reset that isn't just a new password: every session
        # this account has anywhere ends here, including ones on devices this
        # browser has no reach over. Sessions are signed cookies with no
        # server-side store, so the bump IS the logout - see
        # SESSION_VERSION_KEY.
        dashboard.repo.bumpUserSessionVersion(username)

        session.clear()   #< a reset logs this account in - see /login's password branch
        session.permanent = True
        session["email"] = email
        session["username"] = username
        #< after the bump, or the reset signs out the device performing it
        dashboard.stampSessionVersion(username)

        return redirect(url_for("dashboard"))
    app.add_url_rule("/reset-password", "resetPassword", resetPassword, methods=["GET", "POST"])

    def logout():
        session.clear()
        return redirect(url_for("login"))
    # POST-only + CSRF-protected: clearing the session is state-changing, so a
    # cross-site GET must not be able to trigger it (the session cookie rides
    # top-level GET navigations even under SameSite=Lax).
    app.add_url_rule("/logout", "logout", logout, methods=["POST"])

    def _profileRedirect(endpoint, flashFor, success=None, error=None):
        """Back to a profile page with the message attached to `flashFor`'s
        section.

        The same string is the query param the template matches on and the
        anchor the browser scrolls to, so a save confirms itself next to the
        form that produced it rather than at the top of the page."""
        return redirect(url_for(endpoint, success=success, error=error,
                                flash_for=flashFor, _anchor=flashFor))

    def _profileChrome(username, email, subsection):
        """The context all profile pages share: the identity block, which
        sub-nav tab is current, which tabs exist at all, and whatever flash the
        redirect that landed here carried."""
        spotifyCallbackUrl = os.environ.get(SPOTIFY_CALLBACK_URL_ENV_VAR)
        lastfmEnabled = dashboard.repo.isLastfmGenreBackfillEnabled()
        featureEnabled = bool(spotifyCallbackUrl)
        emailNotifsEnabled = dashboard.repo.getAppSetting("email_notifications_enabled", "0") == "1"
        return dict(
            username=username,
            email=email,
            #< stays "profile" on all so the topbar's Account dropdown
            #  highlight needs no special case; subsection drives the sub-nav
            section="profile",
            subsection=subsection,
            sharing_enabled=dashboard.repo.isDataSharingEnabled(),
            email_notifications_enabled=emailNotifsEnabled,
            feature_enabled=featureEnabled,
            lastfm_enabled=lastfmEnabled,
            #< with both integrations off the Connections tab disappears rather
            #  than leading to a page with nothing on it
            connections_available=featureEnabled or lastfmEnabled,
            redirect_uri=spotifyCallbackUrl,
            success=request.args.get("success"),
            error=request.args.get("error"),
            flashFor=request.args.get("flash_for", ""),
        )

    def profilePage():
        """Account: identity, display name and per-user preferences."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        chrome = _profileChrome(username, email, "account")
        success = error = None
        flashFor = ""
        responseStatus = 200   #< 429 when a POST action was rate limited

        if request.method == "POST":
            action = request.form.get("action")
            if action == "save_preferences":
                flashFor = PROFILE_FLASH_PREFERENCES
                default_window = request.form.get("default_dashboard_window")
                #< None (never submitted) means "leave it alone" to
                #  updateUserSettings, which is what any future form that omits
                #  this select should get - see its comment
                default_top_list_window = request.form.get("default_top_list_window")
                # Both selects only ever offer SETTABLE_INTERVALS' seven
                # values (profile.html), so a miss here can only come from a
                # hand-crafted POST - but honouring it would store an
                # interval every _resolveIntervalParam absentDefault caller
                # has to keep working for from then on (X3, 2026-09-02
                # review). None (not submitted) is exempt: that is the
                # "leave it alone" case the comment above already covers,
                # not a value to validate.
                timezone = request.form.get("timezone")
                if timezone == "":
                    #< empty has always meant "no override, use the instance
                    #  zone" - not a value to validate against ZoneInfo below
                    timezone = None
                if ((default_window is not None and default_window not in SETTABLE_INTERVALS)
                        or (default_top_list_window is not None
                            and default_top_list_window not in SETTABLE_INTERVALS)):
                    error = "Invalid time window selected."
                elif timezone is not None and not _isConstructableTimezone(timezone):
                    # profile.html's <select> only ever offers 14 fixed names, so
                    # a miss here is a hand-crafted POST too (same standard as
                    # the interval guard above). Database.refreshSettings wraps
                    # its own ZoneInfo(...) construction in a bare
                    # `except Exception` and falls back to the instance zone -
                    # without this guard the bad value was stored, "saved
                    # successfully" was shown, and every tz-derived figure
                    # (streaks, heatmap, Wrapped, on-this-day) silently used the
                    # instance zone instead with no signal to the user
                    # (2026-09-04 review, F-A-3).
                    error = "Invalid timezone."
                else:
                    # An unchecked checkbox isn't submitted, so absence has to mean
                    # "off" - but both of these controls are gated on an admin
                    # switch (see profile.html), and reading absence as "off"
                    # unconditionally let a save from a form that never SHOWED the
                    # control clear what the user had stored. For hide_now_playing
                    # that silently reverted them to broadcasting their current
                    # track. Each control ships a hidden _present marker, so the
                    # form says which switches it was actually able to offer;
                    # without the marker the stored value is left alone.
                    current = db.repo.getUserSettings(username)

                    def _checkboxValue(field):
                        #< a submitted value is unambiguous with or without the
                        #  marker; only ABSENCE needs to know whether the control
                        #  was on the page at all
                        if request.form.get(field) == "1":
                            return True
                        if request.form.get(f"{field}_present") != "1":
                            return bool(current.get(field))
                        return False

                    hide_tags_panel = _checkboxValue("hide_tags_panel")
                    hide_now_playing = _checkboxValue("hide_now_playing")
                    try:
                        db.repo.updateUserSettings(username, default_window, timezone, hide_tags_panel,
                                                   hide_now_playing,
                                                   default_top_list_window=default_top_list_window)
                        db.refreshSettings()
                        success = "Preferences saved successfully!"
                    except Exception as e:
                        error = f"Failed to save preferences: {str(e)}"
            elif action == "save_display_name":
                flashFor = PROFILE_FLASH_DISPLAY_NAME
                # Throttled like request_share: this name is visible to every
                # user this account shares with, and the uniqueness guard makes
                # a rejected save a cheap probe for which names exist.
                if dashboard._rateLimited("save_display_name"):
                    error = RATE_LIMIT_ERROR_MESSAGE
                    responseStatus = HTTP_TOO_MANY_REQUESTS
                else:
                    #< empty means "go back to the username" - stored as NULL,
                    #  never as the username itself, so the account keeps
                    #  following its key rather than pinning a copy of it
                    submitted = (request.form.get("display_name") or "").strip()
                    policyError = _displayNameError(submitted) if submitted else None
                    if policyError:
                        error = policyError
                    elif dashboard.repo.setDisplayName(username, submitted or None):
                        success = (f"Display name set to {submitted}." if submitted
                                   else f"Display name cleared - you'll show as {username}.")
                    else:
                        error = "That name is already taken."
            else:
                abort(404)

            # POST/Redirect/GET: rendering the POST response directly meant a
            # refresh re-submitted the action. The throttled path deliberately
            # renders instead - see HTTP_TOO_MANY_REQUESTS.
            if responseStatus != HTTP_TOO_MANY_REQUESTS:
                return _profileRedirect("profilePage", flashFor, success=success, error=error)
            chrome.update(success=success, error=error, flashFor=flashFor)

        settings = db.repo.getUserSettings(username)

        return render_template(
            "profile.html",
            #< read AFTER the POST branch above, so a just-saved name is what
            #  the form redisplays rather than the pre-save value
            displayName=dashboard.repo.getDisplayName(username),
            displayNameMinLength=DISPLAY_NAME_MIN_LENGTH,
            displayNameMaxLength=DISPLAY_NAME_MAX_LENGTH,
            default_window=settings.get("default_dashboard_window", "day"),
            default_top_list_window=settings.get("default_top_list_window", TOP_LIST_DEFAULT_WINDOW),
            user_timezone=settings.get("timezone") or "",
            hide_now_playing=settings.get("hide_now_playing", False),
            friends_now_playing_enabled=dashboard.repo.isFriendsNowPlayingEnabled(),
            **chrome,
        ), responseStatus
    app.add_url_rule("/profile", "profilePage", profilePage, methods=["GET", "POST"])

    def profileSharingPage():
        """Mutual data sharing: the request picker and the three lists."""
        if not dashboard.repo.isDataSharingEnabled():
            abort(404)
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        chrome = _profileChrome(username, email, "sharing")
        success = error = None
        responseStatus = 200

        if request.method == "POST":
            if request.form.get("action") != "request_share":
                abort(404)
            target_username = request.form.get("target_username", "").strip()
            # Throttled like login/register: declines delete the row,
            # so nothing else stops a rejected requester from re-
            # requesting (or fanning out to every user) indefinitely.
            if dashboard._rateLimited("request_share"):
                error = RATE_LIMIT_ERROR_MESSAGE
                responseStatus = HTTP_TOO_MANY_REQUESTS
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
                    queue_email_notification(target_username, EVENT_SHARE_REQUEST, {"requester_username": username})
                    success = f"Share request sent to {target_username}."
                elif result == "already_accepted":
                    success = f"You already share data with {target_username}."
                else:   #< "already_requested"
                    success = f"A share request to {target_username} is already pending."

            if responseStatus != HTTP_TOO_MANY_REQUESTS:
                return _profileRedirect("profileSharingPage", PROFILE_FLASH_SHARING,
                                        success=success, error=error)
            chrome.update(success=success, error=error, flashFor=PROFILE_FLASH_SHARING)

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

        return render_template(
            "profile_sharing.html",
            pendingIncoming=pendingIncoming,
            pendingOutgoing=pendingOutgoing,
            acceptedShares=acceptedShares,
            shareCandidates=shareCandidates,
            **chrome,
        ), responseStatus
    app.add_url_rule("/profile/sharing", "profileSharingPage", profileSharingPage,
                     methods=["GET", "POST"])

    def profileConnectionsPage():
        """The third-party API keys: Spotify's Web API and Last.fm's."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        chrome = _profileChrome(username, email, "connections")
        if not chrome["connections_available"]:
            abort(404)   #< both integrations off: the tab isn't offered either
        feature_enabled = chrome["feature_enabled"]
        success = error = None
        flashFor = ""
        responseStatus = 200

        if request.method == "POST":
            action = request.form.get("action")
            if action == "save_lastfm":
                if not chrome["lastfm_enabled"]:
                    abort(404)
                flashFor = PROFILE_FLASH_LASTFM
                # Throttled like request_share: every save fires a live
                # validation request against Last.fm.
                if dashboard._rateLimited("save_lastfm"):
                    error = RATE_LIMIT_ERROR_MESSAGE
                    responseStatus = HTTP_TOO_MANY_REQUESTS
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
                flashFor = PROFILE_FLASH_LASTFM
                try:
                    db.updateUserLastfmApiKey(None)
                    db.stopLastfmGenreBackfiller()
                    db.stopLastfmBiographyBackfiller()
                    db.stopLastfmAlbumBiographyBackfiller()
                    success = "Last.fm API key removed."
                except Exception as e:
                    error = f"Failed to remove the Last.fm API key: {str(e)}"
            else:
                #< anything else on this page is the credentials form, which
                #  predates the action field and posts without one
                if not feature_enabled:
                    abort(404)
                flashFor = PROFILE_FLASH_SPOTIFY
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

            if responseStatus != HTTP_TOO_MANY_REQUESTS:
                return _profileRedirect("profileConnectionsPage", flashFor,
                                        success=success, error=error)
            chrome.update(success=success, error=error, flashFor=flashFor)

        creds = db.getUserSpotifyCredentials() or {}
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")

        return render_template(
            "profile_connections.html",
            client_id=client_id,
            # Never echo the stored secret back into the page - only signal that
            # one is saved so the field can show a placeholder (mirrors the
            # Last.fm key field). client_id is not secret and is still shown.
            has_client_secret=bool(client_secret),
            has_api=bool(client_id and client_secret),
            has_lastfm=bool(db.getUserLastfmApiKey()),
            artist_bio_enabled=dashboard.repo.isArtistBioEnabled(),
            album_bio_enabled=dashboard.repo.isAlbumBioEnabled(),
            is_authenticated=bool(creds.get("refresh_token")),
            spotify_needs_reauth=creds.get("needs_reauth", False),
            **chrome,
        ), responseStatus
    app.add_url_rule("/profile/connections", "profileConnectionsPage", profileConnectionsPage,
                     methods=["GET", "POST"])

    def profileNotificationsPage():
        """User email notification event preferences."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        chrome = _profileChrome(username, email, "notifications")
        if not chrome["email_notifications_enabled"]:
            abort(404)

        success = error = None
        flashFor = ""
        responseStatus = 200

        if request.method == "POST":
            flashFor = PROFILE_FLASH_NOTIFICATIONS
            for event in VALID_NOTIFICATION_EVENTS:
                form_key = f"notif_{event}"
                enabled = request.form.get(form_key) == "1"
                dashboard.repo.setUserNotificationPreference(username, event, enabled)
            success = "Notification preferences saved."
            return _profileRedirect("profileNotificationsPage", flashFor, success=success, error=error)

        preferences = dashboard.repo.getAllUserNotificationPreferences(username)
        return render_template(
            "profile_notifications.html",
            preferences=preferences,
            **chrome,
        ), responseStatus
    app.add_url_rule("/profile/notifications", "profileNotificationsPage", profileNotificationsPage,
                     methods=["GET", "POST"])

    def profileSignOutEverywhere():
        """End every session this account has, on every device.

        There is nothing to delete: sessions are signed cookies with no
        server-side store, so what actually happens is that the account's
        session_version moves and every cookie carrying the old number stops
        being accepted (see SESSION_VERSION_KEY). That includes the cookie
        making THIS request, which is why the session is re-stamped straight
        afterwards - otherwise the button signs the user out of the browser
        they pressed it in, which reads as a broken feature rather than a
        thorough one."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login"))

        dashboard.repo.bumpUserSessionVersion(username)
        dashboard.stampSessionVersion(username)
        return _profileRedirect("profilePage", PROFILE_FLASH_SESSIONS,
                                success="Signed out on every other device.")
    # POST-only + CSRF-protected: it changes state, so it must not be reachable
    # through a cross-site GET link - the same rule /logout and
    # /profile/disconnect follow.
    app.add_url_rule("/profile/sign-out-everywhere", "profileSignOutEverywhere",
                     profileSignOutEverywhere, methods=["POST"])

    def profileDisconnect():
        if not os.environ.get(SPOTIFY_CALLBACK_URL_ENV_VAR):
            abort(404)
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login"))

        try:
            db.updateUserSpotifyCredentials(None, None, None)
            return _profileRedirect("profileConnectionsPage", PROFILE_FLASH_SPOTIFY,
                                    success="Successfully disconnected Spotify API credentials.")
        except Exception as e:
            return _profileRedirect("profileConnectionsPage", PROFILE_FLASH_SPOTIFY, error=f"Failed to disconnect: {str(e)}")
    # POST-only + CSRF-protected: wiping the stored Spotify credentials is
    # state-changing, so it must not be reachable via a cross-site GET link.
    app.add_url_rule("/profile/disconnect", "profileDisconnect", profileDisconnect, methods=["POST"])

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
            return _profileRedirect("profileSharingPage", PROFILE_FLASH_SHARING, success=successMsg)
        return _profileRedirect("profileSharingPage", PROFILE_FLASH_SHARING, error=errorMsg)
    app.add_url_rule("/profile/shares/<int:share_id>", "profileShareAction", profileShareAction, methods=["POST"])

    def spotifyAuthorize():
        spotify_callback_url = os.environ.get(SPOTIFY_CALLBACK_URL_ENV_VAR)
        if not spotify_callback_url:
            abort(404)
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login"))

        creds = db.getUserSpotifyCredentials() or {}
        client_id = creds.get("client_id")
        if not client_id:
            return _profileRedirect("profileConnectionsPage", PROFILE_FLASH_SPOTIFY, error="API Credentials not configured.")

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
        spotify_callback_url = os.environ.get(SPOTIFY_CALLBACK_URL_ENV_VAR)
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
            # Through _profileRedirect like every other failure branch below:
            # this one predates the profile split and still sent the user to
            # the Account tab, away from the 'Authorize with Spotify' button
            # its own message tells them to press, with a bare `error` param
            # no profile template renders without a flash_for beside it.
            return _profileRedirect(
                "profileConnectionsPage", PROFILE_FLASH_SPOTIFY,
                error="Spotify authorization failed: missing or mismatched state - "
                      "please start over with 'Authorize with Spotify'.")

        code = request.args.get("code")
        error = request.args.get("error")

        if error or not code:
            return _profileRedirect(
                "profileConnectionsPage", PROFILE_FLASH_SPOTIFY,
                error=f"Spotify authorization failed: {error or 'No authorization code returned'}")

        creds = db.getUserSpotifyCredentials() or {}
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")

        if not client_id or not client_secret:
            return _profileRedirect("profileConnectionsPage", PROFILE_FLASH_SPOTIFY, error="API Credentials missing.")

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
            resp = requests.post(url, data=payload, headers=headers,
                                 timeout=SPOTIFY_AUTH_TIMEOUT_SECONDS)
            if resp.status_code == 200:
                resp_data = resp.json()
                # Spotify's code exchange includes refresh_token today, but a
                # 200 without one must not NULL the stored token behind a
                # success flash (updateUserSpotifyCredentials writes whatever
                # it is given) - keep the working grant instead.
                refresh_token = resp_data.get("refresh_token") or creds.get("refresh_token")
                if not refresh_token:
                    logger.warning("Spotify token exchange for %s returned no refresh token "
                                   "and none is stored - leaving credentials untouched", username)
                    return _profileRedirect(
                        "profileConnectionsPage", PROFILE_FLASH_SPOTIFY,
                        error="Spotify did not return a refresh token - please try authorizing again.")
                db.updateUserSpotifyCredentials(client_id, client_secret, refresh_token)
                # A fresh authorization always grants the scope /spotify-authorize
                # requested - clear any stale "needs reauth" flag immediately
                # rather than waiting up to WEB_API_POLL_INTERVAL_SECONDS for
                # the next backfill poll to prove it.
                db.setSpotifyNeedsReauth(False)

                # Restart listener thread to pick up the credentials immediately
                db.startListener()

                return _profileRedirect("profileConnectionsPage", PROFILE_FLASH_SPOTIFY,
                                        success="Spotify account successfully authorized and connected!")
            else:
                # Full response body only server-side - the redirect param ends up
                # in browser history/access logs and may echo credential details.
                logger.warning("Spotify token exchange failed for %s (HTTP %s): %s",
                               username, resp.status_code, truncateForLog(resp.text))
                return _profileRedirect(
                    "profileConnectionsPage", PROFILE_FLASH_SPOTIFY,
                    error="Failed to exchange token with Spotify - check your API credentials and try again.")
        except Exception as e:
            logger.warning("Exception during Spotify token exchange for %s: %s", username, e)
            return _profileRedirect(
                "profileConnectionsPage", PROFILE_FLASH_SPOTIFY,
                error="Something went wrong during the token exchange - please try again.")
    app.add_url_rule("/spotify-callback", "spotifyCallback", spotifyCallback, methods=["GET"])

    def profileShareLinkAction(link_id):
        """Owner-only revoke for a public Wrapped share link - accept/
        decline don't apply here (unlike profileShareAction's mutual
        shares), there's only ever one action. ajax=true is how the
        wrapped.html modal calls it (see createWrappedShareLink); the
        no-JS fallback redirects back to the modal.

        The /profile URL is a leftover: the links were managed there until
        they moved into the Wrapped modal, and /wrapped/share-links/<int:...>
        is already taken by createWrappedShareLink's <int:year>, so the two
        would collide on the same converter."""
        isAjax = request.args.get("ajax") == "true"
        year = request.form.get("year", type=int)
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            if isAjax:
                return jsonify(error="Please log in again."), 401
            return redirect(url_for("login"))

        # Checked BEFORE the revoke, not after: the panel this branch re-renders
        # builds a create-form action from `year`, whose rule is <int:year>, so a
        # missing or non-numeric field raised a BuildError - 500ing on an action
        # that had already destroyed the link. Every real client sends it.
        if isAjax and year is None:
            return jsonify(error="Missing the year this panel is showing."), HTTP_BAD_REQUEST

        if dashboard.repo.revokeShareLink(link_id, username):
            if isAjax:
                html = render_template("_share_link_panel.html",
                                       **dashboard.shareLinkPanelArgs(username, year))
                return jsonify(html=html)
            return redirect(url_for("wrappedPage", year=year, success="Share link revoked.",
                                    openShareModal=1))
        if isAjax:
            return jsonify(error="Could not revoke that share link."), 403
        return redirect(url_for("wrappedPage", year=year,
                                error="Could not revoke that share link.", openShareModal=1))
    app.add_url_rule("/profile/share-links/<int:link_id>", "profileShareLinkAction", profileShareLinkAction, methods=["POST"])
