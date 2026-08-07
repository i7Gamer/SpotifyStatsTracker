"""static/manifest.json's icons, checked against the files they point at.

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
import struct
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST_PATH = _ROOT / "static" / "manifest.json"

#< the manifest's src paths are relative to itself (it lives in static/)
_MANIFEST_DIR = _MANIFEST_PATH.parent

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#< width and height are the first two fields of the IHDR chunk's data, which
#  starts at byte 16 of every PNG - close enough to a header read that the
#  suite doesn't need an image library for it
_PNG_DIMENSIONS_OFFSET = 16
_PNG_DIMENSIONS_FORMAT = ">II"


def _pngDimensions(path):
    """(width, height) of a PNG, read from its IHDR chunk."""
    data = path.read_bytes()
    assert data[:len(_PNG_SIGNATURE)] == _PNG_SIGNATURE, f"{path} is not a PNG"
    return struct.unpack(_PNG_DIMENSIONS_FORMAT,
                         data[_PNG_DIMENSIONS_OFFSET:_PNG_DIMENSIONS_OFFSET + 8])


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


if __name__ == "__main__":
    unittest.main()
