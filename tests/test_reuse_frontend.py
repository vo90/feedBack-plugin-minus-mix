"""Execute the screen's state and event behavior through a minimal DOM harness."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is required for frontend checks")
def test_reuse_screen_behaviors():
    result = subprocess.run([shutil.which("node"), "tests/reuse-ui.test.cjs"],
                            cwd=Path(__file__).resolve().parents[1], text=True,
                            encoding="utf-8", capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
