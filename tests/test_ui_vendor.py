"""The page loads nothing from a public CDN: styling, icons and fonts are served
by the portal from static/vendor. A corporate proxy that blocks a CDN host
used to leave the page unstyled and iconless."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from release_agent import app_fastapi as A

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "release_agent" / "static"
VENDOR = STATIC / "vendor"


def _page() -> str:
    return TestClient(A.app).get("/").text


def _attr_values(html: str, attr: str) -> list[str]:
    out, key, i = [], f'{attr}="', 0
    while (i := html.find(key, i)) >= 0:
        i += len(key)
        out.append(html[i:html.find('"', i)])
    return out


def test_the_page_references_no_outside_host():
    html = _page()
    external = [v for a in ("src", "href") for v in _attr_values(html, a)
                if v.startswith(("http://", "https://", "//"))]
    assert external == [], f"the page must not load from outside hosts: {external}"
    assert "url('http" not in html and 'url("http' not in html


def test_every_vendored_file_the_page_uses_exists():
    html = _page()
    refs = [v.split("?")[0] for a in ("src", "href") for v in _attr_values(html, a)
            if v.startswith("static/vendor/")]
    refs.append("static/vendor/inter/inter-latin-wght-normal.woff2")          # @font-face
    assert len(refs) >= 5
    for ref in refs:
        assert (STATIC.parent / ref).is_file(), f"{ref} is missing"


def test_font_awesome_finds_its_woff2_files():
    """The icon CSS points at ../webfonts/*.woff2 (a .ttf fallback follows it,
    which browsers only fetch if woff2 is unsupported — so it is not vendored)."""
    for css in ("solid.min.css", "brands.min.css"):
        text = (VENDOR / "fontawesome" / "css" / css).read_text()
        woff2 = [u.split(")")[0] for u in text.split("url(")[1:] if ".woff2" in u.split(")")[0]]
        assert woff2, css
        for url in woff2:
            assert (VENDOR / "fontawesome" / "css" / url).resolve().is_file(), f"{css}: {url}"


def _tailwind_cli() -> str | None:
    cli = os.environ.get("TAILWIND_CLI") or shutil.which("tailwindcss")
    if not cli:
        return None
    version = subprocess.run([cli, "--help"], capture_output=True, text=True).stdout
    return cli if "v3.4.17" in version else None


@pytest.mark.skipif(_tailwind_cli() is None, reason="Tailwind v3.4.17 CLI not available")
def test_the_built_css_is_up_to_date(tmp_path):
    """A class used for the first time has no effect until the CSS is rebuilt —
    wherever the CLI is available, prove the committed file is current."""
    out = tmp_path / "tailwind.css"
    subprocess.run([_tailwind_cli(), "-c", "scripts/css/tailwind.config.js", "-i", "scripts/css/input.css",
                    "-o", str(out), "--minify"], cwd=ROOT, check=True, capture_output=True)
    assert out.read_text() == (VENDOR / "tailwind.css").read_text(), \
        "static/vendor/tailwind.css is stale — run scripts/css/build.sh and commit the result"
