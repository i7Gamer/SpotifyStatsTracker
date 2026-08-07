"""Static guard: a Top-list entry that CLIMBED is tinted positive, not accent.

The rank-movement badge (templates/_top_list_movement.html) once tinted "up"
with `var(--accent)`, which is theme-scoped - and two of the four themes set the
accent to a red (`#FB717B` rose, `#E50914` red). Against the fixed `var(--danger)`
red on "down", that made an improvement and a decline the same colour, so the
badge's only quick-glance signal said the opposite of what happened.

What is pinned here:

  * "up" names a positive colour that is NOT the accent, so no theme can turn it
    red again,
  * "down" still names `--danger`, and the two never resolve to the same value,
  * neither semantic token is theme-scoped - the pairing has to hold in every
    theme, not just the one it was checked in, and
  * the app has ONE positive green: the summary deltas' `.change-positive` and
    this badge resolve to the same token rather than two hexes that can drift.

Colour is a redundant cue here, never the only one - the glyph and the
visually-hidden text carry direction for anyone who cannot use it (see the
stylesheet comment above `.rank-move`). That is what makes this a polish fix
rather than an accessibility bug, and it is asserted in test_top_list_movement.py.

Cheap file-level assertions - no browser needed.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")

_POSITIVE_TOKEN = "--success"
_NEGATIVE_TOKEN = "--danger"
#< The theme-scoped token "up" must never name again, whatever it is worth today.
_THEMED_TOKEN = "var(--accent)"


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestRankMoveColour(unittest.TestCase):
    def setUp(self):
        #< Comments out, so a token that a comment merely MENTIONS - or one
        #  commented out entirely - can never satisfy a guard below. It also
        #  frees the declaration regexes from having to allow a `*/` boundary.
        self.css = re.sub(r"/\*.*?\*/", "", _readFile(_CSS_PATH), flags=re.S)

    def test_a_climb_is_not_tinted_with_the_theme_accent(self):
        """The regression itself: two themes make the accent a red."""
        self.assertNotEqual(self._declaration(".rank-move-up", "color"), _THEMED_TOKEN)

    def test_a_climb_is_tinted_with_the_positive_token(self):
        self.assertEqual(self._declaration(".rank-move-up", "color"),
                         f"var({_POSITIVE_TOKEN})")

    def test_a_fall_is_still_tinted_with_the_danger_token(self):
        self.assertEqual(self._declaration(".rank-move-down", "color"),
                         f"var({_NEGATIVE_TOKEN})")

    def test_a_climb_and_a_fall_never_resolve_to_the_same_colour(self):
        up = self._resolve(self._declaration(".rank-move-up", "color"))
        down = self._resolve(self._declaration(".rank-move-down", "color"))

        self.assertRegex(up, r"^#[0-9a-fA-F]{3,8}$",
                         "the positive token must bottom out in a literal colour")
        self.assertNotEqual(up, down)

    def test_neither_semantic_token_is_theme_scoped(self):
        """`--danger` is fixed across themes and `--success` has to be too - a
        theme that redefines one of them can undo the pairing on its own."""
        for block in re.findall(r"html\.theme-\w+\s*\{([^}]*)\}", self.css):
            for token in (_POSITIVE_TOKEN, _NEGATIVE_TOKEN):
                with self.subTest(token=token):
                    self.assertNotIn(token, block)

    def test_the_theme_blocks_are_still_where_this_looks_for_them(self):
        # Without this, the guard above passes by finding nothing at all.
        self.assertEqual(len(re.findall(r"html\.theme-\w+\s*\{([^}]*)\}", self.css)), 4)

    def test_the_summary_deltas_and_the_badge_share_one_positive_green(self):
        """Both say "this got better". Two hexes for that drift apart."""
        self.assertEqual(self._declaration(".change-positive", "color"),
                         self._declaration(".rank-move-up", "color"))

    def _resolve(self, value):
        """`value` with a single `var(--token)` indirection followed into
        `:root`; returned unchanged if it is already a literal."""
        token = re.fullmatch(r"var\((--[\w-]+)\)", value or "")
        return self._rootToken(token.group(1)) if token else value

    def _rootToken(self, name):
        root = re.search(r":root\s*\{([^}]*)\}", self.css)
        match = re.search(r"(?:^|;)\s*" + re.escape(name) + r"\s*:\s*([^;]+)", root.group(1))
        return match.group(1).strip() if match else None

    def _declaration(self, selector, property_):
        """The value of `property_` in `selector`'s rule, or None if either the
        rule or the declaration is absent."""
        rule = re.search(re.escape(selector) + r"\s*(?:,[^{}]*)?\{([^}]*)\}", self.css)
        if rule is None:
            return None
        match = re.search(r"(?:^|;)\s*" + re.escape(property_) + r"\s*:\s*([^;]+)", rule.group(1))
        return match.group(1).strip() if match else None


if __name__ == "__main__":
    unittest.main()
