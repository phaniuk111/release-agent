"""Notifications over the portal event log: one item per scope, marking, ordering,
and never raising. The routes: gated, per verified recipient."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from release_agent import app_fastapi as APP
from release_agent import features, identity
from release_agent.config import settings
from release_agent.tools import events_store, notifications as N

ME = "me@example.com"
YOU = "you@example.com"


def _ago(**delta):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)).isoformat()


# ----- notify ------------------------------------------------------------------------

def test_notify_writes_a_created_unread_row_for_the_recipient():
    out = N.notify(ME, kind="release_pr", title="Release PR #7 merged", body="3 charts on UAT",
                   link="https://github.com/example-org/svc-a/pull/7", severity="warning", repo="example-org/svc-a")
    assert out["ok"] and out["event_type"] == "created" and out["subject"].startswith("notification:")
    (row,) = events_store.history("notification")["rows"]
    assert (row["domain"], row["event_type"], row["status"], row["recipient"]) == (
        "notification", "created", "unread", ME)
    assert (row["category"], row["severity"], row["repo"], row["actor"]) == (
        "release_pr", "warning", "example-org/svc-a", "portal")


def test_the_recipient_is_compared_lowercased():
    N.notify("Me@Example.com", kind="requeue", title="Back in the queue")
    assert N.unread_count(ME) == 1


def test_a_scope_with_a_created_row_gets_an_updated_row_with_the_same_subject():
    first = N.notify(ME, kind="deploy_started", title="Deploy started", scope="deploy:ex#7")
    second = N.notify(ME, kind="deploy_finished", title="Deploy finished", body="all green", scope="deploy:ex#7",
                      severity="info")
    assert (first["event_type"], second["event_type"]) == ("created", "updated")
    assert first["subject"] == second["subject"]
    items = N.list_for(ME)["items"]
    assert len(items) == 1, "ONE bell item whose text changed"
    assert items[0]["title"] == "Deploy finished" and items[0]["kind"] == "deploy_finished"
    assert items[0]["event_type"] == "updated"


def test_an_update_after_reading_makes_it_unread_again():
    a = N.notify(ME, kind="deploy_started", title="Started", scope="s")
    N.mark(ME, [a["subject"]], "read", ME)
    assert N.unread_count(ME) == 0
    N.notify(ME, kind="deploy_finished", title="Finished", scope="s")
    assert N.unread_count(ME) == 1


def test_the_same_scope_for_two_people_is_two_threads():
    a = N.notify(ME, kind="release_pr", title="mine", scope="release-pr:42")
    b = N.notify(YOU, kind="release_pr", title="yours", scope="release-pr:42")
    assert a["event_type"] == b["event_type"] == "created"
    assert len(N.list_for(ME)["items"]) == 1 and len(N.list_for(YOU)["items"]) == 1


def test_without_a_scope_every_notification_is_its_own_item():
    N.notify(ME, kind="requeue", title="one")
    N.notify(ME, kind="requeue", title="two")
    assert len(N.list_for(ME)["items"]) == 2


@pytest.mark.parametrize("kw", [
    {"recipient": "", "kind": "k", "title": "t"},
    {"recipient": ME, "kind": "", "title": "t"},
    {"recipient": ME, "kind": "k", "title": " "},
    {"recipient": ME, "kind": "k", "title": "t", "severity": "loud"},
    {"recipient": ME, "kind": "k", "title": "t", "not_a_column": 1},
])
def test_a_bad_notification_is_an_error_dict(kw):
    out = N.notify(**kw)
    assert out["ok"] is False and out["error"]
    assert events_store.history("notification")["rows"] == []


# ----- list / mark / unread ----------------------------------------------------------

def test_list_for_is_newest_first_and_only_the_recipients():
    events_store.record({"domain": "notification", "event_type": "created", "subject": "notification:old",
                         "recipient": ME, "status": "unread", "title": "old", "event_ts": _ago(hours=5)})
    events_store.record({"domain": "notification", "event_type": "created", "subject": "notification:new",
                         "recipient": ME, "status": "unread", "title": "new", "event_ts": _ago(hours=1)})
    N.notify(YOU, kind="requeue", title="not mine")
    out = N.list_for(ME)
    assert out["ok"] and [i["title"] for i in out["items"]] == ["new", "old"] and out["unread"] == 2
    assert set(out["items"][0]) >= {"subject", "kind", "title", "body", "link", "severity", "status", "at"}


def test_list_for_leaves_out_what_is_older_than_since_days():
    events_store.record({"domain": "notification", "event_type": "created", "subject": "notification:old",
                         "recipient": ME, "status": "unread", "title": "old", "event_ts": _ago(days=20)})
    assert N.list_for(ME, since_days=14)["items"] == []
    assert len(N.list_for(ME, since_days=30)["items"]) == 1


def test_mark_read_appends_a_row_and_keeps_the_text():
    a = N.notify(ME, kind="release_pr", title="Merged", body="b", link="l", scope="x")
    out = N.mark(ME, [a["subject"]], "read", ME)
    assert out == {"ok": True, "marked": [a["subject"]], "skipped": []}
    rows = events_store.history("notification", subject=a["subject"])["rows"]
    assert [r["event_type"] for r in rows] == ["read", "created"], "an append, never an edit"
    (item,) = N.list_for(ME)["items"]
    assert (item["status"], item["title"], item["body"], item["link"], item["kind"], item["scope"]) == (
        "read", "Merged", "b", "l", "release_pr", "x")
    assert rows[0]["actor"] == ME and N.unread_count(ME) == 0


def test_dismissed_is_kept_with_its_status():
    a = N.notify(ME, kind="requeue", title="t")
    N.mark(ME, [a["subject"]], "dismissed", ME)
    assert N.list_for(ME)["items"][0]["status"] == "dismissed"
    assert N.unread_count(ME) == 0


def test_reading_does_not_move_an_item_up_the_list():
    events_store.record({"domain": "notification", "event_type": "created", "subject": "notification:old",
                         "recipient": ME, "status": "unread", "title": "old", "event_ts": _ago(hours=5)})
    N.notify(ME, kind="requeue", title="new")
    N.mark(ME, ["notification:old"], "read", ME)
    assert [i["title"] for i in N.list_for(ME)["items"]] == ["new", "old"]
    N.mark(ME, ["notification:old"], "dismissed", ME)
    assert [i["title"] for i in N.list_for(ME)["items"]] == ["new", "old"], "even after a second mark"


def test_mark_touches_only_the_recipients_own_subjects():
    mine = N.notify(ME, kind="requeue", title="mine")
    yours = N.notify(YOU, kind="requeue", title="yours")
    out = N.mark(ME, [mine["subject"], yours["subject"], "notification:nope"], "read", ME)
    assert out["marked"] == [mine["subject"]] and out["skipped"] == [yours["subject"], "notification:nope"]
    assert N.unread_count(YOU) == 1
    assert len(events_store.history("notification", recipient=YOU)["rows"]) == 1


def test_marking_what_is_already_marked_writes_nothing():
    a = N.notify(ME, kind="requeue", title="t")
    N.mark(ME, [a["subject"]], "read", ME)
    before = len(events_store.history("notification")["rows"])
    out = N.mark(ME, [a["subject"]], "read", ME)
    assert out["marked"] == [] and len(events_store.history("notification")["rows"]) == before


def test_mark_refuses_a_status_that_is_not_a_mark():
    a = N.notify(ME, kind="requeue", title="t")
    assert N.mark(ME, [a["subject"]], "unread", ME)["ok"] is False
    assert N.mark(ME, [], "read", ME) == {"ok": True, "marked": [], "skipped": []}


# ----- never raises ------------------------------------------------------------------

def test_a_failing_store_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store is down")

    for name in ("record", "record_many", "latest", "history"):
        monkeypatch.setattr(events_store, name, boom)
    assert N.notify(ME, kind="k", title="t", scope="s")["ok"] is False
    assert N.list_for(ME)["ok"] is False
    assert N.mark(ME, ["notification:a"], "read", ME)["ok"] is False
    assert N.unread_count(ME) == 0


def test_an_error_dict_from_the_store_comes_through(monkeypatch):
    err = {"ok": False, "error": "BigQuery unavailable"}
    monkeypatch.setattr(events_store, "latest", lambda *a, **k: err)
    monkeypatch.setattr(events_store, "record", lambda row: err)
    assert N.list_for(ME) == err and N.unread_count(ME) == 0
    assert N.notify(ME, kind="k", title="t") == err


def test_a_failed_scope_lookup_still_notifies(monkeypatch):
    monkeypatch.setattr(events_store, "history", lambda *a, **k: {"ok": False, "error": "down"})
    out = N.notify(ME, kind="k", title="t", scope="s")
    assert out["ok"] and out["subject"] == "notification:s", "the scope makes the subject, so the bell stays one item"


# ----- routes ------------------------------------------------------------------------

TESTER = identity.Caller(email="Tester@Example.com")
OTHER = identity.Caller(email="other@example.com")


@pytest.fixture
def client():
    return TestClient(APP.app)


@pytest.fixture
def signed_in(monkeypatch):
    def _as(caller):
        monkeypatch.setattr(APP, "_caller", lambda request: caller)
    monkeypatch.setattr(features.settings, "preview_users", "", raising=False)
    monkeypatch.setattr(features.settings, "preview_features", "", raising=False)
    return _as


def test_the_routes_are_a_preview_feature_by_default():
    assert "notifications" in type(settings).model_fields["preview_features"].default
    assert features.refusal("notifications") == "Notifications is not available yet — it is a preview feature."


def test_every_route_refuses_when_the_feature_is_off_for_the_caller(monkeypatch, client):
    monkeypatch.setattr(features.settings, "preview_features", "notifications", raising=False)
    monkeypatch.setattr(features.settings, "preview_users", "someone-else@example.com", raising=False)
    monkeypatch.setattr(APP, "_caller", lambda request: OTHER)
    N.notify(OTHER.email, kind="requeue", title="t")
    for res in (client.get("/api/notifications"),
                client.post("/api/notifications/mark", json={"subjects": ["x"], "status": "read"}),
                client.post("/api/notifications/read-all")):
        assert res.status_code == 403
        assert res.json() == {"ok": False, "error": features.refusal("notifications")}
    assert N.unread_count(OTHER.email) == 1, "nothing was marked"


def test_a_preview_user_gets_the_feature(monkeypatch, client):
    monkeypatch.setattr(features.settings, "preview_features", "notifications", raising=False)
    monkeypatch.setattr(features.settings, "preview_users", "tester@example.com", raising=False)
    monkeypatch.setattr(APP, "_caller", lambda request: TESTER)
    assert client.get("/api/notifications").status_code == 200


def test_the_page_is_told_the_feature_is_on(monkeypatch):
    monkeypatch.setattr(features.settings, "preview_features", "notifications,deploy-tracking", raising=False)
    monkeypatch.setattr(features.settings, "preview_users", "tester@example.com", raising=False)
    # Only notifications is gated in this test; the other UI features stay open.
    assert "notifications" in features.ui_config(TESTER)["features"]
    assert "notifications" not in features.ui_config(OTHER)["features"]
    assert "notifications" not in features.ui_config(None)["features"]


def test_verified_identity_lists_only_its_own_rows(signed_in, client):
    N.notify("tester@example.com", kind="release_pr", title="mine", link="https://example.com/pull/1")
    N.notify(OTHER.email, kind="release_pr", title="theirs")
    signed_in(TESTER)
    body = client.get("/api/notifications?since_days=14").json()
    assert body["ok"] and body["unread"] == 1 and [i["title"] for i in body["items"]] == ["mine"]
    assert "unverified" not in body
    assert dt.datetime.fromisoformat(body["checked_at"]).tzinfo is not None


def test_a_typed_email_never_overrides_the_verified_one(signed_in, client):
    N.notify(OTHER.email, kind="release_pr", title="theirs")
    signed_in(TESTER)
    body = client.get("/api/notifications", params={"email": OTHER.email}).json()
    assert body["items"] == []


def test_with_identity_off_the_typed_email_is_used_and_flagged_unverified(signed_in, client):
    N.notify(YOU, kind="release_pr", title="yours")
    signed_in(None)
    body = client.get("/api/notifications", params={"email": YOU}).json()
    assert body["ok"] and body["unverified"] is True and [i["title"] for i in body["items"]] == ["yours"]
    empty = client.get("/api/notifications").json()
    assert empty["ok"] and empty["items"] == [] and empty["unverified"] is True


def test_with_identity_on_an_unsigned_request_is_refused_not_trusted(monkeypatch, signed_in, client):
    N.notify(YOU, kind="release_pr", title="yours")
    signed_in(None)
    monkeypatch.setattr(identity.settings, "identity_header", "x-rctoken", raising=False)
    body = client.get("/api/notifications", params={"email": YOU}).json()
    assert body["ok"] is False and "Sign-in" in body["error"] and "items" not in body
    assert client.post("/api/notifications/mark", json={"subjects": ["x"], "status": "read", "email": YOU}
                       ).json()["ok"] is False


def test_mark_marks_only_the_callers_subjects(signed_in, client):
    mine = N.notify("tester@example.com", kind="requeue", title="mine")
    theirs = N.notify(OTHER.email, kind="requeue", title="theirs")
    signed_in(TESTER)
    body = client.post("/api/notifications/mark",
                       json={"subjects": [mine["subject"], theirs["subject"]], "status": "dismissed"}).json()
    assert body["ok"] and body["marked"] == [mine["subject"]] and body["skipped"] == [theirs["subject"]]
    assert body["unread"] == 0 and "unverified" not in body
    assert N.unread_count(OTHER.email) == 1
    rows = events_store.history("notification", subject=mine["subject"], event_type="dismissed")["rows"]
    assert rows[0]["actor"] == "Tester@Example.com", "the verified email, as the caller was signed in"


def test_mark_rejects_a_status_that_is_not_a_mark(signed_in, client):
    a = N.notify("tester@example.com", kind="requeue", title="t")
    signed_in(TESTER)
    body = client.post("/api/notifications/mark", json={"subjects": [a["subject"]], "status": "deleted"}).json()
    assert body["ok"] is False
    assert N.unread_count("tester@example.com") == 1


def test_mark_with_identity_off_needs_and_flags_the_typed_email(signed_in, client):
    a = N.notify(YOU, kind="requeue", title="t")
    signed_in(None)
    assert client.post("/api/notifications/mark", json={"subjects": [a["subject"]], "status": "read"}
                       ).json()["ok"] is False
    body = client.post("/api/notifications/mark",
                       json={"subjects": [a["subject"]], "status": "read", "email": YOU}).json()
    assert body["ok"] and body["unverified"] is True and body["unread"] == 0
    rows = events_store.history("notification", subject=a["subject"], event_type="read")["rows"]
    assert "actor" not in rows[0], "a typed email is a claim, not an actor"


def test_read_all_marks_every_unread_of_the_caller_and_no_one_elses(signed_in, client):
    N.notify("tester@example.com", kind="requeue", title="a")
    N.notify("tester@example.com", kind="requeue", title="b")
    N.notify(OTHER.email, kind="requeue", title="c")
    signed_in(TESTER)
    body = client.post("/api/notifications/read-all").json()
    assert body["ok"] and len(body["marked"]) == 2 and body["unread"] == 0
    assert N.unread_count(OTHER.email) == 1


def test_a_store_outage_is_an_error_body_not_a_500(signed_in, client, monkeypatch):
    signed_in(TESTER)
    monkeypatch.setattr(events_store, "latest", lambda *a, **k: {"ok": False, "error": "BigQuery unavailable"})
    res = client.get("/api/notifications")
    assert res.status_code == 200 and res.json()["ok"] is False


# ----- a busy recipient: the 500-row default of `latest` must not hide their older items ----------

def _seed(recipient, count, *, prefix="notification:busy-"):
    """``count`` unread items for ``recipient``, subject N older than N+1 (seconds apart)."""
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    out = events_store.record_many([
        {"domain": "notification", "event_type": "created", "subject": f"{prefix}{n}", "recipient": recipient,
         "status": "unread", "title": f"t{n}", "event_ts": (base + dt.timedelta(seconds=n)).isoformat()}
        for n in range(count)])
    assert out["ok"]


def test_read_all_marks_every_unread_even_past_the_500_newest():
    _seed(ME, 600)
    assert N.unread_count(ME) == 600
    out = N.read_all(ME, ME)
    assert out["ok"] and len(out["marked"]) == 600 and out["skipped"] == []
    assert N.unread_count(ME) == 0
    assert N.read_all(ME, ME) == {"ok": True, "marked": [], "skipped": []}, "nothing left to write"


def test_the_read_all_route_marks_all_600_and_leaves_the_badge_at_zero(signed_in, client):
    _seed("tester@example.com", 600)
    _seed(OTHER.email, 3, prefix="notification:theirs-")
    signed_in(TESTER)
    body = client.post("/api/notifications/read-all").json()
    assert body["ok"] and len(body["marked"]) == 600 and body["unread"] == 0
    assert N.unread_count(OTHER.email) == 3


def test_mark_finds_a_subject_older_than_the_500_newest():
    _seed(ME, 600)      # busy-0 is the oldest: 599 newer items sit above it
    assert "notification:busy-0" not in {r["subject"] for r in events_store.latest(
        "notification", recipient=ME)["rows"]}, "the default cap really hides it"
    out = N.mark(ME, ["notification:busy-0"], "read", ME)
    assert out == {"ok": True, "marked": ["notification:busy-0"], "skipped": []}
    assert events_store.latest("notification", recipient=ME, subject="notification:busy-0")["rows"][0]["status"] == "read"


def test_another_recipients_subject_is_still_skipped_among_a_busy_list():
    _seed(ME, 600)
    _seed(YOU, 2, prefix="notification:yours-")
    out = N.mark(ME, ["notification:busy-0", "notification:yours-1"], "read", ME)
    assert out["marked"] == ["notification:busy-0"] and out["skipped"] == ["notification:yours-1"]
    assert N.unread_count(YOU) == 2
    assert N.read_all(ME, ME)["ok"] and N.unread_count(YOU) == 2, "read-all is the caller's own"


def test_the_recipient_filter_comes_before_the_cap():
    _seed(YOU, 700, prefix="notification:noisy-")
    _seed(ME, 3)
    assert len(events_store.latest("notification", recipient=ME)["rows"]) == 3
    assert len(events_store.latest("notification", recipient=YOU)["rows"]) == 500
    assert len(events_store.latest("notification", recipient=YOU, limit=650)["rows"]) == 650
    assert len(events_store.latest("notification", recipient=YOU, limit=10**9)["rows"]) == 700, "clamped, not refused"


def test_mark_refuses_more_than_200_subjects_in_one_call():
    _seed(ME, 250)
    out = N.mark(ME, [f"notification:busy-{n}" for n in range(201)], "read", ME)
    assert out["ok"] is False and "200" in out["error"]
    assert N.unread_count(ME) == 250, "nothing was written"
    ok = N.mark(ME, [f"notification:busy-{n}" for n in range(200)], "read", ME)
    assert ok["ok"] and len(ok["marked"]) == 200


def test_a_failed_point_read_marks_nothing(monkeypatch):
    _seed(ME, 2)
    real = events_store.latest

    def flaky(domain, **kw):
        if kw.get("subject") == "notification:busy-1":
            return {"ok": False, "error": "BigQuery unavailable"}
        return real(domain, **kw)
    monkeypatch.setattr(events_store, "latest", flaky)
    out = N.mark(ME, ["notification:busy-0", "notification:busy-1"], "read", ME)
    assert out == {"ok": False, "error": "BigQuery unavailable"}
    monkeypatch.setattr(events_store, "latest", real)
    assert N.unread_count(ME) == 2
