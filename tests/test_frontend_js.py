import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
NODE = shutil.which("node") is not None
JSDOM = (ROOT / "node_modules" / "jsdom").exists()


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_frontend_helpers_in_node():
    r = subprocess.run(["node", "--test", "tests/voice.test.js"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not (NODE and JSDOM), reason="needs node + `npm install` (jsdom)")
def test_language_switch_resets_interaction_in_jsdom():
    """Language switch = new interaction: clean screen, zero API calls, next request fully fresh."""
    r = subprocess.run(["node", "--test", "tests/ui_language_switch.test.js"], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize("f", ["app.js", "voice.js", "strings.js"])
def test_js_syntax(f):
    r = subprocess.run(["node", "--check", f"frontend/{f}"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
