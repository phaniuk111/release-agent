"""ADK 2.11 wraps a run in a context token: a run generator that is left
half-consumed and finalised later (by the GC, in another context) logs
"Failed to detach context" at ERROR. Seen on GKE on every paused preview."""
from __future__ import annotations

import gc
import logging

from fastapi.testclient import TestClient

from release_agent.app_fastapi import app


def test_a_paused_deploy_preview_leaves_no_run_to_be_finalised_later(caplog):
    client = TestClient(app)
    with caplog.at_level(logging.DEBUG):
        res = client.post("/api/chat", json={"thread_id": "adk211-close",
                                             "message": "deploy abc-client-api-svc:1.1.1230 to uat"})
        assert res.status_code == 200 and "CONFIRM-" in res.text
        gc.collect()
        client.post("/api/chat", json={"thread_id": "adk211-close", "message": "no"})
        gc.collect()
    noisy = [r for r in caplog.records
             if "Failed to detach context" in r.getMessage() or "leftover tasks" in r.getMessage()]
    assert not noisy, [f"{r.name}: {r.getMessage()[:80]}" for r in noisy]
