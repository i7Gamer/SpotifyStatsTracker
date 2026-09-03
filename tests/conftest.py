"""Shared test-suite guards.

No test in this suite should ever reach the real network: everything external
(Spotify API, image CDNs) must be mocked. A missed mock used to fail silently -
or worse, pass while hammering open.spotify.com - so real socket connections
are blocked for every test and raise instead.
"""
import datetime
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Every per-user background thread a live Database starts, by thread-name
# prefix: the five periodic workers (Database/workers/), the auto-import
# watchdog, and the listener's poll thread. All are named "<prefix><user>" -
# see _noLeakedUserThreads.
USER_DATABASE_THREAD_NAME_PREFIXES = (
    "auto-import-watchdog-",
    "spotify-listener-",
    "spotify-connect-state-",   #< the listener's connect-state loop, one level down
    "metadata-backfiller-",
    "wrapped-worker-",
    "lastfm-genres-",
    "lastfm-bios-",
    "lastfm-album-bios-",
)


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


@pytest.fixture(autouse=True)
def _resetSpotifyRateLimiter():
    """Database.rate_limit.SPOTIFY_LIMITER is a real, process-wide singleton
    that Database/patches.py routes EVERY Spotify request through - the
    connect-state poll loop, the account-settings lookups, and track metadata
    fetches. Two problems for tests, both fixed here:

    Its interval really does time.sleep() between grants, so a test driving
    five poll iterations would pay five real waits for pacing nothing asserts
    on (test_rate_limit.py builds its own fresh instances for that).

    And applyBackoff() is process-wide by design, so a test that exercises a
    rate-limit path would leave a live penalty window that stalls every
    unrelated test after it. Function-scoped (unlike the Last.fm fixture
    above, which only needs the interval zeroed once) precisely because that
    state is per-test."""
    import Database.rate_limit as rateLimitModule

    limiter = rateLimitModule.SPOTIFY_LIMITER
    limiter._interval = 0.0
    limiter._nextSlotAt = 0.0
    limiter._backoffUntil = 0.0
    limiter._backoffCount = 0
    limiter._lastBackoffAt = None
    limiter._lastReason = None


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
            "HTTP call (e.g. patch requests.get / Database.Spotify) instead."
        )

    monkeypatch.setattr(socket.socket, "connect", guardedConnect)


@pytest.fixture(autouse=True)
def _noLeakedUserThreads():
    """A test that starts a real per-user Database must stop it again.

    Activating a user (SpotifyDashboardApp.get_user_db) starts six real daemon
    threads for that user - the five periodic workers and the auto-import
    watchdog, plus the listener's poll thread whenever a login actually
    succeeds - and nothing else ever stops them. Left running, they outlive the
    test: each parks on a randomized startup delay (5-30s), then wakes up long
    after the test that spawned it finished, logs into a pytest capture stream
    that is already closed ("ValueError: I/O operation on closed file", which
    can bury a real teardown failure), creates its user's autoImport/ folder in
    the working tree, and keeps polling for the rest of the session while the
    next tests stack more of the same.

    Named threads, not every new entry in threading.enumerate(): the latter
    would flag any unrelated short-lived helper thread that happens to still be
    winding down, which is exactly the kind of timing-dependent flake this
    suite avoids. Threads already running when the test STARTED are excluded so
    one leaker doesn't fail every test that follows it in the same worker
    process. `dash.shutdown()` (or db.stop()) at teardown is the fix."""
    def userThreads():
        return {t for t in threading.enumerate()
                if t.name.startswith(USER_DATABASE_THREAD_NAME_PREFIXES)}

    before = userThreads()
    yield
    leaked = sorted(t.name for t in userThreads() - before)
    assert not leaked, (
        f"test left per-user Database threads running: {leaked}. "
        "Stop them at teardown (e.g. self.addCleanup(self.dash.shutdown)).")


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


def rawSpotifyTrackForTest(trackId: str, name: str = None, artistName: str = "Artist One") -> dict:
    """A track in SPOTIFY'S OWN wire shape - what `Spotify.track()` returns and
    `Client.formatTrack` converts.

    normalizeTrackForTest above produces the app's INTERNAL shape, which is a
    different thing: passing one where the other is expected fails on
    `track["external_urls"]["spotify"]`. This exists so a test can stub the
    importer's Spotify client instead of constructing a real one - a real client
    tries to log in, then retries a lookup that cannot succeed, which cost one
    test in test_import_commit.py 7.3 seconds (2.1s of it retry backoff)."""
    return {
        "id": trackId,
        "name": name or f"Song {trackId}",
        "duration_ms": 200_000,
        "explicit": False,
        "disc_number": 1,
        "track_number": 1,
        "external_urls": {"spotify": f"https://open.spotify.com/track/{trackId}"},
        "external_ids": {"isrc": ""},
        "artists": [{"id": "a1", "name": artistName,
                     "external_urls": {"spotify": "https://open.spotify.com/artist/a1"}}],
        "album": {
            "id": f"{trackId}-album", "name": "Album One", "total_tracks": 1,
            "release_date": "2023-01-01", "images": [],
            "external_urls": {"spotify": f"https://open.spotify.com/album/{trackId}-album"},
            "artists": [{"id": "a1", "name": artistName,
                         "external_urls": {"spotify": "https://open.spotify.com/artist/a1"}}],
        },
    }


