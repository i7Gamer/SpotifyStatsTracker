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


@pytest.fixture(autouse=True)
def _blockNetwork(monkeypatch):
    def guardedConnect(self, address):
        raise RuntimeError(
            f"Test attempted a real network connection to {address!r} - mock the "
            "HTTP call (e.g. patch requests.get / SpotipyFree) instead."
        )

    monkeypatch.setattr(socket.socket, "connect", guardedConnect)


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
        self._nextDbIndex += 1
        dbPath = Path(self._tmpdir.name) / f"test{self._nextDbIndex}.db"
        db = makeDatabaseWithData(dbPath, tracks, entries, username)
        self.addCleanup(db.repo.connectionManager.close)
        return db
