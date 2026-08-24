# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import logging

# The Repository data-access layer is split into domain mixins under
# Database/queries/. This module composes them and keeps the connection/
# transaction primitives. `import *` re-exports the shared module constants
# (IMAGE_*, *_RETRY_SECONDS, *_SORT_COLUMNS, ...) that callers/tests import
# from Database.repository.
from Database.queries._base import *  # noqa: F401,F403
from Database.queries.tracks import TrackQueries
from Database.queries.plays import PlayQueries
from Database.queries.users import UserQueries
from Database.queries.shares import ShareQueries
from Database.queries.schema import SchemaQueries
from Database.queries.genres import GenreQueries
from Database.queries.bios import BioQueries
from Database.queries.settings import SettingQueries
from Database.queries.wrapped import WrappedQueries
from Database.queries.milestones import MilestoneQueries
from Database.queries.tags import TagQueries
from Database.queries.trends import TrendQueries
from Database.queries.email_queries import EmailQueries, VALID_NOTIFICATION_EVENTS, EVENT_INVALID_COOKIES, EVENT_API_KEY_FAILED, EVENT_SHARE_REQUEST


logger = logging.getLogger(__name__)


class Repository(SqlFragments, TrackQueries, PlayQueries, UserQueries, ShareQueries, SchemaQueries, GenreQueries, BioQueries, SettingQueries, WrappedQueries, MilestoneQueries, TagQueries, TrendQueries, EmailQueries):
    """Data-access layer over the shared SQLite database.

    Catalog methods (tracks/artists/albums/playlists/images) operate on data
    that's global across every user, keyed by Spotify's own catalog ids.
    Per-user methods (plays/users/progress) are scoped by `username`.
    """

    def __init__(self, dbPath: Path | None = None):
        # Resolved against db.DEFAULT_DB_PATH at call time rather than as a
        # normal default argument, so tests can monkeypatch db.DEFAULT_DB_PATH
        # (see conftest.py's _isolateDefaultDbPath) and have every Repository()
        # constructed without an explicit path - including indirectly, e.g. via
        # SpotifyDashboardApp() - redirect to a per-test temp file instead of the
        # real project database.
        self.connectionManager = ConnectionManager(dbPath if dbPath is not None else db.DEFAULT_DB_PATH)

    def _conn(self):
        return self.connectionManager.connection()

    def connection(self):
        """Exposes the thread-local connection for callers that need to compose
        several non-auto-committing writes (upsertTrack/insertPlay) into a single
        transaction - e.g. a bulk import that must commit all-or-nothing."""
        return self._conn()

    def commit(self):
        self._conn().commit()

    def rollback(self):
        self._conn().rollback()

    def rollbackQuietly(self) -> bool:
        """rollback(), never raising. Returns whether it actually rolled back.

        For the `except` blocks that roll back and then re-raise or report the
        ORIGINAL failure. Database.utils.parseError reads only the exception it
        is handed - it never walks __context__ - so whatever reaches it IS the
        whole report, and a rollback that raised took the real cause's place in
        the log, in the import progress line the user reads, and in
        listener_last_error. A rollback is most likely to fail exactly when the
        database is in the trouble the original error describes, which is when
        losing it costs the most.

        Swallowed but NOT silent: a rollback that failed leaves the transaction
        OPEN - the very state the caller was trying to leave - so the next
        commit on this connection adopts whatever was staged. That is worth a
        line of its own even though it cannot be the exception."""
        try:
            self.rollback()
            return True
        except Exception as e:
            logger.error("Rollback failed - staged writes may still be pending on this "
                         "connection and could be adopted by the next commit: %s", e)
            return False
