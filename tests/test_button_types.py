"""Every <button> in the templates declares its type.

A <button> without one is a SUBMIT button. Outside a <form> that is harmless
today, which is exactly how it stays unnoticed until a filter strip or a
toggle is moved into one - at which point every click reloads the page and
nothing in the suite says why (templates/_wrapped_export_button.html records
one such afternoon). Stating the type where the button is written keeps the
question from ever depending on where it later ends up.

Jinja comments are stripped first: a `<button` inside `{# ... #}` is prose.
"""
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"

JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.S)
BUTTON_TAG_RE = re.compile(r"<button\b[^>]*>", re.S)
TYPE_ATTR_RE = re.compile(r"\btype\s*=")


def untypedButtons(templateText: str) -> list[str]:
    stripped = JINJA_COMMENT_RE.sub("", templateText)
    return [tag for tag in BUTTON_TAG_RE.findall(stripped) if not TYPE_ATTR_RE.search(tag)]


class TestButtonTypes(unittest.TestCase):

    def test_every_template_button_declares_its_type(self):
        offenders = {}
        for template in sorted(TEMPLATES_DIR.glob("*.html")):
            found = untypedButtons(template.read_text(encoding="utf-8"))
            if found:
                offenders[template.name] = [" ".join(tag.split())[:80] for tag in found]
        self.assertEqual(offenders, {}, f"<button> without type=: {offenders}")

    def test_the_scan_sees_through_a_jinja_comment_and_a_multiline_tag(self):
        text = """{# a <button> in prose #}
<button
    class="x"
    type="button">ok</button>
<button class="y">missing</button>"""
        self.assertEqual(untypedButtons(text), ['<button class="y">'])


if __name__ == "__main__":
    unittest.main()
