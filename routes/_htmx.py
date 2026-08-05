# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What counts as an htmx swap, asked in one place.

Every migrated route decides "fragment or whole page" the same way, and used to
spell it inline as ``request.headers.get("HX-Request")`` in fourteen places.
That was right for exactly as long as ``HX-Request`` meant only one thing.

It does not. htmx sends it on history restores too - the request it makes when
the user presses Back to an htmx-managed entry that is no longer in its snapshot
cache. The vendored 2.0.9 build ships ``historyRestoreAsHxRequest: true``, so
such a request carries BOTH ``HX-Request`` and ``HX-History-Restore-Request``,
and htmx swaps the answer into the history element - ``document.body``, since
nothing here carries ``hx-history-elt``. A fragment served to it replaces the
entire document with a list or a card: no chrome, no nav, no scripts.

So the two are separated here rather than at fourteen call sites, where the next
route to be written would have got it wrong again. A restore is a page load; it
is the only kind of htmx request that wants the shell.

Deliberately a leaf module: it imports flask and nothing else, so both
``app.py`` and every ``routes/`` module can use it without an import cycle.
"""
from flask import request

#< set on every request htmx makes
HTMX_REQUEST_HEADER = "HX-Request"
#< ...and additionally on the Back/Forward re-fetch, whose response replaces the
#  history element rather than a swap target
HTMX_HISTORY_RESTORE_HEADER = "HX-History-Restore-Request"


def isHtmxSwap() -> bool:
    """True when htmx is asking for a fragment to swap into a region.

    False for a plain page load AND for a history restore - the latter carries
    ``HX-Request`` but wants the whole document, because what it gets back
    replaces ``document.body``.

    Guarding server-side rather than client-side (``htmx.config
    .historyRestoreAsHxRequest = false``) is deliberate: the config default has
    changed before, and a page whose chrome vanishes on Back is not a failure
    anyone would trace back to a vendored library's default.
    """
    return bool(request.headers.get(HTMX_REQUEST_HEADER)
                and not request.headers.get(HTMX_HISTORY_RESTORE_HEADER))


def isHtmxHistoryRestore() -> bool:
    """True for the Back/Forward re-fetch specifically.

    Separate from ``not isHtmxSwap()`` because the two answer different
    questions: routes ask the first to choose a template, while a caller that
    must not send ``HX-Redirect`` - which the restore XHR ignores, leaving an
    empty 204 to be swapped into ``document.body`` - asks this one.
    """
    return bool(request.headers.get(HTMX_HISTORY_RESTORE_HEADER))
