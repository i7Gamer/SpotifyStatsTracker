# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What counts as a caller that must be answered in JSON, asked in one place.

Several routes serve one URL two ways: a plain form POST gets a redirect back
to the page it came from, and the same POST made by ``fetch`` gets ``{kind,
message}`` JSON so the page can show the result without navigating away and
losing its tab/sort/page state. Those routes pick between the two by reading
``X-Requested-With``, which the fetch sets.

It was spelled inline at each of them, and the guard IN FRONT of them didn't
spell it at all - ``unauthenticatedResponse`` looked only for the ``?ajax=``
marker the page loaders use. So an expired session answered the admin console's
Create-backup and Refresh-Last.fm fetches with a 302, ``fetch`` followed it
transparently, ``resp.json()`` choked on the login page's HTML, and the admin
was told "Backup failed - try again" for what was really a logged-out session -
the exact failure the ``?ajax=`` branch exists to prevent, reachable through the
other spelling.

Matched exactly rather than merely present: a client that sends some other
value has not declared it can parse JSON, and the routes have always compared
it exactly. This module and ``routes/_htmx.py`` answer the same shape of
question for the two other clients this app has.

Deliberately a leaf module: it imports flask and nothing else, so ``app.py``
and every ``routes/`` module can use it without an import cycle.
"""
from flask import request

#< set by every fetch in this app that posts to a dual-mode route
XHR_REQUEST_HEADER = "X-Requested-With"
XHR_REQUEST_VALUE = "XMLHttpRequest"


def declaresItselfXhr() -> bool:
    """True when the caller announced it is a scripted request expecting JSON.

    False for a plain page load and for a no-JS form POST, both of which want
    the redirect."""
    return request.headers.get(XHR_REQUEST_HEADER) == XHR_REQUEST_VALUE
