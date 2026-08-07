"""The app's install icons, checked against the files they point at.

Mostly static/manifest.json, plus the one icon that cannot live there: iOS does
not read a manifest's icons for Add to Home Screen and takes
`<link rel="apple-touch-icon">` instead. Same job, same assets directory, so it
is checked here rather than in a file of its own.

A manifest is the one place in the app where an asset's dimensions are written
down twice - once as a `sizes` string, once as the PNG's own header - and
nothing but a browser ever compared them. Chrome does, on every page load, and
complains in the console when they disagree:

    Error while trying to use the following icon from the Manifest:
    .../static/images/favicon.png (Resource size is not correct - typo in the
    Manifest?)

which is what `"sizes": "any"` on a 64x64 raster icon earned. `any` means
"scalable, so it satisfies every request" - true of an SVG, a lie about a PNG.
Chrome took it at its word, picked the file for a request it could not satisfy,
downloaded it, measured it, and rejected it. Declaring what the file actually
is takes it out of the running for sizes it cannot serve, and the error with it.

These are file-level assertions on purpose: the manifest is served straight off
disk by the static route, so there is no app behaviour in between to render.
"""
import json
import re
import struct
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST_PATH = _ROOT / "static" / "manifest.json"
_TEMPLATES = _ROOT / "templates"

#< both, like the <link rel="icon"> beside it: Add to Home Screen reads the page
#  it is invoked from, and that can be the login page as easily as the dashboard
_LAYOUTS = ("layout.html", "layout_public.html")

#< iOS renders the home-screen icon at 180x180 on every current device
_APPLE_TOUCH_ICON_PX = 180

#< the manifest's src paths are relative to itself (it lives in static/)
_MANIFEST_DIR = _MANIFEST_PATH.parent

#< Chrome's installability floor. A manifest whose largest icon is under this
#  never produces an install prompt, whatever else it declares - which is what
#  a lone 64x64 favicon left the app with once `any` stopped overstating it.
_INSTALLABLE_ICON_PX = 192

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#< width and height are the first two fields of the IHDR chunk's data, which
#  starts at byte 16 of every PNG - close enough to a header read that the
#  suite doesn't need an image library for it
_PNG_DIMENSIONS_OFFSET = 16
_PNG_DIMENSIONS_FORMAT = ">II"
#< IHDR again: bit depth is byte 24, colour type 25. Bit 2 of the colour type is
#  the alpha channel, so types 4 and 6 carry one; palette images (3) express
#  transparency through a tRNS chunk instead.
_PNG_COLOUR_TYPE_OFFSET = 25
_PNG_ALPHA_COLOUR_TYPES = (4, 6)


def _pngDimensions(path):
    """(width, height) of a PNG, read from its IHDR chunk."""
    data = path.read_bytes()
    assert data[:len(_PNG_SIGNATURE)] == _PNG_SIGNATURE, f"{path} is not a PNG"
    return struct.unpack(_PNG_DIMENSIONS_FORMAT,
                         data[_PNG_DIMENSIONS_OFFSET:_PNG_DIMENSIONS_OFFSET + 8])


def _pngHasTransparency(path):
    """Whether a PNG can carry any pixel that is not fully opaque."""
    data = path.read_bytes()
    return (data[_PNG_COLOUR_TYPE_OFFSET] in _PNG_ALPHA_COLOUR_TYPES
            or b"tRNS" in data)


class TestManifestIcons(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
        self.icons = self.manifest.get("icons", [])

    def test_the_manifest_declares_at_least_one_icon(self):
        """Without one the <link rel="manifest"> is pointless, and the rest of
        this file would pass by having nothing to check."""
        self.assertTrue(self.icons)

    def test_every_icon_file_exists(self):
        for icon in self.icons:
            with self.subTest(src=icon["src"]):
                self.assertTrue((_MANIFEST_DIR / icon["src"]).is_file())

    def test_a_raster_icon_never_claims_to_be_scalable(self):
        """`sizes: "any"` is for formats that genuinely scale. On a PNG it makes
        the icon a candidate for every size Chrome asks for, including ones it
        is far too small to fill - the console error this file is named for."""
        for icon in self.icons:
            with self.subTest(src=icon["src"]):
                if icon.get("type") != "image/svg+xml":
                    self.assertNotEqual(icon.get("sizes"), "any")

    def test_every_declared_size_matches_the_file_on_disk(self):
        """The drift Chrome reports. Declared WxH, actual WxH, same numbers."""
        for icon in self.icons:
            src = icon["src"]
            if icon.get("type") == "image/svg+xml":
                continue
            with self.subTest(src=src):
                width, height = _pngDimensions(_MANIFEST_DIR / src)
                declared = sorted(icon["sizes"].split())
                self.assertIn(f"{width}x{height}", declared,
                              f"{src} is {width}x{height} but the manifest says "
                              f"{icon['sizes']}")

    def test_an_icon_is_big_enough_to_install_the_app_with(self):
        """Declaring the truth about a 64x64 icon silenced Chrome's console
        error and, by the same stroke, left nothing that meets the install
        criteria - `any` had been overstating the only icon there was. This is
        the other half of that fix, and it is a floor rather than an exact size
        so a future icon set can add to the list without editing this."""
        largest = max(
            (min(_pngDimensions(_MANIFEST_DIR / icon["src"]))
             for icon in self.icons if icon.get("type") != "image/svg+xml"),
            default=0)

        self.assertGreaterEqual(largest, _INSTALLABLE_ICON_PX)

    def test_the_tab_favicon_is_among_them(self):
        """layout.html's <link rel="icon"> and the manifest name the same file
        for the small size. A manifest that dropped it would leave the browser
        tab pulling an icon nothing in here describes."""
        self.assertIn("images/favicon.png", [icon["src"] for icon in self.icons])


class TestAppleTouchIcon(unittest.TestCase):
    """The manifest's icons do not reach iOS. Safari's Add to Home Screen reads
    `<link rel="apple-touch-icon">` off the page it is invoked from and nothing
    else, so without one it screenshots the page or scales the 64x64 favicon."""

    def _declaredFilenames(self, layout):
        markup = (_TEMPLATES / layout).read_text(encoding="utf-8")
        return re.findall(
            r"""<link[^>]*rel=["']apple-touch-icon["'][^>]*filename=['"]([^'"]+)['"]""",
            markup)

    def test_both_layouts_declare_one(self):
        for layout in _LAYOUTS:
            with self.subTest(layout=layout):
                self.assertEqual(len(self._declaredFilenames(layout)), 1)

    def test_both_layouts_name_the_same_file_and_it_exists(self):
        declared = {name for layout in _LAYOUTS for name in self._declaredFilenames(layout)}

        self.assertEqual(len(declared), 1, f"the layouts disagree: {declared}")
        self.assertTrue((_ROOT / "static" / declared.pop()).is_file())

    def test_it_is_the_size_ios_renders(self):
        path = _ROOT / "static" / self._declaredFilenames(_LAYOUTS[0])[0]

        self.assertEqual(_pngDimensions(path), (_APPLE_TOUCH_ICON_PX, _APPLE_TOUCH_ICON_PX))

    def test_it_is_opaque(self):
        """iOS composites an apple-touch-icon onto black and offers no say in
        it, so the flattening has to be ours. The artwork is a circular record
        on a transparent square - left with its alpha, the corners become black
        against a black record and the icon loses its edge."""
        path = _ROOT / "static" / self._declaredFilenames(_LAYOUTS[0])[0]

        self.assertFalse(_pngHasTransparency(path))


if __name__ == "__main__":
    unittest.main()
