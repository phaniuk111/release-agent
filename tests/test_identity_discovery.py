"""What the mesh tells us about the caller — discovery only.

ASM authenticates at the edge, but that identity stops at the sidecar unless a
header carries it in, and the app reads no headers today. This block reports
what actually arrives so the header can be identified BEFORE any code depends on
it. Nothing consumes it yet, which is the point: it is safe to deploy.
"""
from release_agent.app_fastapi import _identity_report, _mask_identity


class _Req:
    def __init__(self, headers):
        self.headers = headers


def test_a_known_identity_header_is_found_and_masked():
    r = _identity_report(_Req({
        "X-Goog-Authenticated-User-Email": "accounts.google.com:alice@corp.com",
    }))
    assert r["identity_headers"] == {"x-goog-authenticated-user-email": "a***@corp.com"}


def test_an_unknown_header_name_is_still_surfaced():
    """The case that matters — a corporate mesh may use a name nobody guessed."""
    r = _identity_report(_Req({"X-Corp-Staff-Identity": "bob@corp.com"}))
    assert "x-corp-staff-identity" in r["other_candidate_headers"]


def test_a_token_shaped_value_is_never_echoed():
    """/api/diagnostics gets screenshotted into tickets — a JWT must not ride along."""
    jwt = "eyJ" + "x" * 400
    out = _mask_identity(jwt)
    assert jwt not in out and out == f"<{len(jwt)} chars>"


def test_the_authorization_header_is_reported_as_presence_only():
    r = _identity_report(_Req({"Authorization": "Bearer supersecret"}))
    assert r["authorization_present"] is True
    assert "supersecret" not in str(r)
    assert "authorization" not in r["other_candidate_headers"]


def test_it_says_whether_the_request_came_through_the_gateway():
    """A header is only trustworthy if the request could not have bypassed the
    ingress — so the report distinguishes the two."""
    assert _identity_report(_Req({"x-forwarded-for": "10.0.0.1"}))["via_gateway"] is True
    assert _identity_report(_Req({}))["via_gateway"] is False


def test_every_header_name_is_listed_even_when_nothing_matches():
    """If no candidate is found, the raw name list is what tells us so."""
    r = _identity_report(_Req({"x-weird-thing": "1", "accept": "*/*"}))
    assert r["identity_headers"] == {}
    assert r["all_header_names"] == ["accept", "x-weird-thing"]


def test_plain_addresses_and_short_values_are_both_reduced():
    assert _mask_identity("alice@corp.com") == "a***@corp.com"
    assert _mask_identity("abc") == "***"
    assert _mask_identity("") == ""
