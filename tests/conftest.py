"""Shared test-suite guards.

No test in this suite should ever reach the real network: everything external
(Spotify API, image CDNs) must be mocked. A missed mock used to fail silently -
or worse, pass while hammering open.spotify.com - so real socket connections
are blocked for every test and raise instead.
"""
import socket
import tempfile
import unittest
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _fastLastfmRateLimiter():
    """Database.lastfm.RATE_LIMITER is a real, process-wide singleton
    (Database/lastfm.py:174) that every LastfmClient(...) call defaults to
    (routes/auth.py's save_lastfm handler included) - it really does
    time.sleep() to keep requests LASTFM_REQUESTS_PER_SECOND apart, which is
    correct in production but means any test that touches a real
    (non-mocked-class) LastfmClient shares one real-time clock with every
    other such test in the session, each paying a real wait. No test asserts
    on this singleton's actual pacing (that's RateLimiterTestCase in
    test_lastfm_client.py, which builds its own fresh LastfmRateLimiter
    instances instead), so collapsing its interval to 0 removes the wait
    without touching what's actually under test."""
    import Database.lastfm as lastfmModule

    lastfmModule.RATE_LIMITER._interval = 0.0


@pytest.fixture(autouse=True, scope="session")
def _fastPasswordHashing():
    """generate_password_hash defaults to scrypt (~85ms/call - a real,
    deliberately-expensive security parameter, not something app logic
    controls). Every login/register/reset-password test pays that cost, and
    no test asserts on the hash's method/format, so a single cheap pbkdf2
    round is fine here. Mutates the shared function object's __defaults__
    rather than reassigning werkzeug.security.generate_password_hash itself:
    routes/auth.py and several test files already did `from werkzeug.security
    import generate_password_hash`, each binding its own reference to this
    same function object, so only mutating the object in place (not
    rebinding the name in werkzeug.security) reaches every one of them
    regardless of import order."""
    from werkzeug.security import generate_password_hash

    generate_password_hash.__defaults__ = ("pbkdf2:sha256:1", 16)


@pytest.fixture(autouse=True)
def _blockNetwork(monkeypatch):
    def guardedConnect(self, address):
        raise RuntimeError(
            f"Test attempted a real network connection to {address!r} - mock the "
            "HTTP call (e.g. patch requests.get / SpotipyFree) instead."
        )

    monkeypatch.setattr(socket.socket, "connect", guardedConnect)


