# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real spotapi exceptions, in the shapes the live listener actually meets.

Exception('...some text...') stand-ins pinned the classifier to messages
spotapi never produces (RequestError("Got status 429 from server") - the
2026-09-05 review). These drive spotapi's own TLSClient the way the network
does, so a test holds the exact type, message and .error detail:

- a TRANSPORT failure (reset, timeout, DNS): TLSClient.build_request wraps
  curl_cffi's RequestException in RequestError("Failed to complete request.",
  error=<curl detail>) - NO status anywhere in str(exc);
- a NON-2xx on the profile endpoint: TLSClient.get sends with danger=True and
  Login.__init__ sets fail_exception = LoginError, so _send raises
  LoginError("Could not GET <url>. Status Code: NNN", error="Request Failed.")
  - the status rides in the MESSAGE, the detail is a constant.

No network: the client's request method is replaced before the call."""
from unittest.mock import MagicMock

PROFILE_URL = "https://www.spotify.com/api/account-settings/v1/profile"
CURL_CONNECTION_RESET = ("Failed to perform, curl: (56) Recv failure: Connection was reset. "
                         "See https://curl.se/libcurl/c/libcurl-errors.html first for more details.")
_IMPERSONATE = "chrome_120"
_NO_PROXY = ""
_SINGLE_ATTEMPT = 1


class _FakeCurlResponse:
    """Enough of curl_cffi's Response for spotapi's parse_response."""

    def __init__(self, status, body, contentType="text/html"):
        self.status_code = status
        self.text = body
        self.headers = {"content-type": contentType}
        self.url = PROFILE_URL

    def json(self):
        import json
        return json.loads(self.text)


def _realTlsClient():
    import spotapi
    return spotapi.TLSClient(_IMPERSONATE, _NO_PROXY, auto_retries=_SINGLE_ATTEMPT)


def realTransportRequestError(curlDetail=CURL_CONNECTION_RESET):
    """The RequestError a connection reset produces (see module docstring)."""
    from curl_cffi.requests.exceptions import RequestException
    from spotapi.exceptions.errors import RequestError
    client = _realTlsClient()
    try:
        client.request = MagicMock(side_effect=RequestException(curlDetail))
        try:
            client.build_request("GET", PROFILE_URL)
        except RequestError as exc:
            return exc
        raise AssertionError("TLSClient.build_request did not raise RequestError")
    finally:
        client.close()


def realProfileStatusError(status, body="<html>fallback page</html>"):
    """The LoginError a non-2xx on the profile endpoint produces (see module
    docstring) - a 503 outage, a 429 rate limit, a 403 refusal all look alike
    apart from the number in the message."""
    from spotapi.exceptions.errors import LoginError
    client = _realTlsClient()
    try:
        client.fail_exception = LoginError   #< exactly what Login.__init__ sets
        client.request = MagicMock(return_value=_FakeCurlResponse(status, body))
        try:
            client.get(PROFILE_URL)
        except LoginError as exc:
            return exc
        raise AssertionError(f"TLSClient.get did not raise LoginError for status {status}")
    finally:
        client.close()
