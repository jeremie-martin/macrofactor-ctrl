"""Runs the web UI's logic tests (tests/web_core.test.js) when node is available."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_web_core_logic():
    test_file = Path(__file__).with_name("web_core.test.js")
    result = subprocess.run(["node", "--test", str(test_file)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
