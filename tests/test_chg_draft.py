"""Drafting the change-request prose from developer-supplied details.

A change request is a regulated record, so the two properties worth pinning are
that the draft is GROUNDED (built only from what developers actually wrote) and
that a drafting failure leaves the form usable rather than half-filled.

This is the draft DF releases, and CARE releases in the default fileset mode,
have always had — switching CARE_RELEASE_MODE to mono must not change it. CARE
in mono mode drafts differently (tests/test_chg_draft_mono.py).
"""
import json

import pytest

from adk_release_agent import chg_draft as D


class _Response:
    def __init__(self, text):
        self.text = text


@pytest.fixture
def model(monkeypatch):
    """Stub the Gemini call; returns the prompt it was given."""
    seen = {}

    def _set(reply, error=None):
        class _Models:
            def generate_content(self, model, contents, config):
                seen["prompt"] = contents
                seen["config"] = config
                if error is not None:
                    raise error
                return _Response(reply)

        class _Client:
            models = _Models()

        import google.genai as genai
        monkeypatch.setattr(genai, "Client", lambda *a, **k: _Client())
        return seen

    return _set


_ITEMS = [
    {"artifact_name": "payments-api", "artifact_version": "1.4.2",
     "jira_ticket": "ABC-4471", "jira_summary": "Retry settlement callback",
     "change_details": "adds a retry with backoff", "requested_by": "dev@acme.com"},
    {"artifact_name": "risk-fetcher", "artifact_version": "4.0.153",
     "change_details": "timeout bump"},
]

_GOOD = json.dumps({
    "change_summary": "Release of payments-api 1.4.2 and risk-fetcher 4.0.153",
    "change_description": "- payments-api:1.4.2 (ABC-4471): retry with backoff\n- risk-fetcher:4.0.153: timeout bump",
    "change_reason": "Delivers ABC-4471",
    "associated_risk": "Not provided.",
    "consequence": "Not provided.",
    "user_impact": "Not provided.",
})


def test_the_prompt_carries_only_what_developers_supplied(model):
    seen = model(_GOOD)
    D.draft_change_request(_ITEMS)
    prompt = seen["prompt"]
    for expected in ("payments-api:1.4.2", "ABC-4471", "adds a retry with backoff",
                     "risk-fetcher:4.0.153", "timeout bump", "dev@acme.com"):
        assert expected in prompt


def test_the_prompt_forbids_inventing_and_forbids_asserting_safety(model):
    seen = model(_GOOD)
    D.draft_change_request(_ITEMS)
    prompt = seen["prompt"].lower()
    assert "not provided." in prompt              # the required fallback
    assert "never infer" in prompt
    assert "absence of information is not evidence of safety" in prompt


def test_drafting_is_deterministic(model):
    """The same queue must not produce a different change record on a re-press."""
    seen = model(_GOOD)
    D.draft_change_request(_ITEMS)
    assert seen["config"]["temperature"] == 0.0


def test_a_draft_returns_every_field(model):
    model(_GOOD)
    out = D.draft_change_request(_ITEMS)
    assert out["ok"] is True and out["grounded_on"] == 2
    assert set(out["draft"]) == set(D._FIELDS)
    assert out["draft"]["associated_risk"] == "Not provided."


def test_a_dataflow_release_is_described_as_such(model):
    seen = model(_GOOD)
    D.draft_change_request(_ITEMS, kind="df")
    assert "Dataflow release" in seen["prompt"]


def test_nothing_selected_is_refused_without_a_model_call(model):
    seen = model(_GOOD)
    out = D.draft_change_request([])
    assert out["ok"] is False
    assert "prompt" not in seen              # no spend on an empty request


def test_a_model_failure_leaves_the_form_usable(model):
    model(_GOOD, error=RuntimeError("vertex quota"))
    out = D.draft_change_request(_ITEMS)
    assert out["ok"] is False and "manually" in out["error"]


@pytest.mark.parametrize("reply", ["not json at all", "[]", '"a string"', "{}"])
def test_unusable_model_output_never_half_fills_the_form(model, reply):
    model(reply)
    out = D.draft_change_request(_ITEMS)
    assert out["ok"] is False


# --- mono mode leaves this draft alone -----------------------------------------

@pytest.fixture
def mono(monkeypatch):
    from release_agent.config import settings

    monkeypatch.setattr(settings, "care_release_mode", "mono")


def test_a_fileset_care_draft_is_the_models_six_fields_without_the_mono_leads(model):
    """CARE_RELEASE_MODE unset: no "Low risk." / "No user impact is expected."
    and no per-field sources — the fields are exactly what the model wrote."""
    seen = model(_GOOD)
    out = D.draft_change_request(_ITEMS, kind="care")
    assert out == {"ok": True, "draft": json.loads(_GOOD), "grounded_on": 2}
    assert "response_schema" not in seen["config"]
    assert "never state that risk is low/high" in seen["prompt"].lower()


def test_a_dataflow_draft_is_unchanged_in_mono_mode(model, mono):
    seen = model(_GOOD)
    out = D.draft_change_request(_ITEMS, kind="df")
    assert out == {"ok": True, "draft": json.loads(_GOOD), "grounded_on": 2}
    assert "Dataflow release" in seen["prompt"] and "response_schema" not in seen["config"]


def test_only_a_care_release_in_mono_mode_takes_the_mono_draft(model, mono, monkeypatch):
    taken = []
    monkeypatch.setattr(D, "_draft_mono", lambda items: taken.append(items) or {"ok": False, "error": "-"})
    model(_GOOD)
    D.draft_change_request(_ITEMS, kind="df")
    assert taken == []
    D.draft_change_request(_ITEMS, kind="care")
    assert taken == [_ITEMS]


def test_the_endpoint_answers_as_before_outside_mono_mode(model, monkeypatch):
    from fastapi.testclient import TestClient

    from release_agent import app_fastapi as A
    from release_agent.tools import release_queue as RQ

    monkeypatch.setattr(RQ, "current_queue", lambda *a, **k: {"ok": True, "queue": [dict(i) for i in _ITEMS]})
    model(_GOOD)
    for kind in ("care", "df"):
        r = TestClient(A.app).post("/api/release-draft", json={
            "kind": kind, "artifacts": ["payments-api:1.4.2", "risk-fetcher:4.0.153"]}).json()
        assert r == {"ok": True, "draft": json.loads(_GOOD), "grounded_on": 2}, kind
