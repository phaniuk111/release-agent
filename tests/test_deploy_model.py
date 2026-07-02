"""Unit checks for the deployment.json model: full-entry parsing, multi-chart,
complete-override planning, and the prd (not 'prod') file-path spelling. No network."""
import json

from release_agent.agent.parsing import _try_parse_json_payload
from release_agent.tools.gh_tools import assemble_entry, plan_deploy, _entries_for_deploy
from release_agent.tools.promotion import _replace_with


def test_parse_preserves_full_multichart_entries():
    payload = json.dumps(
        {
            "environment": "uat",
            "include": [
                {"helm_chart_name": "a", "helm_chart_version": "1", "gke_namespace": "ns-a"},
                {"helm_chart_name": "b", "helm_chart_version": "2", "gke_namespace": "ns-b"},
            ],
        }
    )
    req = _try_parse_json_payload(payload)
    assert len(req["entries"]) == 2
    assert [e["gke_namespace"] for e in req["entries"]] == ["ns-a", "ns-b"]
    assert req["environment"] == "uat"


def test_prd_spelling_not_prod():
    # The file model uses 'prd' everywhere, even though callers may pass 'prod'.
    e = assemble_entry("svc", "1.0", "prod")
    assert e["helm_values_file_name"] == "prd/values_prd.yaml"
    e2 = assemble_entry("svc", "1.0", "prd")
    assert e2["helm_values_file_name"] == "prd/values_prd.yaml"


def test_uat_plan_is_single_file_override():
    ents = _entries_for_deploy(
        "uat", "", json.dumps({"include": [{"helm_chart_name": "x", "helm_chart_version": "1"}]}), "", "", ""
    )
    plan = plan_deploy("uat", ents)
    assert list(plan.keys()) == ["uat/deployment.json"]
    assert plan["uat/deployment.json"][0]["helm_values_file_name"] == "uat/values_uat.yaml"


def test_prod_plan_writes_both_files_with_env_values():
    ents = _entries_for_deploy(
        "prod", "", json.dumps({"include": [{"helm_chart_name": "x", "helm_chart_version": "1"}]}), "", "", ""
    )
    plan = plan_deploy("prod", ents)
    assert sorted(plan) == ["prd/deployment.json", "uat/deployment.json"]
    assert plan["prd/deployment.json"][0]["helm_values_file_name"] == "prd/values_prd.yaml"
    assert plan["uat/deployment.json"][0]["helm_values_file_name"] == "uat/values_uat.yaml"


def test_replace_with_overrides_not_upserts():
    include = [{"helm_chart_name": "old", "helm_chart_version": "1"}]
    changed = _replace_with([{"helm_chart_name": "new", "helm_chart_version": "2"}])(include)
    assert changed is True
    assert [e["helm_chart_name"] for e in include] == ["new"]  # old one is gone


def test_upsert_entry_drops_stale_duplicates():
    # A whole-branch git merge can leave a chart twice in include[]; an upsert must
    # replace the first match and drop the stragglers (self-heal), even when the
    # first match already carries the target version.
    from release_agent.tools.promotion import _upsert_entry

    include = [
        {"helm_chart_name": "svc", "helm_chart_version": "1.1.0"},
        {"helm_chart_name": "svc", "helm_chart_version": "1.0.0"},
        {"helm_chart_name": "other", "helm_chart_version": "2"},
    ]
    changed = _upsert_entry(include, {"helm_chart_name": "svc", "helm_chart_version": "1.1.0"})
    assert changed is True  # dropping the stale duplicate counts as a change
    assert include == [
        {"helm_chart_name": "svc", "helm_chart_version": "1.1.0"},
        {"helm_chart_name": "other", "helm_chart_version": "2"},
    ]


def test_remove_entry_removes_all_duplicates():
    from release_agent.tools.promotion import _remove_entry

    include = [
        {"helm_chart_name": "svc", "helm_chart_version": "1"},
        {"helm_chart_name": "keep", "helm_chart_version": "2"},
        {"helm_chart_name": "svc", "helm_chart_version": "0"},
    ]
    assert _remove_entry(include, "svc") is True
    assert include == [{"helm_chart_name": "keep", "helm_chart_version": "2"}]
    assert _remove_entry(include, "svc") is False


def test_image_tags_path_still_works():
    ents = _entries_for_deploy("uat", "a:1,b:2", "", "", "", "")
    assert [e["helm_chart_name"] for e in ents] == ["a", "b"]


def test_lenient_parse_recovers_loose_objects():
    from release_agent.agent.parsing import _try_parse_json_payload

    # two entries, NO comma between them, NO include[] wrapper (the reported case)
    loose = (
        '{ "helm_chart_name":"svc-a","helm_chart_version":"1.0" }\n'
        '{ "helm_chart_name":"svc-b","helm_chart_version":"2.0" }'
    )
    req = _try_parse_json_payload(loose)
    assert [e["name"] for e in req["images"]] == ["svc-a", "svc-b"]
    assert len(req["entries"]) == 2


def test_lenient_parse_missing_comma_inside_include():
    from release_agent.agent.parsing import _try_parse_json_payload

    loose = '{"include":[{"helm_chart_name":"a","helm_chart_version":"1"}{"helm_chart_name":"b","helm_chart_version":"2"}]}'
    assert [e["name"] for e in _try_parse_json_payload(loose)["images"]] == ["a", "b"]


def test_non_json_returns_none():
    from release_agent.agent.parsing import _try_parse_json_payload

    assert _try_parse_json_payload("just deploy something please") is None
