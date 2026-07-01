from adk_release_agent import deploy, tools


def test_adk_chat_tools_exclude_release_defining_mutations():
    names = {tool.__name__ for tool in tools.ADK_CHAT_TOOLS}

    assert not (tools.RELEASE_DEFINING_MUTATIONS & names)
    assert {"remove_from_release", "retrigger_deployment_workflow", "merge_prod_release"} <= names


def test_adk_tool_result_coercion_preserves_json_objects():
    assert tools._coerce_tool_result('{"ok": true, "value": 1}') == {"ok": True, "value": 1}
    assert tools._coerce_tool_result("[1, 2]") == {"result": [1, 2]}
    assert tools._coerce_tool_result("plain text") == {"result": "plain text"}


def test_adk_agent_module_imports_without_google_adk_installed():
    import adk_release_agent.agent as agent

    assert hasattr(agent, "build_root_agent")
    assert hasattr(agent, "root_agent")


def test_deploy_preview_mints_token_and_exact_uat_plan():
    deploy._PENDING_PREVIEWS.clear()

    preview = deploy.prepare_deploy_preview(
        image_tags="abc-client-api-svc:1.1.1230",
        environment="uat",
        namespace="eod1",
    )

    assert preview["ok"] is True
    assert preview["status"] == "awaiting_confirmation"
    assert preview["environment"] == "uat"
    assert preview["token"].startswith("CONFIRM-")
    assert list(preview["proposed"]) == ["uat/deployment.json"]
    entry = preview["proposed"]["uat/deployment.json"][0]
    assert entry["helm_chart_name"] == "abc-client-api-svc"
    assert entry["helm_chart_version"] == "1.1.1230"
    assert entry["gke_namespace"] == "eod1"
    assert preview["token"] in deploy._PENDING_PREVIEWS


def test_deploy_preview_for_prod_plans_both_files():
    deploy._PENDING_PREVIEWS.clear()

    preview = deploy.prepare_deploy_preview(
        message="deploy abc-client-api-svc:1.1.1230 to prod",
    )

    assert preview["ok"] is True
    assert preview["environment"] == "prod"
    assert {"uat/deployment.json", "prd/deployment.json"} == set(preview["proposed"])


def test_apply_confirmed_deploy_rejects_missing_or_wrong_token(monkeypatch):
    deploy._PENDING_PREVIEWS.clear()
    calls = []
    monkeypatch.setattr(deploy, "_invoke_tool", lambda name, args: calls.append((name, args)))

    result = deploy.apply_confirmed_deploy("CONFIRM-NOPE")

    assert result["ok"] is False
    assert result["status"] == "not_confirmed"
    assert calls == []


def test_apply_confirmed_deploy_calls_release_pr_tool(monkeypatch):
    deploy._PENDING_PREVIEWS.clear()
    preview = deploy.prepare_deploy_preview(
        image_tags="abc-client-api-svc:1.1.1230",
        environment="uat",
    )
    calls = []

    def fake_invoke(name, args):
        calls.append((name, args))
        return {"action": "deployed", "environment": args["environment"]}

    monkeypatch.setattr(deploy, "_invoke_tool", fake_invoke)

    result = deploy.apply_confirmed_deploy(f"yes {preview['token']}")

    assert result["ok"] is True
    assert result["confirmed_token"] == preview["token"]
    assert calls == [
        (
            "open_release_pr",
            {"environment": "uat", "image_tags": "abc-client-api-svc:1.1.1230"},
        )
    ]
    assert preview["token"] not in deploy._PENDING_PREVIEWS
