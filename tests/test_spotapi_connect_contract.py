# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline cross-layer contracts against the installed SpotAPI dependency.

Fixture provenance: pushedCluster/pushFrame reuse the synthetic schema verified
against Phase 0 dealer captures (2026-07-30): payloads[].cluster.player_state,
devices and active_device_id. Last source/fixture validation: 2026-09-09 against
TzurSoffer/SpotAPI d538654574e2fb9511d1d09012aee962d380df8c (requirements.txt).
A dependency bump or observed protocol incident must re-check these fixtures
against upstream source or sanitized known-good frames; offline success cannot
establish the current live wire format.

Existing contract coverage (do not duplicate the transport rewrite):
* constructor cleanup: test_listener_login_failure.TestAFailedConstructionRetiresItsSession;
  initial packets and @enforce method resolution: test_patches.TestEnforceShadowRemoval;
* renewed base.access_token/_Undefined and expiry: TestReconnectRefreshesTheAccessToken;
  ws/rlock/connection_id publication: TestReconnectInitPacketHandling,
  TestReconnectYieldsToAStopThatLandedMidHandshake, TestReconnectSerialization;
* recv timeout, ws_dump, deliberate close and bounded failure counts:
  TestPatchedGetPacket and TestPatchedKeepAlive;
* push channel/renewal stamps, idle versus stale and rate limiting:
  test_recently_played_loop, test_playback_recovery and test_listener_*.

The gap covered here is one complete wire frame through the real PlayerStatus
cache/properties and Listener reader, including active-to-idle polling. Only
socket/HTTP transport is faked. Constructor lifecycle is covered separately.
"""

import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from spotapi.status import PlayerStatus

import Database.patches  # noqa: F401 - install the application's compatibility patches
from Database.Listeners.spotifyListener import Listener
from Database.Spotify.recentlyPlayed import _adoptCluster, _clusterFromPacket
from test_patches import _FakeWebsocket
from test_recently_played_loop import pushedCluster, pushFrame


RECEIVE_TIMEOUT_SECONDS = 0.25
RENEWAL_TIME = 1234.5
NEXT_TRACK_URI = "spotify:track:next"


@pytest.fixture
def player():
    # Keep real upstream slots, properties and method lookup; lifecycle has its
    # own construction tests. Neither real sockets nor worker threads start.
    manager = PlayerStatus.__new__(PlayerStatus)
    manager.rlock = threading.Lock()
    manager.ws_dump = None
    manager.device_id = "contract-device"
    manager.connection_id = "contract-connection"
    manager.client = SimpleNamespace(put=Mock(side_effect=AssertionError("unexpected HTTP")))
    return manager


@pytest.fixture
def listener(player):
    reader = Listener.__new__(Listener)
    reader.sp = SimpleNamespace(lastPlayedManager=SimpleNamespace(manager=player))
    return reader


def adoptFrame(player, frame):
    player.ws = _FakeWebsocket([json.dumps(frame)])
    packet = player.get_packet(timeout=RECEIVE_TIMEOUT_SECONDS)
    cluster = _clusterFromPacket(packet)
    return cluster is not None and _adoptCluster(player, cluster)


def test_complete_frames_refresh_real_saved_state_without_mutating_or_polling(player, listener):
    for uri in (pushedCluster()["player_state"]["track"]["uri"], NEXT_TRACK_URI):
        frame = pushFrame(pushedCluster(trackUri=uri))
        expected = copy.deepcopy(frame)
        assert adoptFrame(player, frame)

        # Repeat conversion: upstream Track.from_dict mutates its input. The
        # patched property must isolate that mutation on every access.
        assert player.saved_state.track.uri == uri
        assert player.saved_state.track.metadata.title == "Song"
        assert listener.getConnectPlayerState() == expected["payloads"][0]["cluster"]["player_state"]
        assert player._device_dump == expected["payloads"][0]["cluster"]
        assert player.ws_dump == expected
        assert player.ws.recvTimeouts == [RECEIVE_TIMEOUT_SECONDS]
    player.client.put.assert_not_called()


@pytest.mark.parametrize("frame", [
    {"type": "pong"},
    {"payloads": {"cluster": pushedCluster()}},
    {"payloads": [None, [], "malformed", {"cluster": []}]},
    {"payloads": [{"cluster": {"devices": {}}}]},
    {"payloads": [{"cluster": {"player_state": None}}]},
    {"payloads": [{"cluster": {"player_state": []}}]},
])
def test_unrelated_or_malformed_push_does_not_erase_active_cache(player, listener, frame):
    assert adoptFrame(player, pushFrame(pushedCluster()))
    active = copy.deepcopy(listener.getConnectPlayerState())
    assert not adoptFrame(player, frame)
    assert listener.getConnectPlayerState() == active
    player.client.put.assert_not_called()


def test_valid_payload_after_unrelated_entries_is_still_adopted(player, listener):
    frame = pushFrame(pushedCluster())
    frame["payloads"][:0] = [None, {"deviceBroadcastStatus": {}}]
    assert adoptFrame(player, frame)
    assert listener.getConnectPlayerState()["track"]["uri"] == pushedCluster()["player_state"]["track"]["uri"]


def test_successful_stateless_poll_clears_active_playback_and_proves_liveness(player, listener):
    assert adoptFrame(player, pushFrame(pushedCluster()))
    assert listener.getConnectPlayerState() is not None
    idleCluster = {"devices": {}}
    player.client.put.side_effect = None
    player.client.put.return_value = SimpleNamespace(fail=False, response=idleCluster)

    with patch("Database.patches.time.monotonic", return_value=RENEWAL_TIME):
        with pytest.raises(ValueError, match="Could not get player state"):
            _ = player.state

    assert listener.getConnectPlayerState() is None
    assert player._device_dump == idleCluster
    assert player._devices == {}
    assert player.stateRenewalSucceededAt == RENEWAL_TIME
    player.client.put.assert_called_once()
    assert player.client.put.call_args.kwargs["headers"] == {
        "x-spotify-connection-id": player.connection_id,
    }
