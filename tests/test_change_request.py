"""Tests for the PROD change-request feature and the prod-updates-both-files guarantee."""
import json

from adk_release_agent import deploy
from adk_release_agent.deploy_workflow import _preview_text
from release_agent.agent.parsing import _try_parse_json_payload
from release_agent.tools.promotion import _deployment_path, change_request_doc, plan_deploy


# --- requirement 1: a prod deploy plans BOTH deployment.json files ---------------

def test_prod_deploy_plans_both_deployment_files():
    entries = [{"helm_chart_name": "svc", "helm_chart_version": "1.0.0"}]
    plan = plan_deploy("prod", entries)
    assert set(plan) == {_deployment_path("uat"), _deployment_path("prd")}
    # UAT copy repoints the values file to the uat one; prd keeps the prod entries.
    assert plan[_deployment_path("prd")] == entries


def test_uat_deploy_plans_only_uat_file():
    plan = plan_deploy("uat", [{"helm_chart_name": "svc", "helm_chart_version": "1.0.0"}])
    assert set(plan) == {_deployment_path("uat")}


# --- change_request_doc mapping (pure) ------------------------------------------

def test_change_request_doc_maps_semantic_and_chg_keys():
    d = change_request_doc(
        {"summary": "S", "change_description": "D", "start_time": "a", "end_time": "b"}, "NOW"
    )
    assert d == {
        "chg_summary": "S",
        "description": "D",
        "start_date": "a",
        "end_date": "b",
        "updated_by": "release-copilot",
        "updated_at": "NOW",
    }
    # Canonical CHG keys pass through unchanged.
    d2 = change_request_doc({"chg_summary": "X", "start_date": "s", "end_date": "e"}, "NOW")
    assert d2["chg_summary"] == "X" and d2["start_date"] == "s" and d2["description"] == ""
    # JSON string is accepted.
    assert change_request_doc('{"summary": "J"}', "NOW")["chg_summary"] == "J"


def test_change_request_doc_returns_none_when_empty():
    assert change_request_doc(None, "NOW") is None
    assert change_request_doc({}, "NOW") is None
    assert change_request_doc("not json", "NOW") is None
    assert change_request_doc({"unrelated": "x"}, "NOW") is None


# --- flow: change_request carried through parse -> preview -> apply args ----------

def test_json_payload_carries_change_request():
    req = _try_parse_json_payload(
        json.dumps(
            {
                "environment": "prod",
                "include": [{"helm_chart_name": "a", "helm_chart_version": "1"}],
                "change_request": {"chg_summary": "S"},
            }
        )
    )
    assert req["change_request"]["chg_summary"] == "S"
    # No change_request key -> None (uat/CLI paths unaffected).
    req2 = _try_parse_json_payload(
        json.dumps({"include": [{"helm_chart_name": "a", "helm_chart_version": "1"}]})
    )
    assert req2["change_request"] is None


def test_prod_change_request_flows_into_open_release_pr(monkeypatch):
    deploy._PENDING_PREVIEWS.clear()
    payload = json.dumps(
        {
            "environment": "prod",
            "include": [
                {"helm_chart_name": "svc-a", "helm_chart_version": "1.0.0", "gke_namespace": "ns"}
            ],
            "change_request": {
                "chg_summary": "Quarterly release",
                "description": "Deploy svc-a",
                "start_date": "2026-07-03T09:00:00Z",
                "end_date": "2026-07-03T11:00:00Z",
            },
        }
    )
    preview = deploy.prepare_deploy_preview(deployment_json=payload)
    assert preview["ok"] is True and preview["environment"] == "prod"
    assert preview["change_request"]["chg_summary"] == "Quarterly release"

    calls = []

    def fake_invoke(name, args):
        calls.append((name, args))
        return {"ok": True, "note": "staged"}

    monkeypatch.setattr(deploy, "_invoke_tool", fake_invoke)
    deploy.apply_confirmed_deploy(preview["token"])

    assert len(calls) == 1
    name, args = calls[0]
    assert name == "open_release_pr" and args["environment"] == "prod"
    assert args["change_request"]["chg_summary"] == "Quarterly release"
    assert args["change_request"]["end_date"] == "2026-07-03T11:00:00Z"


def test_uat_deploy_carries_no_change_request(monkeypatch):
    deploy._PENDING_PREVIEWS.clear()
    preview = deploy.prepare_deploy_preview(image_tags="svc-a:1.0.0", environment="uat")
    calls = []
    monkeypatch.setattr(deploy, "_invoke_tool", lambda n, a: calls.append((n, a)) or {"ok": True})
    deploy.apply_confirmed_deploy(preview["token"])
    _, args = calls[0]
    assert "change_request" not in args


# --- preview rendering ----------------------------------------------------------

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
