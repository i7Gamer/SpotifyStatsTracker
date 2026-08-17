# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""The authenticated-route decorator every page under routes/ shares.

Each view used to open with the same three lines:

    email, username, db = dashboard.get_current_user_or_redirect()
    if not email:
        return dashboard.unauthenticatedResponse()

which is a guard you can forget - and forgetting it doesn't fail loudly, it
serves another user's page shape to an anonymous visitor. Written once here,
a view can't be registered without it.

`dashboard` is a per-app object rather than an import, so this is a factory:
each routes module builds its own decorator inside register().
"""
import functools

from flask import abort, jsonify, redirect, url_for


def makeRequiresUser(dashboard):
    """Returns the `@requiresUser` decorator bound to this dashboard.

    A decorated view is called as `view(username, db, **urlParams)` - the
    email is only ever used for the guard itself, so it isn't passed on. Flask
    hands URL converters in as keyword arguments, which is why they ride
    through as **kwargs rather than positionally.

    Three flavours, because the routes genuinely have three contracts:

    `@requiresUser` is for a page. It delegates to
    dashboard.unauthenticatedResponse(), which redirects to the login screen -
    except for an ?ajax= request, which gets a 401 JSON instead, because
    fetch() follows a redirect transparently and the page would then try to
    parse the login HTML as JSON.

    `@requiresUser(api=True)` is for an endpoint that only ever answers JSON
    and is fetched without an ?ajax= marker. Redirecting those would hand the
    caller a login page to parse, so they always get the 401.

    `@requiresUser(admin=True)` additionally requires Repository.isAdmin, or
    403. Its anonymous branch answers with next=/admin - the admin surface's
    own convention: whatever sub-endpoint was POSTed, land back on the admin
    page after logging in. It routes that through unauthenticatedResponse
    rather than redirecting itself, so an admin fetch gets the 401 it can act
    on for the same reason a page's ?ajax= fetch does. This flavour replaced 13
    hand-rolled copies of the same four lines in routes/admin.py, which was
    exactly the forgettable-guard situation this file exists to close."""
    def requiresUser(view=None, *, api=False, admin=False):
        def decorate(view):
            @functools.wraps(view)
            def wrapped(**urlParams):
                email, username, db = dashboard.get_current_user_or_redirect()
                if not email:
                    if api:
                        return jsonify({"error": "Not logged in"}), 401
                    if admin:
                        #< through unauthenticatedResponse, not a bare redirect:
                        #  the admin console posts Create-backup by fetch, and a
                        #  302 to the login page is not something resp.json() can
                        #  read - it reported an expired session as a failed backup
                        return dashboard.unauthenticatedResponse(
                            nextPath=url_for("adminPage"))
                    return dashboard.unauthenticatedResponse()
                if admin and not dashboard.repo.isAdmin(username):
                    abort(403)
                return view(username, db, **urlParams)
            return wrapped
        #< supports both bare @requiresUser and @requiresUser(api=True/admin=True)
        return decorate(view) if view is not None else decorate
    return requiresUser
