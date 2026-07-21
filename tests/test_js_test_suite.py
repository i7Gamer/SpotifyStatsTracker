# Runs the plain-node unit test scripts (tests/test_*.js) so they're part of
# `pytest`/CI instead of only firing when someone manually runs `node <file>`.
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
JS_TEST_FILES = sorted(TESTS_DIR.glob("test_*.js"))
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.mark.parametrize("js_test_file", JS_TEST_FILES, ids=lambda p: p.name)
def test_js_test_script_passes(js_test_file):
    result = subprocess.run(
        [NODE, str(js_test_file)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
