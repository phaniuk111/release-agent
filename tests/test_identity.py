"""The signed-in user comes from the mesh's SIGNED token — verified, never just read.

Real RSA keys and a real JWKS endpoint (a local HTTP server standing in for
authservice), so signature, issuer, expiry and audience checks are PyJWT's own.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from release_agent import identity
from release_agent import app_fastapi as APP
from release_agent.session_creds import SessionCredentials, SessionCredentialStore

ISSUER = "authservice.asm-user-auth.svc.cluster.local"
AUD = "dev-portal"


def _keypair(kid):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return key, {**jwk, "kid": kid, "alg": "RS256", "use": "sig"}


SIGNER, SIGNER_JWK = _keypair("k1")
STRANGER, _ = _keypair("k1")           # same kid, different key: a forgery


@pytest.fixture(scope="module")
def jwks_url():
    body = json.dumps({"keys": [SIGNER_JWK]}).encode()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/_gcp_user_auth/jwks"
    srv.shutdown()


@pytest.fixture(autouse=True)
def configured(monkeypatch, jwks_url):
    s = identity.settings
    monkeypatch.setattr(s, "identity_header", "x-asm-rctoken", raising=False)
    monkeypatch.setattr(s, "identity_jwks_url", jwks_url, raising=False)
    monkeypatch.setattr(s, "identity_issuer", ISSUER, raising=False)
    monkeypatch.setattr(s, "identity_audience", AUD, raising=False)
    monkeypatch.setattr(s, "identity_email_claim", "attributes.email,email", raising=False)
    monkeypatch.setattr(s, "identity_required", False, raising=False)
    monkeypatch.setitem(identity._keys, "at", 0.0)
    monkeypatch.setitem(identity._keys, "by_kid", {})


def rctoken(key=SIGNER, kid="k1", **over):
    """An RCToken shaped like the cluster's: identity nested under 'attributes'."""
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUD, "sub": "d1234", "iat": now, "exp": now + 300,
              "attributes": {"email": "Dev.One@Example.com", "name": "Dev One"}}
    claims.update(over)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def test_a_genuine_token_names_the_caller():
    caller, why = identity.from_headers({"X-ASM-RCTOKEN": rctoken()})
    assert why == "" and caller.email == "dev.one@example.com" and caller.name == "Dev One"


@pytest.mark.parametrize("token, reason", [
    (lambda: rctoken(key=STRANGER), "Signature verification failed"),
    (lambda: rctoken(iss="someone-else"), "issuer"),
    (lambda: rctoken(aud="another-app"), "audience"),
    (lambda: rctoken(exp=int(time.time()) - 600), "expired"),
    (lambda: rctoken(kid="unknown-kid"), "no key"),
    (lambda: jwt.encode({"iss": ISSUER, "exp": 9999999999, "attributes": {"email": "a@b.c"}},
                        "shared-secret", algorithm="HS256", headers={"kid": "k1"}), "algorithm"),
    (lambda: "not-a-jwt", "not a JWT"),
])
def test_a_forged_expired_or_foreign_token_names_nobody(token, reason):
    caller, why = identity.from_headers({"x-asm-rctoken": token()})
    assert caller is None and reason.lower() in why.lower()


def test_only_the_algorithm_the_issuer_declared_is_accepted():
    """The JWK says RS256; a token claiming RS512 with the same key is refused."""
    now = int(time.time())
    other_alg = jwt.encode({"iss": ISSUER, "aud": AUD, "exp": now + 300,
                            "attributes": {"email": "dev@example.com"}}, SIGNER,
                           algorithm="RS512", headers={"kid": "k1"})
    caller, why = identity.from_headers({"x-asm-rctoken": other_alg})
    assert caller is None and "alg" in why.lower()


def test_an_unsigned_alg_none_token_is_refused():
    now = int(time.time())
    forged = jwt.encode({"iss": ISSUER, "aud": AUD, "exp": now + 300,
                         "attributes": {"email": "boss@example.com"}}, None, algorithm="none")
    caller, why = identity.from_headers({"x-asm-rctoken": forged})
    assert caller is None and "algorithm" in why


def test_a_verified_token_without_an_email_is_explained():
    caller, why = identity.from_headers({"x-asm-rctoken": rctoken(attributes={"name": "x"})})
    assert caller is None and "UserAuthConfig" in why


def test_off_by_default_and_no_header_is_not_signed_in(monkeypatch):
    assert identity.from_headers({})[0] is None
    monkeypatch.setattr(identity.settings, "identity_header", "", raising=False)
    assert "off" in identity.from_headers({"x-asm-rctoken": rctoken()})[1]


