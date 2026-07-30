"""Extracting Spotify's TOTP secret from its own web-player bundle.

The secrets are shipped as string literals in the JS the app ALREADY downloads
for persisted-query hashes (spotapi's get_sha256_hash). Each character's code
point is one secret byte - the same array the community mirrors publish - and
the first entry is the newest.

This file deliberately holds the REAL fragment captured from
web-player.5f92fe2d.js on 2026-07-30, unlike the module's own docstring, which
uses placeholders. The distinction is intentional: here the real bytes are
EVIDENCE - test_the_newest_matches_the_known_v61_array is what proves the whole
approach, and it can only prove it against genuine input. A dated sample does
not go stale the way documentation does; it stays a true record of what Spotify
served that day, and the parser must keep handling it regardless of what
version is current.

The parser anchors on the SHAPE ({secret:...,version:N}), never on the minified
identifiers, which change every build. `secret` and `version` are read by name
elsewhere in the bundle, so a minifier cannot rename them.
"""
import unittest

from Database.Spotify.totpSecret import (
    parseSecretsFromBundle, MIN_SECRET_BYTES, MAX_SECRET_BYTES,
)

# The real declaration, verbatim from web-player.5f92fe2d.js. Raw string so the
# doubled backslash in v60 stays exactly as the JS source has it. Covers all
# three quoting cases that occur live: single quotes containing a double quote,
# an escaped backslash, and a double-quoted literal.
LIVE_BUNDLE_FRAGMENT = r'''...,eL=r(84686).hp;let eD=[{secret:',7/*F("rLJ2oxaKL^f+E1xvP@N',version:61},{secret:'OmE{ZA.J^":0FG\\Uz?[@WW',version:60},{secret:"{iOFn;4}<1PFYKPV?5{%u14]M>/V0hDH",version:59}].map(e=>{var t;let r,i;return{secret:(t=e.secret,r=[],r="string"==typeof t?t.split("").map((e,t)=>e.charCodeAt(0)^t%33+9):t.map((e,t)=>e^t%33+9),...'''

KNOWN_V61 = [44, 55, 47, 42, 70, 40, 34, 114, 76, 74, 50, 111, 120,
             97, 75, 76, 94, 102, 43, 69, 49, 120, 118, 80, 64, 78]


class TestParsingTheLiveShape(unittest.TestCase):
    def test_it_finds_every_secret_in_the_real_fragment(self):
        found = parseSecretsFromBundle(LIVE_BUNDLE_FRAGMENT)

        self.assertEqual([version for version, _ in found], [61, 60, 59],
                         "newest first - the bundle's own [0] is the active one")

    def test_the_newest_matches_the_known_v61_array(self):
        """The check that proves the whole approach: byte-for-byte identical to
        what the community mirrors publish for version 61."""
        version, secret = parseSecretsFromBundle(LIVE_BUNDLE_FRAGMENT)[0]

        self.assertEqual(version, 61)
        self.assertEqual(list(secret), KNOWN_V61)

    def test_it_handles_the_three_quoting_forms_that_occur_live(self):
        found = dict(parseSecretsFromBundle(LIVE_BUNDLE_FRAGMENT))

        self.assertEqual(len(found[61]), 26)   #< single-quoted, contains a double quote
        self.assertEqual(len(found[60]), 22)   #< single-quoted, contains an escaped backslash
        self.assertEqual(len(found[59]), 32)   #< double-quoted

    def test_an_escaped_backslash_becomes_one_byte(self):
        r"""v60 ships as ...FG\\Uz... - two characters in the source, ONE
        backslash byte in the secret. Getting this wrong shifts every
        subsequent byte and yields a silently wrong TOTP."""
        found = dict(parseSecretsFromBundle(LIVE_BUNDLE_FRAGMENT))

        self.assertIn(ord("\\"), found[60])
        self.assertEqual(len(found[60]), 22)


class TestParsingRefusesJunk(unittest.TestCase):
    """A wrong secret is worse than no secret: it would be adopted, fail
    silently, and hide the real cause. Anything not clearly a secret is
    dropped rather than guessed at."""

    def test_a_bundle_without_secrets_yields_nothing(self):
        self.assertEqual(parseSecretsFromBundle("var a=1;function b(){return 2}"), [])

    def test_empty_input_is_not_an_error(self):
        self.assertEqual(parseSecretsFromBundle(""), [])
        self.assertEqual(parseSecretsFromBundle(None), [])

    def test_an_implausibly_short_secret_is_rejected(self):
        tooShort = "x" * (MIN_SECRET_BYTES - 1)

        self.assertEqual(parseSecretsFromBundle("{secret:'%s',version:99}" % tooShort), [])

    def test_an_implausibly_long_secret_is_rejected(self):
        tooLong = "x" * (MAX_SECRET_BYTES + 1)

        self.assertEqual(parseSecretsFromBundle("{secret:'%s',version:99}" % tooLong), [])

    def test_a_non_latin1_character_is_rejected(self):
        """charCodeAt gives a UTF-16 code unit; anything above 255 is not a
        byte, so the match is not the thing we are looking for."""
        secret = "abcdefghij中文"

        self.assertEqual(parseSecretsFromBundle("{secret:'%s',version:99}" % secret), [])

    def test_unrelated_secret_shaped_objects_do_not_match(self):
        """`version` must be numeric and adjacent - a config object that merely
        has a `secret` key is not a TOTP secret."""
        noise = "{secret:'someApiKeyValueHere',version:'1.2.3'},{secret:'other',name:'x'}"

        self.assertEqual(parseSecretsFromBundle(noise), [])

    def test_it_survives_a_realistic_amount_of_surrounding_noise(self):
        haystack = ("x" * 50000) + LIVE_BUNDLE_FRAGMENT + ("y" * 50000)

        found = parseSecretsFromBundle(haystack)

        self.assertEqual(found[0][0], 61)
        self.assertEqual(list(found[0][1]), KNOWN_V61)


if __name__ == "__main__":
    unittest.main()
