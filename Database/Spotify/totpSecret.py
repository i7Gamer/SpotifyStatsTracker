# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Recover Spotify's current TOTP secret from Spotify's own web player.

The secret the web player authenticates `open.spotify.com/api/token` with is
shipped, as plain string literals, in the very JS bundle this application
already downloads for persisted-query hashes. Its shape, as observed on
2026-07-30 (values below are ILLUSTRATIVE - the real ones are minified junk,
and the only copy this codebase keeps is the pin in Database/patches.py, so
that there is exactly one place to update after a rotation):

    let eD=[{secret:'<22-32 printable ASCII characters>',version:61},
            {secret:'<the previous one, may contain \\ or ">',version:60},
            {secret:"<double quotes occur too>",version:59}]
      .map(e=>{ ... t.split("").map((e,t)=>e.charCodeAt(0)^t%33+9) ... })[0]

Each character's code point is one byte of the secret - the same arrays the
community mirrors republish - and `[0]` selects the newest. Verified on
2026-07-30: the extracted bytes matched the mirror's published array exactly,
and generated the same TOTP spotapi produced in the same second. The real
captured fragment lives in tests/test_totp_secret.py, where it belongs: a
dated sample the parser is tested against, not documentation that would go
stale at the next rotation.

This exists so a rotation can be survived without depending on anyone else's
repository staying alive. It is NOT the normal path: Database/patches.py pins a
known-good secret and only calls in here once a rotation has actually been
confirmed. See the block comment there.

Both requests are unauthenticated - no cookies, no token - which is precisely
why they still work during a rotation, when everything token-dependent is
failing.
"""

import logging
import re

import requests

logger = logging.getLogger(__name__)

WEB_PLAYER_HOME = "https://open.spotify.com"

# Anchored on the SHAPE of the data, never on the surrounding identifiers:
# minification renames `eD`/`eU`/`eN` on every build, but `secret` and
# `version` are read by name elsewhere in the bundle, so they survive.
SECRET_PATTERN = re.compile(
    r"\{\s*secret\s*:\s*(?P<quote>['\"])(?P<secret>(?:\\.|(?!(?P=quote)).)*)(?P=quote)"
    r"\s*,\s*version\s*:\s*(?P<version>\d+)\s*\}"
)

# Observed lengths are 22-32 bytes; this is a sanity band, not a spec. Its job
# is to reject a coincidental match, because adopting a wrong secret is worse
# than adopting none - it would fail silently and hide the real cause.
MIN_SECRET_BYTES = 8
MAX_SECRET_BYTES = 128

MAX_BYTE_VALUE = 255   #< charCodeAt yields a UTF-16 code unit; >255 is not a secret byte

REQUEST_TIMEOUT_SECONDS = 15
# Enough to hold the main pack (~4.5 MB in July 2026) with room to grow, and
# small enough that a redirect to something unexpected can't exhaust memory.
MAX_BUNDLE_BYTES = 32 * 1024 * 1024

# The bundle is served to browsers; ask for it as one.
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Only the MAIN pack carries the secrets. spotapi additionally walks ~70 chunk
# files for query hashes; recovery deliberately does not - two requests, not
# seventy-three.
_MAIN_PACK_MARKER = "web-player/web-player"

_JS_STRING_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _unescapeJsString(raw: str) -> str:
    r"""A JS string literal's body as its actual characters. Only the escapes
    that occur here matter - the live v60 secret contains \\, which is ONE
    backslash byte; reading it as two shifts every following byte and yields a
    silently wrong TOTP."""
    return re.sub(r"\\(.)", lambda m: _JS_STRING_ESCAPES.get(m.group(1), m.group(1)), raw)


def parseSecretsFromBundle(bundleText) -> list:
    """Every {secret, version} pair in the text, newest version first, as
    (version, bytearray). Implausible matches are dropped rather than repaired:
    this is pattern-matching against someone else's minified output, so a
    confident wrong answer is the failure mode to avoid."""
    if not bundleText:
        return []

    found = {}
    for match in SECRET_PATTERN.finditer(bundleText):
        characters = _unescapeJsString(match.group("secret"))
        if not MIN_SECRET_BYTES <= len(characters) <= MAX_SECRET_BYTES:
            continue
        codePoints = [ord(character) for character in characters]
        if any(point > MAX_BYTE_VALUE for point in codePoints):
            continue
        version = int(match.group("version"))
        found.setdefault(version, bytearray(codePoints))

    return sorted(found.items(), key=lambda pair: -pair[0])


def fetchWebPlayerSecrets(timeout: float = REQUEST_TIMEOUT_SECONDS) -> list:
    """Download Spotify's main web-player pack and parse the secrets out of it.

    Two unauthenticated GETs: the home page (to learn the current pack's hashed
    URL) and the pack itself. Returns [] on any failure - a caller in this
    situation is already degraded, and an exception here would replace a
    diagnosable auth error with an unrelated network one."""
    # Closed in the finally below rather than left to the garbage collector.
    # Constructed outside the try on purpose: it opens no socket and cannot
    # realistically raise, and doing it here means `session` is always bound for
    # that finally. Same leak shape as _verifyCookiesMatchEmail's (fixed in
    # 95d89b6) - a pooled TLS connection per call, kept alive by the Session
    # object, on a path that runs whenever Spotify rotates its TOTP secret.
    session = requests.Session()
    try:
        session.headers["User-Agent"] = _BROWSER_USER_AGENT

        home = session.get(WEB_PLAYER_HOME, timeout=timeout)
        home.raise_for_status()

        packUrl = _findMainPackUrl(home.text)
        if not packUrl:
            logger.warning("No web-player pack URL found on %s - Spotify may have "
                           "restructured its page.", WEB_PLAYER_HOME)
            return []

        bundle = session.get(packUrl, timeout=timeout, stream=True)
        bundle.raise_for_status()
        text = _readCapped(bundle)
        if text is None:
            logger.warning("Web-player pack at %s exceeded %d bytes; not reading it.",
                           packUrl, MAX_BUNDLE_BYTES)
            return []

        secrets = parseSecretsFromBundle(text)
        logger.info("Read %d TOTP secret(s) from %s: version(s) %s",
                    len(secrets), packUrl, [version for version, _ in secrets] or "none")
        return secrets
    except Exception as e:  # noqa: BLE001 - recovery must never raise into the auth path
        logger.warning("Could not read the TOTP secret from Spotify's web player: %s", e)
        return []
    finally:
        #< also drops the streamed pack response's connection back to the pool,
        #  which _readCapped may have stopped reading early on an over-cap body
        session.close()


def _findMainPackUrl(homeHtml: str):
    """The hashed URL of the main web-player pack, or None. Same selection
    spotapi's get_session makes, done here without needing its client."""
    for url in re.findall(r"https://[^\"'\s]+\.js", homeHtml):
        if _MAIN_PACK_MARKER in url:
            return url
    return None


def _readCapped(response):
    """The response body as text, or None if it runs past MAX_BUNDLE_BYTES."""
    chunks, total = [], 0
    for chunk in response.iter_content(chunk_size=1 << 16):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_BUNDLE_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")
