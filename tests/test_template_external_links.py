# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Every link opened in a new tab carries rel="noreferrer noopener".

target="_blank" without it leaks the referring URL (with whatever page state
its query string carries) to the destination site, and in older browsers hands
the opened page a window.opener back into ours. The pair is already the house
convention on every other such link; this sweep pins the invariant so the next
external link cannot quietly regress it - two links had, which is how this
test came to exist.
"""
import os
import sys
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
REQUIRED_REL_TOKENS = ("noreferrer", "noopener")


class TestExternalLinkRel(unittest.TestCase):
    def test_every_target_blank_link_carries_the_rel_pair(self):
        self.assertTrue(TEMPLATES_DIR.is_dir(), TEMPLATES_DIR)
        missing = []
        checked = 0
        for template in sorted(TEMPLATES_DIR.rglob("*.html")):
            soup = BeautifulSoup(template.read_text(encoding="utf-8"), "html.parser")
            for anchor in soup.find_all("a", attrs={"target": "_blank"}):
                checked += 1
                #< html.parser hands rel back as a token list (multi-valued attr)
                relTokens = anchor.get("rel") or []
                if any(token not in relTokens for token in REQUIRED_REL_TOKENS):
                    missing.append(f"{template.name}: {anchor.get('href')}")
        self.assertGreater(checked, 0, "the sweep found no target=_blank links at all - "
                                       "selector or template layout changed under it")
        self.assertEqual(missing, [],
                         "target=\"_blank\" links missing rel=\"noreferrer noopener\"")


if __name__ == "__main__":
    unittest.main()
