"""Refresh verdicts must not become bearer tokens or repeated reauth emails."""
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from Database.database import Database
from Database.repository import Repository
from Database.Listeners.spotifyListener import REFRESH_TOKEN_REVOKED, _refresh_spotify_access_token
from Database.workers.metadata_backfiller import _CycleAccessToken


REPEATED_CYCLES = 5
CONCURRENT_CALLERS = 2
THREAD_TIMEOUT_SECONDS = 5
HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
INVALID_NUMERIC_TOKEN = 123


@pytest.fixture
def tokenDb(tmp_path):
    # Only the repository and token consumers are needed; no workers or app.
    db = Database.__new__(Database)
    db.user = "alice"
    db.repo = Repository(tmp_path / "tokens.db")
    db.repo.upsertUser(db.user, "alice@example.com")
    db._webApiTokenCache = None
    yield db
    db.repo.connectionManager.close()


@pytest.mark.parametrize("token", [None, "", object(), "usable-token"])
def test_non_revocation_results_never_request_reauth(tokenDb, token):
    with patch("services.email_worker.queue_email_notification") as notify:
        for _ in range(REPEATED_CYCLES):
            cycle = _CycleAccessToken(lambda: token)
            cycle()
            tokenDb._noteTokenHealth(cycle, True)
    assert not tokenDb.repo.getSpotifyNeedsReauth(tokenDb.user)
    notify.assert_not_called()


def test_revocation_flags_immediately_and_queues_once_across_cycles(tokenDb):
    with patch("services.email_worker.queue_email_notification") as notify:
        for _ in range(REPEATED_CYCLES):
            mint = MagicMock(return_value=REFRESH_TOKEN_REVOKED)
            cycle = _CycleAccessToken(mint)
            assert cycle() is REFRESH_TOKEN_REVOKED
            tokenDb._noteTokenHealth(cycle, True)
            tokenDb._noteTokenHealth(cycle, True)
            assert tokenDb.repo.getSpotifyNeedsReauth(tokenDb.user)
            mint.assert_called_once_with()
    notify.assert_called_once_with(tokenDb.user, "api_key_failed")


def test_unused_cycle_does_not_mint_or_change_health(tokenDb):
    mint = MagicMock(return_value=REFRESH_TOKEN_REVOKED)
    cycle = _CycleAccessToken(mint)
    tokenDb._noteTokenHealth(cycle, True)
    mint.assert_not_called()
    assert not cycle.noted
    assert not tokenDb.repo.getSpotifyNeedsReauth(tokenDb.user)


def test_missing_credentials_are_not_a_revocation(tokenDb):
    cycle = _CycleAccessToken(lambda: REFRESH_TOKEN_REVOKED)
    cycle()
    tokenDb._noteTokenHealth(cycle, False)
    assert not tokenDb.repo.getSpotifyNeedsReauth(tokenDb.user)


def test_unscoped_token_success_cannot_clear_existing_reauth_flag(tokenDb):
    tokenDb.repo.setSpotifyNeedsReauth(tokenDb.user, True)
    cycle = _CycleAccessToken(lambda: "usable-token")
    cycle()
    tokenDb._noteTokenHealth(cycle, True)
    assert tokenDb.repo.getSpotifyNeedsReauth(tokenDb.user)


def test_notification_only_on_a_real_false_to_true_transition(tokenDb):
    with patch("services.email_worker.queue_email_notification") as notify:
        tokenDb.setSpotifyNeedsReauth(False)
        notify.assert_not_called()
        tokenDb.setSpotifyNeedsReauth(True)
        tokenDb.setSpotifyNeedsReauth(True)
        notify.assert_called_once_with(tokenDb.user, "api_key_failed")
        tokenDb.setSpotifyNeedsReauth(False)
        notify.assert_called_once_with(tokenDb.user, "api_key_failed")
        notify.reset_mock()
        tokenDb.setSpotifyNeedsReauth(True)
        notify.assert_called_once_with(tokenDb.user, "api_key_failed")


def test_repository_reports_transition_and_missing_user(tokenDb):
    repo = tokenDb.repo
    assert repo.setSpotifyNeedsReauth("missing", True) is False
    assert repo.setSpotifyNeedsReauth(tokenDb.user, False) is False
    assert repo.setSpotifyNeedsReauth(tokenDb.user, True) is True
    assert repo.setSpotifyNeedsReauth(tokenDb.user, True) is False
    assert repo.setSpotifyNeedsReauth(tokenDb.user, False) is True


def test_concurrent_connections_only_enqueue_one_notification(tokenDb):
    barrier = threading.Barrier(CONCURRENT_CALLERS)
    dbPath = tokenDb.repo.connectionManager.dbPath

    def flagFromSeparateConnection():
        db = Database.__new__(Database)
        db.user = tokenDb.user
        db.repo = Repository(dbPath)
        try:
            db.repo.connection()
            barrier.wait(timeout=THREAD_TIMEOUT_SECONDS)
            db.setSpotifyNeedsReauth(True)
        finally:
            db.repo.connectionManager.close()

    with patch("services.email_worker.queue_email_notification") as notify:
        with ThreadPoolExecutor(max_workers=CONCURRENT_CALLERS) as pool:
            futures = [pool.submit(flagFromSeparateConnection) for _ in range(CONCURRENT_CALLERS)]
            for future in futures:
                future.result(timeout=THREAD_TIMEOUT_SECONDS)
    notify.assert_called_once_with(tokenDb.user, "api_key_failed")


@pytest.mark.parametrize("token", [REFRESH_TOKEN_REVOKED, None, "", object()])
def test_unusable_artist_token_is_never_cached_or_sent(tokenDb, token):
    creds = {"client_id": "client", "client_secret": "secret", "refresh_token": "refresh"}
    tokenDb.getUserSpotifyCredentials = MagicMock(return_value=creds)
    with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value=token) as mint, \
         patch.object(tokenDb, "_mediaGet") as get, \
         patch("Database.Spotify.Spotify") as cookieClient:
        cookieClient.return_value.artist.return_value = {"images": [{"url": "https://example.test/fallback"}]}
        assert tokenDb._cachedWebApiAccessToken(creds) is None
        assert tokenDb._webApiTokenCache is None
        assert tokenDb._fetchArtistImageUrl("artist") == "https://example.test/fallback"
        get.assert_not_called()
        expectedMintAttempts = 2
        assert mint.call_count == expectedMintAttempts


def test_revocation_during_401_refresh_does_not_retry_with_sentinel(tokenDb):
    tokenDb.getUserSpotifyCredentials = MagicMock(return_value={
        "client_id": "client", "client_secret": "secret", "refresh_token": "refresh"})
    with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
               side_effect=["expired-token", REFRESH_TOKEN_REVOKED]), \
         patch.object(tokenDb, "_mediaGet", return_value=MagicMock(status_code=HTTP_UNAUTHORIZED)) as get, \
         patch("Database.Spotify.Spotify") as cookieClient:
        cookieClient.return_value.artist.return_value = {"images": []}
        assert tokenDb._fetchArtistImageUrl("artist") is None
    get.assert_called_once()
    assert tokenDb._webApiTokenCache is None


@pytest.mark.parametrize("token", [None, "", True, INVALID_NUMERIC_TOKEN, ["token"], {"token": "value"}])
def test_malformed_successful_refresh_never_returns_an_unusable_token(token, caplog):
    response = MagicMock(status_code=HTTP_OK)
    response.json.return_value = {"access_token": token}
    with patch("requests.post", return_value=response):
        assert _refresh_spotify_access_token("client", "secret", "grant") is None
    assert "no usable access token" in caplog.text
