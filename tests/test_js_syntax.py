# Catches JS syntax errors (e.g. unbalanced braces) before they reach the
# browser, where they otherwise surface only as a runtime console error.
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC_JS_DIR = Path(__file__).resolve().parent.parent / "static" / "js"
JS_FILES = sorted(STATIC_JS_DIR.glob("*.js"))
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.mark.parametrize("js_file", JS_FILES, ids=lambda p: p.name)
def test_js_file_has_valid_syntax(js_file):
    result = subprocess.run(
        [NODE, "--check", str(js_file)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
