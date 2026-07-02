"""Free-form deploy-intent fallback: pre-filter, classifier glue, payload shape.

The classifier itself is a model call — stubbed here. What's pinned is the
routing contract: only deploy-ish messages trigger a call, only a complete
(chart + version) deploy intent produces a payload, the payload is exactly what
the deterministic parser understands, and every failure path returns None (fall
back to the chat lane).
"""
import json

import pytest

from adk_release_agent import intent
from adk_release_agent.deploy import _request_from_inputs


def test_prefilter_skips_ordinary_chat(monkeypatch):
    def boom(_msg):
        raise AssertionError("classifier must not be called")

    monkeypatch.setattr(intent, "_classify", boom)
    assert intent.deploy_payload_from_freeform("what images can I promote?") is None
    assert intent.deploy_payload_from_freeform("hello") is None
    # env word without any action word: still no call
    assert intent.deploy_payload_from_freeform("uat looks broken") is None


@pytest.mark.parametrize(
    "message",
    [
        "can you get payments-api:1.2.3 to prod",
        "please push targeted-svc 1.4.0 out to production",
        "ship orders-api v2 to uat",
    ],
)
def test_prefilter_lets_deployish_messages_through(monkeypatch, message):
    calls = []
    monkeypatch.setattr(intent, "_classify", lambda m: calls.append(m) or None)
    assert intent.deploy_payload_from_freeform(message) is None  # classifier said no
    assert calls, f"expected a classifier call for {message!r}"


def test_full_intent_produces_parser_compatible_payload(monkeypatch):
    monkeypatch.setattr(
        intent,
        "_classify",
        lambda m: {
            "is_deploy": True,
            "chart": "payments-api",
            "version": "1.2.3",
            "environment": "prod",
            "deployment_repo": "my-org/deploy-repo",
        },
    )
    payload = intent.deploy_payload_from_freeform("can you get payments-api:1.2.3 to prod")
    assert payload is not None
    doc = json.loads(payload)
    assert doc == {
        "environment": "prod",
        "images": {"payments-api": "1.2.3"},
        "deployment_repo": "my-org/deploy-repo",
    }
    # The deterministic deploy parser accepts the payload unchanged.
    req = _request_from_inputs(message=payload)
    assert req["environment"] == "prod"
    assert req["images"] == [{"name": "payments-api", "tag": "1.2.3"}]
    assert req["deployment_repo"] == "my-org/deploy-repo"


def test_incomplete_or_negative_intents_fall_back(monkeypatch):
    cases = [
        None,                                                   # classifier failure shape
        {"is_deploy": False},                                   # not a deploy
        {"is_deploy": True, "chart": "svc", "version": ""},     # no version
        {"is_deploy": True, "chart": "", "version": "1.0"},     # no chart
    ]
    for verdict in cases:
        monkeypatch.setattr(intent, "_classify", lambda m, v=verdict: v)
        assert intent.deploy_payload_from_freeform("ship it to prod") is None


def test_classifier_exception_falls_back(monkeypatch):
    def boom(_msg):
        raise RuntimeError("model down")

    monkeypatch.setattr(intent, "_classify", boom)
    assert intent.deploy_payload_from_freeform("push svc 1.0 to prod") is None


def test_environment_defaults_to_uat(monkeypatch):
    monkeypatch.setattr(
        intent,
        "_classify",
        lambda m: {"is_deploy": True, "chart": "svc", "version": "1.0", "environment": "somewhere"},
    )
    payload = intent.deploy_payload_from_freeform("get svc 1.0 deployed to uat please")
    assert json.loads(payload)["environment"] == "uat"
