"""The UI JavaScript lives as ES modules under src/release_agent/static/ served via
StaticFiles — not embedded in a Python string. Syntax-check every module so a typo
(like the \\b / \\\\ escaping bugs that previously blanked the whole UI) is caught in
CI instead of in the browser. Skips gracefully if node isn't installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "release_agent" / "static"
# Every module we write (vendored libraries excluded) — found, not listed, so a
# new screen or core module is checked the day it is added.
MODULES = sorted(str(p.relative_to(STATIC)) for p in STATIC.rglob("*.js")
                 if "vendor" not in p.parts)
JS_TESTS = sorted((Path(__file__).resolve().parent / "js").glob("*.test.mjs"))


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


def test_every_expected_layer_exists():
    """The layering a React port relies on: rules, API contract, screens."""
    for name in ("core/queue.js", "core/format.js", "api.js", "forms/queue_form.js"):
        assert name in MODULES, f"{name} is missing"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_core_rules_pass_their_js_tests():
    """static/core/ is pure (no DOM, no fetch), so Node's built-in runner tests
    it directly — the rules both UIs share, checked without a browser."""
    assert JS_TESTS, "no tests/js/*.test.mjs found"
    r = subprocess.run(["node", "--test", *map(str, JS_TESTS)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
