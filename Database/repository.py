from __future__ import annotations

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


class Repository(TrackQueries, PlayQueries, UserQueries, ShareQueries, SchemaQueries, GenreQueries, BioQueries, SettingQueries, WrappedQueries, MilestoneQueries):
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
