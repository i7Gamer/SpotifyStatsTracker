# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

# Imported for its SIDE EFFECTS, not for anything it exports: importing
# Database.patches is what installs the spotapi monkey-patches (the websocket
# reconnect, the deliberate-close handling, the state deep-copy). Unused-import
# tooling reads it as dead - removing it leaves an unpatched spotapi under the
# whole app, so it carries the marker explicitly rather than relying on F401
# staying unselected.
from . import patches  # noqa: F401
