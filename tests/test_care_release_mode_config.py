"""The CARE release type is the deployment's: CARE_RELEASE_MODE in values.yaml.

The form offers no choice, and nothing a request carries — a stale form's
``care_release_mode``, a ``mode`` on the defaults or draft call — can move a
release into the other type.
"""
import datetime as dt
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from release_agent import app_fastapi as A
from release_agent.tools import _common
from release_agent.tools import care_release as CR
from release_agent.tools import chg_defaults as C
from release_agent.tools import git_snapshot as G
from release_agent.tools import release_fileset as RF
from release_agent.tools import release_queue as RQ

MONO = "example-org/mono-repo"
DEPLOY = "example-org/deploy-repo"
ART = "https://artifactory.example.com/docker/example-ds"


@pytest.fixture
def both(monkeypatch):
    """Configured for both repos; CARE_RELEASE_MODE alone decides."""
    for k, v in {"care_release_mode": "fileset", "care_release_repo": MONO, "deploy_repo": DEPLOY,
                 "care_release_name_format": "<TEAM> CARE Release - %Y.%m.%d",
                 "care_previous_tags_file": ""}.items():
        monkeypatch.setattr(C.settings, k, v, raising=False)
    monkeypatch.setattr(_common, "active_deploy_repo", lambda: DEPLOY)
    return C.settings


def _payload(**over):
    base = {"release_name": "R1", "start_date": "2026-10-01 10:00:00",
            "end_date": "2026-10-02 23:00:00", "change_initiator": "someone@example.com", "jira": "ABC-1",
            "change_summary": "R1", "change_description": "d", "change_reason": "r",
            "associated_risk": "Low risk.", "consequence": "c",
            "user_service_impact": "No user impact is expected.", "prl1_only": [], "df_images": [],
            "artefact": [f"{ART}/svc-a:5.0.470"], "release_kind": "care"}
    base.update(over)
    return base


def test_the_setting_decides_the_type(both):
    assert C.care_mono("care") is False
    both.care_release_mode = "mono"
    assert C.care_mono("care") is True
    assert C.care_mono("df") is False, "DF is never mono"


def test_the_queue_context_names_one_type_and_offers_no_choice(both, monkeypatch):
    monkeypatch.setattr(RQ, "current_queue", lambda *a, **k: {"ok": True, "queue": []})
    monkeypatch.setattr(A, "_known_charts", lambda: [])
    r = TestClient(A.app).get("/api/release-queue").json()
    assert r["care_release_mode"] == "fileset" and "care_release_modes" not in r


def test_a_mode_on_the_defaults_call_is_ignored(both, monkeypatch):
    monkeypatch.setattr(RQ, "current_queue", lambda *a, **k: {"ok": True, "queue": []})
    ask = {"artifacts": [f"{ART}/svc-a:5.0.470"], "kind": "care", "date": "2026-10-01", "mode": "mono"}
    r = TestClient(A.app).post("/api/release-defaults", json=ask).json()
    assert r["fields"]["release_name"] == "", "file-set: the release manager names it, whatever was asked"
    both.care_release_mode = "mono"
    r = TestClient(A.app).post("/api/release-defaults", json={**ask, "mode": "fileset"}).json()
    assert r["fields"]["release_name"] == "<TEAM> CARE Release - 2026.10.01"
    assert C.build_defaults([], "care", dt.date(2026, 10, 1))["release_name"] == "<TEAM> CARE Release - 2026.10.01"


def test_a_payload_cannot_take_a_file_set_deployment_into_mono(both, monkeypatch):
    cloned = []
    monkeypatch.setattr(RF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: object()))
    monkeypatch.setattr(RF, "_resolve_github_token", lambda: "")
    monkeypatch.setattr(RF, "_open_prd_pr_blocker", lambda repo, **kw: None)
    monkeypatch.setattr(RF.settings, "sit_branch", "SIT", raising=False)

    def clone(url, branch, dest, token):
        cloned.append(branch)
        raise RuntimeError("clone reached")

    monkeypatch.setattr(G, "clone_branch", clone)
    monkeypatch.setattr(CR, "prepare", lambda *a, **k: pytest.fail("CARE_RELEASE_MODE is fileset"))
    out = RF.prepare_release_fileset(_payload(care_release_mode="mono", deployment_repo=DEPLOY))
    assert out == {"ok": False, "errors": ["clone reached"]} and cloned == ["SIT"]


def test_a_payload_cannot_take_a_mono_deployment_into_the_file_set(both, monkeypatch):
    both.care_release_mode = "mono"
    seen = []
    monkeypatch.setattr(CR, "prepare", lambda payload, details: seen.append(payload) or {"ok": True, "mode": "mono"})
    monkeypatch.setattr(G, "clone_branch", lambda *a, **k: pytest.fail("CARE_RELEASE_MODE is mono"))
    assert RF.prepare_release_fileset(_payload(care_release_mode="fileset"))["mode"] == "mono"
    assert len(seen) == 1
