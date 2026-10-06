"""PROD is reachable ONLY through a release (queue -> CARE/DF release -> promote).

A chart:version deploy whose environment resolves to prod is refused in
``adk_release_agent.deploy.prepare_deploy_preview`` — before any GitHub work and
before a CONFIRM token is minted. The release path (deployment_type="release",
which itself sets environment="prod" internally — see agent/parsing.py) is a
DIFFERENT flow and must keep previewing normally; so must a uat chart deploy.
"""
import json

from adk_release_agent import deploy
from adk_release_agent.deploy_workflow import _preview_text


def _fresh():
    deploy._PENDING_PREVIEWS.clear()


# --- the refusal ---------------------------------------------------------------

def test_prod_chart_deploy_is_refused_and_mints_no_token(monkeypatch):
    _fresh()
    calls = []
    monkeypatch.setattr(deploy, "_invoke_tool", lambda name, args: calls.append((name, args)))

    out = deploy.prepare_deploy_preview(image_tags="payments-api:1.2.3", environment="prod")

    assert out["ok"] is False
    assert "release" in out["error"].lower()
    assert "token" not in out
    assert deploy._PENDING_PREVIEWS == {}
    assert calls == []  # no GitHub work at all


def test_prod_chart_deploy_is_refused_from_json_payload_too(monkeypatch):
    """The same refusal applies when the environment comes from a pasted
    deployment JSON payload, not the environment= argument."""
    _fresh()
    monkeypatch.setattr(deploy, "_invoke_tool", lambda name, args: (_ for _ in ()).throw(
        AssertionError("must not reach open_release_pr")))
    payload = json.dumps({
        "environment": "prod",
        "include": [{"helm_chart_name": "payments-api", "helm_chart_version": "1.2.3"}],
    })

    out = deploy.prepare_deploy_preview(deployment_json=payload)

    assert out["ok"] is False
    assert "release" in out["error"].lower()
    assert deploy._PENDING_PREVIEWS == {}


# --- what must keep working -----------------------------------------------------

def test_uat_chart_deploy_still_previews_and_applies(monkeypatch):
    _fresh()
    calls = []
    monkeypatch.setattr(deploy, "_invoke_tool", lambda name, args: calls.append((name, args)) or
                        {"ok": True, "note": "deployed"})

    preview = deploy.prepare_deploy_preview(image_tags="payments-api:1.2.3", environment="uat")
    assert preview["ok"] is True
    assert preview["token"].startswith("CONFIRM-")
    assert preview["environment"] == "uat"

    out = deploy.apply_confirmed_deploy(preview["token"])
    assert out.get("ok") is not False
    assert calls == [("open_release_pr", {"environment": "uat", "image_tags": "payments-api:1.2.3"})]


def test_release_payload_environment_prod_still_previews(monkeypatch):
    """A live release payload sets environment="prod" internally (it always has
    — that is what "release ships to prod" means), and is a completely different
    flow from a chart:version deploy: it must keep previewing."""
    from release_agent.tools import release_fileset as RF

    _fresh()
    monkeypatch.setattr(
        RF, "prepare_release_fileset",
        lambda payload: {
            "ok": True, "release_name": "REL-1", "kind": "care", "landing_branch": "SIT",
            "preview": {"changed_files": ["release_details.json"]},
        },
    )
    payload = json.dumps({
        "release_name": "REL-1",
        "artefact": ["https://example/payments-api:1.2.3"],
    })

    out = deploy.prepare_deploy_preview(deployment_json=payload)

    assert out["ok"] is True
    assert out["environment"] == "prod"
    assert out["token"].startswith("CONFIRM-")
    assert "REL-1" in out["heading"]


# --- preview rendering (pure) ----------------------------------------------------

def test_preview_text_includes_change_request_for_prod():
    text = _preview_text(
        {"prd/deployment.json": []},
        "CONFIRM-X",
        "prod",
        "svc:1",
        {"chg_summary": "S", "start_date": "a", "end_date": "b", "description": "D"},
    )
    assert "Change request" in text and "S" in text and "a → b" in text


def test_preview_text_omits_change_request_when_none():
    text = _preview_text({"uat/deployment.json": []}, "CONFIRM-Y", "uat", "svc:1", None)
    assert "Change request" not in text
