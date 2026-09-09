"""Token refresh cooldown is isolated by client and never blocks HTTP threads."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import Database.Listeners.spotifyListener as listenerModule
from Database.rate_limit import SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS


CLOCK_START = 100.0
RETRY_SECONDS = 60.0
SHORT_RETRY_SECONDS = 10.0
BEFORE_DEADLINE_SECONDS = 1.0
THREAD_TIMEOUT_SECONDS = 5
CONCURRENT_REQUESTS = 2
HTTP_OK = 200
HTTP_RATE_LIMITED = 429


@pytest.fixture
def clock(monkeypatch):
    current = [CLOCK_START]
    monkeypatch.setattr(listenerModule, "time", SimpleNamespace(monotonic=lambda: current[0]))
    monkeypatch.setattr(listenerModule, "_accountsTokenBackoffUntil", {}, raising=False)
    return current


def response(status=HTTP_OK, retryAfter=None):
    result = MagicMock(status_code=status)
    result.headers = {} if retryAfter is None else {"Retry-After": str(retryAfter)}
    result.json.return_value = {"access_token": "valid-token"}
    return result


def test_same_client_skips_until_exact_deadline_while_other_client_continues(clock):
    with patch("requests.post", side_effect=[response(HTTP_RATE_LIMITED, RETRY_SECONDS),
                                            response(), response()]) as post:
        assert listenerModule._refresh_spotify_access_token("client", "secret", "grant") is None
        assert listenerModule._refresh_spotify_access_token("client", "secret", "other-grant") is None
        post.assert_called_once()
        assert listenerModule._refresh_spotify_access_token("other-client", "secret", "grant") == "valid-token"
        clock[0] = CLOCK_START + RETRY_SECONDS - BEFORE_DEADLINE_SECONDS
        assert listenerModule._refresh_spotify_access_token("client", "secret", "grant") is None
        clock[0] = CLOCK_START + RETRY_SECONDS
        assert listenerModule._refresh_spotify_access_token("client", "secret", "grant") == "valid-token"
    assert "client" not in listenerModule._accountsTokenBackoffUntil


@pytest.mark.parametrize("header,expected", [
    (None, SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS),
    ("malformed", SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS),
    (listenerModule.WEB_API_MAX_BACKOFF_SECONDS * 2, listenerModule.WEB_API_MAX_BACKOFF_SECONDS),
    ("0", 0.0),
])
def test_retry_after_parser_controls_deadline(clock, header, expected):
    with patch("requests.post", return_value=response(HTTP_RATE_LIMITED, header)) as post:
        assert listenerModule._refresh_spotify_access_token("client", "secret", "grant") is None
    post.assert_called_once()
    assert listenerModule._accountsTokenBackoffUntil["client"] == CLOCK_START + expected


def test_late_shorter_429_cannot_shorten_an_existing_cooldown(clock):
    longEntered = threading.Event()
    shortEntered = threading.Event()
    releaseLong = threading.Event()
    releaseShort = threading.Event()

    def overlappingPost(url, *, data, **kwargs):
        isLong = data["refresh_token"] == "long-grant"
        entered, release = (longEntered, releaseLong) if isLong else (shortEntered, releaseShort)
        entered.set()
        assert release.wait(THREAD_TIMEOUT_SECONDS)
        return response(HTTP_RATE_LIMITED, RETRY_SECONDS if isLong else SHORT_RETRY_SECONDS)

    with patch("requests.post", side_effect=overlappingPost):
        with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as pool:
            longRequest = pool.submit(listenerModule._refresh_spotify_access_token, "client", "secret", "long-grant")
            shortRequest = pool.submit(listenerModule._refresh_spotify_access_token, "client", "secret", "short-grant")
            try:
                assert longEntered.wait(THREAD_TIMEOUT_SECONDS)
                assert shortEntered.wait(THREAD_TIMEOUT_SECONDS)
                releaseLong.set()
                assert longRequest.result(timeout=THREAD_TIMEOUT_SECONDS) is None
                releaseShort.set()
                assert shortRequest.result(timeout=THREAD_TIMEOUT_SECONDS) is None
            finally:
                releaseLong.set()
                releaseShort.set()
    assert listenerModule._accountsTokenBackoffUntil["client"] == CLOCK_START + RETRY_SECONDS
