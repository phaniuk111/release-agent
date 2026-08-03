"""requirements.txt must stay in sync with pyproject/uv.lock.

The Docker image installs from uv.lock, but downstream/enterprise builds install
from requirements.txt — so a dependency added to pyproject that never gets
re-exported ships a working image and a BROKEN downstream build. That drift has
already happened once in this repo (google-cloud-bigquery + ruamel.yaml were
hand-appended while uv.lock went stale), hence this guard.

Regenerate with:
    uv lock && uv export --no-dev --no-hashes --no-emit-project -o requirements.txt
"""
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"


def _package_lines(text: str) -> list[str]:
    """Pinned lines only — drops the generated header/comment lines so the
    exact `uv export` invocation recorded at the top doesn't matter."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_requirements_txt_matches_uv_lock():
    result = subprocess.run(
        ["uv", "export", "--no-dev", "--no-hashes", "--no-emit-project"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"uv export failed:\n{result.stderr[-500:]}"

    exported = _package_lines(result.stdout)
    committed = _package_lines(REQUIREMENTS.read_text())
    if exported != committed:
        only_lock = sorted(set(exported) - set(committed))
        only_req = sorted(set(committed) - set(exported))
        pytest.fail(
            "requirements.txt is out of sync with uv.lock — downstream builds "
            "installing from it would differ from the image.\n"
            f"  in uv.lock but NOT requirements.txt: {only_lock or '-'}\n"
            f"  in requirements.txt but NOT uv.lock: {only_req or '-'}\n"
            "  fix: uv lock && uv export --no-dev --no-hashes "
            "--no-emit-project -o requirements.txt"
        )


def test_direct_dependencies_are_pinned_in_requirements():
    """Every runtime dependency the app imports must appear in requirements.txt
    (guards a hand-edited export that silently drops one)."""
    committed = _package_lines(REQUIREMENTS.read_text())
    names = {line.split("==")[0].strip().lower().replace("_", "-") for line in committed}
    required = {
        "google-adk",
        "google-genai",
        "google-cloud-bigquery",  # release intake queue / analytics
        "ruamel-yaml",  # the deploy repo's updater script runs in this venv
        "fastapi",
        "uvicorn",
        "pygithub",
        "pydantic",
        "pydantic-settings",
    }
    assert required <= names, f"missing from requirements.txt: {sorted(required - names)}"


def test_requests_defaults_to_system_ca_bundle(monkeypatch, tmp_path):
    """Behind a TLS-inspecting proxy the corporate CA lands in the system trust
    store, which requests ignores in favour of certifi — so GitHub calls fail
    while curl/git/urllib succeed in the SAME pod. The store path is asked of
    OpenSSL, not guessed: it differs per distro (Debian
    /etc/ssl/certs/ca-certificates.crt, RHEL /etc/pki/..., Alpine and some
    corporate images /etc/ssl/cert.pem)."""
    import os
    import ssl
    from types import SimpleNamespace

    from release_agent import config

    bundle = tmp_path / "cert.pem"          # an Alpine-style path, not the Debian one
    bundle.write_text("x")

    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "sentinel")   # so teardown reverts
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    monkeypatch.setattr(
        ssl, "get_default_verify_paths",
        lambda: SimpleNamespace(cafile=str(bundle), capath=None),
    )

    # 1) whatever OpenSSL reports is what requests gets pointed at
    os.environ.pop("REQUESTS_CA_BUNDLE")
    config._use_system_ca_for_requests()
    assert os.environ["REQUESTS_CA_BUNDLE"] == str(bundle)

    # 2) an explicit operator setting is never overridden
    os.environ["REQUESTS_CA_BUNDLE"] = "/explicit/ca.pem"
    config._use_system_ca_for_requests()
    assert os.environ["REQUESTS_CA_BUNDLE"] == "/explicit/ca.pem"

    # 3) OpenSSL reports nothing (or the file is gone) -> leave it alone;
    #    verification stays on via certifi
    os.environ.pop("REQUESTS_CA_BUNDLE")
    monkeypatch.setattr(ssl, "get_default_verify_paths",
                        lambda: SimpleNamespace(cafile=None, capath=None))
    config._use_system_ca_for_requests()
    assert "REQUESTS_CA_BUNDLE" not in os.environ