def makeDashboardDbMock() -> MagicMock:
    """A MagicMock db answering everything the dashboard route (`/`) reads, with
    empty-but-well-shaped values. Callers override only what their test is about.

    Shared because the route reads six things whose SHAPES matter - a MagicMock
    default would be a Mock where a dict or a tuple is expected, so every
    dashboard route test had to stub all six, and three files wrote out the same
    block. That is real coupling, not just repetition: adding a query to the
    dashboard means editing every one of them, which is how getListeningCalendar
    landed as three separate edits."""
    db = MagicMock()
    #< a real tzinfo, not a Mock: the milestone card formats achieved_at with it
    db.tz = datetime.timezone.utc
    db.repo.getUserSettings.return_value = {"default_dashboard_window": "day"}
    db.getOverallStats.return_value = {
        "currentTopSongs": [], "currentTopArtists": [],
        "totalSongsPlayed": 0, "totalDurationMs": 0,
        "previousSongsPlayed": 0, "previousDurationMs": 0,
    }
    db.getCurrentStreak.return_value = {"days": 0, "activeToday": False}
    db.getOnThisDay.return_value = []
    db.getPlayTotals.return_value = (0, 0)          #< lifetime totals feed the Next-milestones bars
    #< the calendar card only renders when weeks is non-empty, so an empty grid
    #  keeps unrelated tests from being perturbed by it
    db.getListeningCalendar.return_value = {
        "weeks": [], "monthLabels": [], "maxCount": 0, "activeDays": 0, "totalPlays": 0}
    return db


def wrappedCachedRow(topSongs=None, topArtists=None, topAlbums=None,
                     discoveredSongs=None, discoveredArtists=None, discoveredAlbums=None,
                     timeSeriesDay=None, timeSeriesWeek=None, timeSeriesMonth=None,
                     totalPlays=0, totalMs=0, longestStreak=0, peakDay=None, peakPlays=0,
                     uniqueSongs=0, uniqueArtists=0, discoveredSongsCount=0, discoveredArtistsCount=0):
    """A user_wrapped row exactly as Repository.getCachedWrapped returns it -
    every list-shaped arg is JSON-encoded automatically. Since R6 (2026-09-02)
    made the cache the only path dashboard._buildWrappedContext reads from,
    this is what a MagicMock db's `db.repo.getCachedWrapped.return_value`
    must be set to for any list/total to reach the rendered page - the old
    per-field `db.getTopSongs.return_value = [...]` mocks are dead once that
    branch is gone."""
    return {
        "total_plays": totalPlays, "total_ms": totalMs, "longest_streak": longestStreak,
        "peak_day": peakDay, "peak_plays": peakPlays,
        "unique_songs": uniqueSongs, "unique_artists": uniqueArtists,
        "discovered_songs": discoveredSongsCount, "discovered_artists": discoveredArtistsCount,
        "time_series_day": json.dumps(timeSeriesDay or []),
        "time_series_week": json.dumps(timeSeriesWeek or []),
        "time_series_month": json.dumps(timeSeriesMonth or []),
        "top_songs": json.dumps(topSongs or []),
        "top_artists": json.dumps(topArtists or []),
        "top_albums": json.dumps(topAlbums or []),
        "discovered_songs_list": json.dumps(discoveredSongs or []),
        "discovered_artists_list": json.dumps(discoveredArtists or []),
        "discovered_albums_list": json.dumps(discoveredAlbums or []),
    }


def makeDatabaseWithData(dbPath: Path, tracks: dict, entries: list, username: str = "testuser",
                         startWorkers: bool = False):
    """A Database instance backed by a fresh temp SQLite file, seeded with the
    given track catalog (dict of trackId -> Client.formatTrack-shaped dict, fields
    may be omitted - see normalizeTrackForTest) and play history (list of
    {id, playedAt, timePlayed, playedFrom} entries). The DB-backed replacement for
    the old in-memory tracksCache/entriesCache test fixture: every distinct track
    id referenced by `entries` gets at least a minimal placeholder row, since
    plays.track_id is a foreign key into tracks.id.

    Background workers are OFF here (unlike production): they spawned five
    threads per instance that parked on a randomized startup delay and then had
    to be joined at teardown, for work no assertion depended on. A test that
    actually exercises worker lifecycle passes startWorkers=True."""
    from Database.database import Database

    db = Database(username, dbPath=dbPath, startWorkers=startWorkers)

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

    def _makeDb(self, tracks, entries, username="testuser", startWorkers=False):
        """`startWorkers=True` for tests that assert on the background workers
        themselves (lifecycle, telemetry, stop events) - see
        makeDatabaseWithData for why they're off by default."""
        from Database.database import Database

        self._nextDbIndex += 1
        dbPath = Path(self._tmpdir.name) / f"test{self._nextDbIndex}.db"
        db = makeDatabaseWithData(dbPath, tracks, entries, username, startWorkers=startWorkers)
        self.addCleanup(db.repo.connectionManager.close)
        # Only the 5 always-on background workers, not stop() as a whole. These
        # are no-ops when startWorkers=False, but stay registered because a test
        # can start a worker explicitly after construction:
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
        self.addCleanup(Database._active_isrc_backfills.clear)
        return db


class RecordingConnection:
    """A connection that notes, for every statement, whether a transaction was
    already open when it ran. Enough to answer "was this read holding the
    write lock?" without a second thread and without a clock.

    Shared by test_wrapped_invalidation_scope (where the technique originated)
    and test_guard_transactions (the 2026-08-16 repo-wide sweep of the same
    defect class)."""

    def __init__(self, conn, log):
        self._conn = conn
        self._log = log

    def execute(self, sql, *args, **kwargs):
        self._log.append((" ".join(sql.split()), self._conn.in_transaction))
        return self._conn.execute(sql, *args, **kwargs)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._conn, name)