def test_keys_are_fetched_once_not_per_request(monkeypatch):
    calls = []
    real = identity._fetch_keys
    monkeypatch.setattr(identity, "_fetch_keys", lambda: calls.append(1) or real())
    for _ in range(5):
        assert identity.from_headers({"x-asm-rctoken": rctoken()})[0]
    assert len(calls) == 1


def test_cluster_local_keys_bypass_the_corporate_proxy():
    assert identity._cluster_local("http://authservice.asm-user-auth.svc.cluster.local:10004/x")
    assert not identity._cluster_local("https://login.example.com/jwks")


# --- what the verified caller changes ------------------------------------------
def _req(token=None):
    return SimpleNamespace(headers={"x-asm-rctoken": token} if token else {})


def test_the_verified_email_beats_the_typed_one(monkeypatch):
    seen = {}
    monkeypatch.setattr("release_agent.tools.queue_gate.queue_release_intent",
                        lambda **kw: seen.update(kw) or {"ok": True})
    APP.release_queue_add_batch(
        APP.QueueBatchRequest(rows=[APP.QueueRow(artifact="a:1")], requested_by="typed@else.com"),
        _req(rctoken()))
    assert seen["requested_by"] == "dev.one@example.com"


def test_withdraw_is_recorded_against_the_caller(monkeypatch):
    seen = {}
    monkeypatch.setattr("release_agent.tools.release_queue.withdraw_intent",
                        lambda name, who, version="": seen.update(who=who) or {"ok": True})
    APP.release_queue_withdraw(APP.QueueWithdrawRequest(artifact_name="a", requested_by="x@y.z"),
                               _req(rctoken()))
    assert seen["who"] == "dev.one@example.com"


def test_required_mode_refuses_writes_without_a_verified_caller(monkeypatch):
    monkeypatch.setattr(identity.settings, "identity_required", True, raising=False)
    out = APP.release_queue_add_batch(
        APP.QueueBatchRequest(rows=[APP.QueueRow(artifact="a:1")], requested_by="t@x.com"),
        _req(rctoken(key=STRANGER)))
    assert out["ok"] is False and "Sign-in required" in out["error"]


def test_without_required_mode_the_typed_email_still_works(monkeypatch):
    seen = {}
    monkeypatch.setattr("release_agent.tools.queue_gate.queue_release_intent",
                        lambda **kw: seen.update(kw) or {"ok": True})
    APP.release_queue_add_batch(
        APP.QueueBatchRequest(rows=[APP.QueueRow(artifact="a:1")], requested_by="typed@x.com"), _req())
    assert seen["requested_by"] == "typed@x.com"


def test_whoami_reports_the_caller_or_why_not():
    assert APP.whoami(_req(rctoken()))["email"] == "dev.one@example.com"
    out = APP.whoami(_req(rctoken(key=STRANGER)))
    assert out["signed_in"] is False


def test_the_chat_tools_record_the_caller_not_what_the_model_typed(monkeypatch):
    from adk_release_agent import tools as T

    seen = {}
    monkeypatch.setattr("release_agent.tools.release_queue.withdraw_intent",
                        lambda name, who, version="": seen.update(who=who) or {"ok": True})
    with identity.activate(identity.Caller(email="dev.one@example.com")):
        T.withdraw_release_intent("a", requested_by="made-up@else.com")
    assert seen["who"] == "dev.one@example.com"


def test_adk_sessions_are_per_caller():
    from release_agent import adk_service as S

    assert S._user_id() == S._USER_ID
    with identity.activate(identity.Caller(email="dev.one@example.com")):
        assert S._user_id() == "dev.one@example.com"


def test_a_threads_github_token_is_withheld_from_anyone_but_its_owner():
    store = SessionCredentialStore()
    store.set("t1", SessionCredentials(pat_token="ghp_x", owner="dev.one@example.com"))
    assert store.get("t1", owner="dev.one@example.com").pat_token == "ghp_x"
    assert store.get("t1", owner="someone@example.com") is None
    assert store.get("t1", owner="") is None, "an anonymous caller is not the owner"
    with store.activate("t1", owner="someone@example.com") as creds:
        assert creds is None
    assert store.get("t1").pat_token == "ghp_x", "identity off: no check, as before"


def test_nobody_can_replace_the_token_on_someone_elses_thread():
    """Found in multi-user testing: ``get`` withheld another person's token but
    ``set`` did not check at all, so knowing a thread id (not a secret) was
    enough to EVICT the owner's PAT — their next GitHub action then ran as the
    server token instead of as them."""
    store = SessionCredentialStore()
    assert store.set("t1", SessionCredentials(pat_token="ghp_one", owner="dev.one@example.com"))
    assert not store.set("t1", SessionCredentials(pat_token="ghp_two", owner="dev.two@example.com"))
    assert store.get("t1", owner="dev.one@example.com").pat_token == "ghp_one"
    # the owner may still reconnect, and identity-off behaviour is unchanged
    assert store.set("t1", SessionCredentials(pat_token="ghp_new", owner="dev.one@example.com"))
    assert store.set("t2", SessionCredentials(pat_token="ghp_a"))
    assert store.set("t2", SessionCredentials(pat_token="ghp_b"))


