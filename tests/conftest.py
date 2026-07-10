"""Shared test-suite guards.

No test in this suite should ever reach the real network: everything external
(Spotify API, image CDNs) must be mocked. A missed mock used to fail silently -
or worse, pass while hammering open.spotify.com - so real socket connections
are blocked for every test and raise instead.
"""
import socket

import pytest


@pytest.fixture(autouse=True)
def _blockNetwork(monkeypatch):
    def guardedConnect(self, address):
        raise RuntimeError(
            f"Test attempted a real network connection to {address!r} - mock the "
            "HTTP call (e.g. patch requests.get / SpotipyFree) instead."
        )

    monkeypatch.setattr(socket.socket, "connect", guardedConnect)