@pytest.fixture(autouse=True)
def _isolateEncryptionKey(tmp_path, monkeypatch):
    """No test may read or write the real secrets/data_encryption_key.txt, nor
    pick up a DATA_ENCRYPTION_KEY/FLASK_SECRET_KEY from the host environment -
    each test gets its own key file path (auto-created on first use), so
    encryption is deterministic within a test and isolated between tests."""
    import Database.secret_store as secretStore

    monkeypatch.delenv(secretStore.ENCRYPTION_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(secretStore.FLASK_SECRET_KEY_ENV_VAR, raising=False)
    monkeypatch.setattr(secretStore, "DEFAULT_KEY_PATH", tmp_path / "test_data_encryption_key.txt")


@pytest.fixture(autouse=True)
def _isolateMediaDir(tmp_path, monkeypatch):
    """No test should scan or write into the real Database/Data/Media folder -
    Repository.getGlobalDatabaseStats() walks it (Database.queries.settings
    re-imports Database.database.MEDIA_DIR at call time, so this monkeypatch
    takes effect), and a real dev checkout's media cache can be large enough
    to make that walk noticeably slow, and shared enough to flake two
    concurrent test runs (e.g. under pytest-xdist) into each other."""
    import Database.database as databaseModule

    monkeypatch.setattr(databaseModule, "MEDIA_DIR", tmp_path / "test_media")


@pytest.fixture(autouse=True)
def _isolateDefaultDbPath(tmp_path, monkeypatch):
    """No test should ever touch the real Database/Users/spotify_stats.db - only
    tests that explicitly pass dbPath= are meant to touch a database at all.
    Redirects the default path (used by any Database()/Repository()/
    SpotifyDashboardApp() constructed without an explicit override - notably
    SpotifyDashboardApp's own user/cookie lookups) to a per-test temp file.
    Database.repository.Repository resolves this at call time (not as a normal
    default argument) specifically so this monkeypatch takes effect."""
    import Database.db as dbModule

    monkeypatch.setattr(dbModule, "DEFAULT_DB_PATH", tmp_path / "test_default.db")


def normalizeTrackForTest(track: dict) -> dict:
    """Fill in the fields Client.formatTrack normally provides but that test
    fixtures often omit for brevity, so a minimal {"id", "name", "artists"} dict
    can still be upserted through Repository.upsertTrack."""
    track = dict(track)
    trackId = track["id"]
    track.setdefault("url", f"http://example.com/track/{trackId}")
    track.setdefault("duration", 0)
    track.setdefault("explicit", False)
    track.setdefault("isrc", "")
    track.setdefault("discNumber", 0)
    track.setdefault("trackNumber", 0)
    track.setdefault("releaseDate", 0)
    albumId = track.get("imageId") or f"{trackId}-album"
    track.setdefault("imageId", albumId)
    track.setdefault("imageUrl", "")
    track.setdefault("album", {
        "id": albumId, "name": "Unknown Album", "url": "http://example.com/album",
        "imageId": albumId, "imageUrl": "", "totalTracks": 1, "releaseDate": track["releaseDate"],
    })
    track["artists"] = [
        {
            "id": artist["id"],
            "name": artist.get("name", artist["id"]),
            "url": artist.get("url", f"http://example.com/artist/{artist['id']}"),
            "imageUrl": artist.get("imageUrl", ""),
            "imageId": artist.get("imageId", artist["id"]),
        }
        for artist in track.get("artists", [])
    ]
    return track


def makeDatabaseWithData(dbPath: Path, tracks: dict, entries: list, username: str = "testuser"):
    """A Database instance backed by a fresh temp SQLite file, seeded with the
    given track catalog (dict of trackId -> Client.formatTrack-shaped dict, fields
    may be omitted - see normalizeTrackForTest) and play history (list of
    {id, playedAt, timePlayed, playedFrom} entries). The DB-backed replacement for
    the old in-memory tracksCache/entriesCache test fixture: every distinct track
    id referenced by `entries` gets at least a minimal placeholder row, since
    plays.track_id is a foreign key into tracks.id."""
    from Database.database import Database

    db = Database(username, dbPath=dbPath)

    allTrackIds = set(tracks.keys()) | {e["id"] for e in entries}
    for trackId in allTrackIds:
        track = tracks.get(trackId) or {"id": trackId, "name": f"Song {trackId}", "artists": []}
        db.repo.upsertTrack(normalizeTrackForTest(track))
    for entry in entries:
        db.repo.insertPlay(username, entry["id"], entry["playedAt"], entry["timePlayed"], entry.get("playedFrom"))
    db.repo.commit()
    return db


class DatabaseTestCase(unittest.TestCase):
    """Base test case that provisions isolated temp-file-backed Database
    instances per test via _makeDb(tracks, entries)."""

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._nextDbIndex = 0

    def _makeDb(self, tracks, entries, username="testuser"):
        from Database.database import Database

        self._nextDbIndex += 1
        dbPath = Path(self._tmpdir.name) / f"test{self._nextDbIndex}.db"
        db = makeDatabaseWithData(dbPath, tracks, entries, username)
        self.addCleanup(db.repo.connectionManager.close)
        # Only the 5 always-on background workers, not stop() as a whole:
        # Database.__init__ never auto-starts the listener/autoImporter watchdog
        # (those need an explicit startListener()/watchFolder() call a test opts
        # into), but some tests (e.g. test_now_playing.py) replace db.listener
        # with a bare stub - db.stop() would crash on stub.stop() at teardown.
        self.addCleanup(db.stopMetadataBackfiller)
        self.addCleanup(db.stopWrappedCalculationsWorker)
        self.addCleanup(db.stopLastfmGenreBackfiller)
        self.addCleanup(db.stopLastfmBiographyBackfiller)
        self.addCleanup(db.stopLastfmAlbumBiographyBackfiller)
        self.addCleanup(Database._active_backfills.clear)
        return db
