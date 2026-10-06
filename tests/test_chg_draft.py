"""Drafting the change-request prose from developer-supplied details.

A change request is a regulated record, so the two properties worth pinning are
that the draft is GROUNDED (built only from what developers actually wrote) and
that a drafting failure leaves the form usable rather than half-filled.

CARE releases in the default fileset mode keep the draft they have always had
(switching CARE_RELEASE_MODE to mono must not change it). DF uses the CARE
summariser with DF's own wording (below); CARE in mono mode is in
tests/test_chg_draft_mono.py.
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


# --- DF: the CARE summariser, DF's own risk/consequence/impact wording ------------

_DF_GOOD = json.dumps({
    "services": [{"name": "payments-api", "clause": "retries settlement callbacks with backoff"},
                 {"name": "risk-fetcher", "clause": "longer fetch timeout"}],
    "change_summary": "Settlement retry and a longer fetch timeout",
    "change_description": "payments-api retries settlement callbacks with backoff; risk-fetcher gets a longer timeout.",
    "change_reason": "ABC-4471 makes settlement callbacks resilient; the timeout bump reduces fetch failures.",
})


def test_a_dataflow_release_summarises_every_developer_entry_as_data(model):
    seen = model(_DF_GOOD)
    D.draft_change_request(_ITEMS, kind="df")
    prompt = seen["prompt"]
    assert "Dataflow images" in prompt and D.DATA_CLAUSE in prompt
    for fact in ("payments-api", "1.4.2", "ABC-4471", "adds a retry with backoff", "risk-fetcher", "timeout bump"):
        assert fact in prompt
    assert "dev@acme.com" not in prompt, "who queued it is not the model's business"
    assert "response_schema" in seen["config"] and seen["config"]["temperature"] == 0.0


def test_a_dataflow_draft_writes_summary_description_reason_and_keeps_df_wording(model):
    model(_DF_GOOD)
    out = D.draft_change_request(_ITEMS, kind="df")
    assert out["ok"] and list(out["draft"]) == list(D.DF_FIELDS) and out["grounded_on"] == 2
    assert out["draft"]["change_summary"] == "Settlement retry and a longer fetch timeout"
    assert {f: out["sources"][f] for f in ("change_summary", "change_description", "change_reason")} == \
        {"change_summary": "ai", "change_description": "ai", "change_reason": "ai"}
    assert {out["sources"][f] for f in ("associated_risk", "consequence", "user_service_impact")} == {"team"}
    assert not out["draft"]["associated_risk"].startswith("Low risk."), "no mono lead for DF"
    assert "user_impact" not in out["draft"]


@pytest.mark.parametrize("field,text", [
    ("change_reason", "ABC-9999 makes settlement callbacks resilient."),           # a ticket nobody gave
    ("change_summary", "Upgrade to payments-api 2.0.0"),                           # a version nobody gave
    ("change_description", "payments-api cuts latency by 40 percent; risk-fetcher timeout."),  # a figure
])
def test_a_dataflow_draft_that_invents_an_identifier_falls_back(model, field, text):
    reply = json.loads(_DF_GOOD)
    reply[field] = text
    model(json.dumps(reply))
    out = D.draft_change_request(_ITEMS, kind="df")
    assert out["sources"][field] == "fallback" and text not in out["draft"][field]


def test_a_dataflow_description_missing_an_image_is_rebuilt_from_its_clauses(model):
    reply = json.loads(_DF_GOOD)
    reply["change_description"] = "payments-api retries settlement callbacks with backoff."
    model(json.dumps(reply))
    out = D.draft_change_request(_ITEMS, kind="df")
    d = out["draft"]["change_description"]
    assert "payments-api" in d and "risk-fetcher" in d and out["sources"]["change_description"] == "ai"


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


def test_a_dataflow_draft_is_the_same_in_mono_mode(model, mono):
    model(_DF_GOOD)
    assert list(D.draft_change_request(_ITEMS, kind="df")["draft"]) == list(D.DF_FIELDS)


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
    r = TestClient(A.app).post("/api/release-draft", json={
        "kind": "care", "artifacts": ["payments-api:1.4.2", "risk-fetcher:4.0.153"]}).json()
    assert r == {"ok": True, "draft": json.loads(_GOOD), "grounded_on": 2}
