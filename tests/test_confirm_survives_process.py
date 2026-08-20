"""A confirmation must be appliable by a process that never served the preview.

This is the cross-replica case, and the restart case, which are the same bug:
the preview lived in a module-level dict, so it existed only in the Python
process that produced it. Simulated here by clearing the in-process dicts
between preview and confirm — what a second replica sees is an EMPTY dict, not
a missing one, so clearing is a faithful stand-in.
"""

from adk_release_agent import deploy as D


def _fresh():
    """What a pod that never served the preview starts with."""
    D._PENDING_PREVIEWS.clear()


def test_apply_works_from_the_session_payload_alone(monkeypatch):
    called = {}
    monkeypatch.setattr(D, "_invoke_tool", lambda name, args=None: called.setdefault("args", args) or {"ok": True})

    prep = D.prepare_deploy_preview(image_tags="payments-api:1.4.2", environment="uat")
    assert prep["ok"]
    token, pending = prep["token"], prep["pending"]

    _fresh()                                  # <- the "other pod"
    out = D.apply_confirmed_deploy(token, pending=pending)
    assert out.get("ok") is not False, out
    assert called["args"]["image_tags"] == "payments-api:1.4.2"


def test_without_the_payload_the_other_pod_still_refuses(monkeypatch):
    """The gate must stay closed: no payload and no local record means the token
    cannot be honoured — a wrong apply is far worse than a failed one."""
    monkeypatch.setattr(D, "_invoke_tool", lambda name, args=None: {"ok": True})
    prep = D.prepare_deploy_preview(image_tags="payments-api:1.4.2", environment="uat")
    _fresh()
    out = D.apply_confirmed_deploy(prep["token"])          # no pending= handed in
    assert out["ok"] is False
    assert out["status"] == "not_confirmed"


def test_a_wrong_token_is_refused_even_with_a_payload(monkeypatch):
    """The payload is recovered by thread, but the TOKEN still has to match —
    session state must not become a way around the gate."""
    monkeypatch.setattr(D, "_invoke_tool", lambda name, args=None: {"ok": True})
    prep = D.prepare_deploy_preview(image_tags="payments-api:1.4.2", environment="uat")
    _fresh()
    out = D.apply_confirmed_deploy("CONFIRM-NOPE01", pending=prep["pending"])
    assert out["ok"] is False


def test_prepare_returns_the_payload_the_workflow_persists():
    prep = D.prepare_deploy_preview(image_tags="a:1", environment="uat")
    assert set(prep["pending"]) == {"token", "request", "preview", "created_at"}
    assert prep["pending"]["token"] == prep["token"]      # the gate's anchor
    # and it is plain data — it has to survive a JSON round trip through the
    # session service
    import json
    assert json.loads(json.dumps(prep["pending"]))["request"]["images"][0]["name"] == "a"


def test_the_workflow_binds_the_payload_from_state(monkeypatch):
    """ADK binds a node parameter named after a state key (parameter_binding=
    'state'), so the apply node receives what the gate persisted."""
    from adk_release_agent import deploy_workflow as W

    seen = {}
    monkeypatch.setattr(
        D, "apply_confirmed_deploy",
        lambda token, pending=None: seen.update(token=token, pending=pending) or {"ok": True},
    )
    W._apply_deploy({"token": "CONFIRM-ABC123"}, deploy_pending={"request": {"x": 1}})
    assert seen["token"] == "CONFIRM-ABC123"
    assert seen["pending"] == {"request": {"x": 1}}


def test_a_rejected_release_cleans_up_from_the_session_copy(monkeypatch):
    """On a replica the prepared workdir is only known through session state, so
    cancel has to clean up from there or the clone is orphaned."""
    from release_agent.tools import release_fileset as RF
    from adk_release_agent import deploy_workflow as W

    cleaned = []
    monkeypatch.setattr(RF, "cleanup_prepared_release", lambda prep: cleaned.append(prep))
    W._cancel_deploy({"token": "CONFIRM-X"},
                     deploy_pending={"request": {"release_prep": {"workdir": "/tmp/x"}}})
    assert cleaned == [{"workdir": "/tmp/x"}]


# ------------------------------------------------ Change C: release file-set

def test_prepare_returns_data_not_a_directory(monkeypatch):
    """The prepared release used to be a directory on one machine, which tied
    the confirmation to that process. It must now be plain data."""
    from release_agent.tools import release_fileset as RF

    captured = {}

    def _fake_generate(payload):
        captured["called"] = True
        return {"ok": True, "release_name": "R", "branch": "release/r",
                "details": payload, "fileset_hash": "abc", "deployment_repo": "o/r",
                "artifacts": [], "preview": {"changed_files": ["a.json"]}}

    monkeypatch.setattr(RF, "prepare_release_fileset", _fake_generate)
    prep = RF.prepare_release_fileset({"release_name": "R"})
    assert "workdir" not in prep
    assert prep["details"] and prep["fileset_hash"]


def test_apply_refuses_when_the_regenerated_set_differs(monkeypatch):
    """The whole point of the fingerprint: what gets pushed is what was
    approved, or nothing is."""
    from release_agent.tools import release_fileset as RF

    monkeypatch.setattr(
        RF, "prepare_release_fileset",
        lambda details, _keep_workdir=False: {"ok": True, "fileset_hash": "DIFFERENT",
                         "preview": {"changed_files": ["b.json"]},
                         "workdir": "/tmp/nope", "branch": "release/r"},
    )
    out = RF.apply_release_fileset({
        "details": {"release_name": "R"}, "fileset_hash": "APPROVED",
        "preview": {"changed_files": ["a.json"]},
    })
    assert out["ok"] is False
    assert "no longer matches what you approved" in out["error"]
    # and it names what moved, so the operator can see why
    assert "a.json" in out["error"] or "b.json" in out["error"]


def test_apply_without_the_inputs_refuses_rather_than_guessing():
    from release_agent.tools import release_fileset as RF

    out = RF.apply_release_fileset({"fileset_hash": "x"})
    assert out["ok"] is False and "incomplete" in out["error"]
