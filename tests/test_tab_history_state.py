# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static guard: every AJAX tab switcher seeds its initial history entry.

`admin-page.js` and `profile-page.js` are twins - both intercept subnav
clicks, swap the body in place, `pushState` the new URL, and re-render from
`popstate`. That popstate handler is guarded on `e.state`, because a popstate
carrying someone else's state (or none) is not this switcher's to handle.

The entry the browser creates for the FIRST page load has `state === null`, so
without an explicit prime it never matches that guard: pressing Back after a
tab switch pops to it, the handler declines, and the URL reverts to the first
tab while the DOM keeps showing the second one - until a refresh silently
changes what is on screen. admin-page.js got the prime; its twin didn't, which
is the drift this pins.

`null` is not the only unprimed shape. The prime used to be guarded on
`!history.state`, which also skipped an entry whose state was written by
somebody else - and a foreign state fails the popstate check for exactly the
same reason `null` does. Both modules now test for their OWN key.

Cheap file-level assertions - no browser needed.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_JS_DIR = os.path.join(_ROOT, "static", "js")

# module -> the history.state key it owns. Explicit rather than derived: this
# mapping is what a reader needs to add the next tab switcher.
_TAB_SWITCHERS = {
    "admin-page.js": "adminTab",
    "profile-page.js": "profileTab",
}


def _readModule(module):
    with open(os.path.join(_JS_DIR, module), encoding="utf-8") as fh:
        return fh.read()


class TestTabSwitcherHistoryState(unittest.TestCase):
    def test_every_switcher_still_pushes_its_own_state_key(self):
        """Guards the map: a module that stopped pushStateing tab navigation
        has no history entries to prime, and its entry below would assert
        nothing."""
        for module, stateKey in _TAB_SWITCHERS.items():
            with self.subTest(module=module):
                self.assertRegex(
                    _readModule(module),
                    r"history\.pushState\(\s*\{\s*" + re.escape(stateKey),
                    f"{module} no longer pushes a {stateKey} state")

    def test_every_switcher_declines_a_popstate_that_is_not_its_own(self):
        """The guard that makes priming necessary in the first place."""
        for module, stateKey in _TAB_SWITCHERS.items():
            with self.subTest(module=module):
                self.assertRegex(
                    _readModule(module),
                    r"e\.state\s*&&\s*e\.state\." + re.escape(stateKey),
                    f"{module}'s popstate handler no longer checks e.state")

    def test_every_switcher_primes_the_initial_history_entry(self):
        """The first page load's entry has state === null, so Back from a
        swapped tab lands on an entry the handler above refuses to re-render:
        the URL says one tab, the DOM shows another.

        The condition is "does this entry carry MY key", not "does it carry a
        state at all" - see the module docstring."""
        for module, stateKey in _TAB_SWITCHERS.items():
            with self.subTest(module=module):
                source = _readModule(module)
                self.assertRegex(
                    source,
                    r"!\(\s*history\.state\s*&&\s*history\.state\." + re.escape(stateKey),
                    f"{module} treats any state as primed, so an entry holding somebody "
                    "else's state is left unseeded and Back over it does nothing")
                self.assertRegex(
                    source,
                    r"history\.replaceState\(\s*\{\s*" + re.escape(stateKey),
                    f"{module} pushes {stateKey} states but never seeds the initial "
                    "entry with one, so Back after a tab switch leaves the URL and the "
                    "rendered tab disagreeing")


if __name__ == "__main__":
    unittest.main()
