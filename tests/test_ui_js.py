"""The UI JavaScript lives as ES modules under src/release_agent/static/ served via
StaticFiles — not embedded in a Python string. Syntax-check every module so a typo
(like the \\b / \\\\ escaping bugs that previously blanked the whole UI) is caught in
CI instead of in the browser. Skips gracefully if node isn't installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "release_agent" / "static"
MODULES = ["state.js", "chat.js", "connect.js", "forms.js", "palette.js",
           "insights.js", "status.js", "main.js"]


def test_ui_modules_present():
    for name in MODULES:
        f = STATIC / name
        assert f.is_file(), f"UI module missing: {f}"
        assert f.stat().st_size > 200, f"{name} looks truncated"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("name", MODULES)
def test_ui_module_syntax_valid(name):
    src = (STATIC / name).read_bytes()
    r = subprocess.run(
        ["node", "--input-type=module", "--check"],
        input=src, capture_output=True,
    )
    assert r.returncode == 0, f"static/{name} has a JS syntax error:\n{r.stderr.decode()}"