def test_a_confirm_token_is_bound_to_the_caller_it_was_minted_for(monkeypatch):
    """Found in multi-user testing: ``_PENDING_PREVIEWS`` is process-wide and
    keyed by token alone, and the token is printed in the chat — so the
    stateless fallback in adk_service let anyone who saw someone else's token
    APPLY their pending deploy from their own thread."""
    from adk_release_agent import deploy as D

    monkeypatch.setattr(D, "_invoke_tool", lambda name, args=None: {"ok": True})
    D._PENDING_PREVIEWS.clear()
    with identity.activate(identity.Caller(email="dev.one@example.com")):
        prep = D.prepare_deploy_preview(image_tags="payments-api:1.4.2", environment="uat")
    assert D._PENDING_PREVIEWS[prep["token"]]["owner"] == "dev.one@example.com"
    with identity.activate(identity.Caller(email="dev.two@example.com")):
        from release_agent import adk_service as S

        assert S._user_id() != D._PENDING_PREVIEWS[prep["token"]]["owner"]
    D._PENDING_PREVIEWS.clear()


def test_diagnostics_say_whether_the_token_verified():
    ok = APP._verified_identity({"x-asm-rctoken": rctoken()})
    assert ok["signed_in"] and ok["email"] != "dev.one@example.com", "masked"
    bad = APP._verified_identity({"x-asm-rctoken": rctoken(aud="other")})
    assert not bad["signed_in"] and "audience" in bad["reason"].lower()


def test_deploys_and_releases_record_who_confirmed(monkeypatch):
    """Found in E2E: deploy and release events carried no actor. With identity
    on, the verified caller who confirmed is recorded — never a typed email."""
    from release_agent.tools import release_queue as RQ

    written = []
    monkeypatch.setattr(RQ, "_insert", lambda rows: written.extend(rows) or {"ok": True})
    with identity.activate(identity.Caller(email="dev.one@example.com")):
        RQ.record_deployment("uat", [{"name": "a", "tag": "1"}], "o/deploy", 7)
        RQ.mark_released("R1", 8, [{"name": "a", "tag": "1"}])
    RQ.record_deployment("uat", [{"name": "b", "tag": "2"}], "o/deploy", 9)
    assert [w["requested_by"] for w in written] == ["dev.one@example.com", "dev.one@example.com", None]


def test_a_settled_pending_pr_keeps_who_confirmed_it():
    from release_agent.tools import pr_reconcile as R

    pending = {"event_type": "pending", "note": R.pending_note("deployed", "a:1"),
               "deployment_repo": "o/d", "pr_number": 5, "environment": "prd",
               "artifact_name": "a", "artifact_version": "1", "requested_by": "dev.one@example.com"}
    [row] = R.resolution_rows([pending], "merged", None, "approver")
    assert row["requested_by"] == "dev.one@example.com"


def test_required_mode_lets_a_signed_in_user_queue_through_the_forms(monkeypatch):
    """Found in round 3: the REST endpoints verified the caller but never bound
    it, and the batch rows run on pool threads with an empty context — so the
    queue tool's own check refused a SIGNED-IN user in required mode."""
    from release_agent.tools import release_queue as RQ

    monkeypatch.setattr(identity.settings, "identity_required", True, raising=False)
    monkeypatch.setattr(RQ, "queue_enabled", lambda: True, raising=False)
    seen = []

    def fake_intent(**kw):
        who, refused = identity.actor(kw["requested_by"], identity.current())
        seen.append((who, refused))
        return {"ok": not refused, "error": refused}

    monkeypatch.setattr("release_agent.tools.queue_gate.queue_release_intent", fake_intent)
    rows = [APP.QueueRow(artifact="a:1", build_run_url="u", jira_ticket="J-1"),
            APP.QueueRow(artifact="b:2", build_run_url="u", jira_ticket="J-2")]
    out = APP.release_queue_add_batch(
        APP.QueueBatchRequest(rows=rows, requested_by="typed@x.com", change_details="d"), _req(rctoken()))
    assert out["ok"] and not out["refused"]
    assert seen == [("dev.one@example.com", "")] * 2
    single = APP.release_queue_add_batch(
        APP.QueueBatchRequest(rows=[APP.QueueRow(artifact="a:1")], requested_by="t@x.com"), _req(rctoken()))
    assert single["ok"]
