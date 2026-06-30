"""The UI JavaScript now lives in a real file (src/release_agent/static/app.js) served
via StaticFiles — not embedded in a Python string. Syntax-check it so an inline-script
typo (like the \\b / \\\\ escaping bugs that previously blanked the whole UI) is caught
in CI instead of in the browser. Skips gracefully if node isn't installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "src" / "release_agent" / "static" / "app.js"


def test_app_js_present():
    assert APP_JS.is_file(), f"UI script missing: {APP_JS}"
    assert APP_JS.stat().st_size > 1000, "app.js looks truncated"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_syntax_valid():
    r = subprocess.run(["node", "--check", str(APP_JS)], capture_output=True, text=True)
    assert r.returncode == 0, f"static/app.js has a JS syntax error:\n{r.stderr}"
