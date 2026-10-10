"""CHG / RMG tickets read from a PR's body and comments (tools/tickets.py): the
tokenizer, the newest-mention-wins reading, the cache, the event-log note."""
import datetime as dt
from types import SimpleNamespace

import pytest

from release_agent.config import settings
from release_agent.tools import events_store, notifications as N, tickets as T

REPO = "example-org/deploy"
PR_URL = f"https://github.com/{REPO}/pull/7"


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "ticket_prefixes", "CHG,RMG", raising=False)
    monkeypatch.setattr(settings, "ticket_state_words", "created,approved,rejected,closed,cancelled",
                        raising=False)
    T.clear_cache()
    yield
    T.clear_cache()


def _ids(text, prefixes=None):
    return [(t.id, t.state) for t in T.tickets_in(text, prefixes)]


# ----- the tokenizer -----------------------------------------------------------------

def test_a_ticket_is_prefix_dash_digits_or_prefix_digits_in_any_case():
    assert _ids("CHG-12345 created") == [("CHG-12345", "created")]
    assert _ids("chg-12345 created") == [("CHG-12345", "created")]
    assert _ids("CHG12345 created") == [("CHG-12345", "created")]
    assert _ids("Chg12345") == [("CHG-12345", "")]


def test_punctuation_around_a_ticket_is_not_part_of_it():
    assert _ids("Change (CHG-12345), see #CHG-12346.") == [("CHG-12345", ""), ("CHG-12346", "")]
    assert _ids("- CHG-12345: approved") == [("CHG-12345", "approved")]
    assert _ids("ticket=CHG-1;RMG-2") == [("CHG-1", ""), ("RMG-2", "")]
    assert _ids("[CHG-9]") == [("CHG-9", "")]


def test_only_the_configured_prefixes_count_and_digits_must_follow():
    assert _ids("CHG-12345 and INC-1 and CHGX-1 and CHG- and CHG-12a") == [("CHG-12345", "")]
    assert _ids("INC-1 CHG-2", ["INC"]) == [("INC-1", "")]
    assert _ids("INC-1 CHG-2", []) == []


def test_the_state_is_the_last_state_word_on_the_same_line():
    assert _ids("CHG-12345 created then Approved") == [("CHG-12345", "approved")]
    assert _ids("approved: CHG-12345 was rejected") == [("CHG-12345", "rejected")]
    assert _ids("CHG-12345 created\nCHG-12345 approved") == [("CHG-12345", "created"),
                                                           ("CHG-12345", "approved")]
    assert _ids("CHG-12345\napproved") == [("CHG-12345", "")], "another line's word is not its state"
    assert _ids("CHG-12345 recreated") == [("CHG-12345", "")], "a state word inside a word is not one"


def test_leading_zeros_are_part_of_the_name():
    assert _ids("CHG-0001691") == [("CHG-0001691", "")]


# ----- a PR's tickets ------------------------------------------------------------------

def test_the_newest_mention_per_prefix_wins_body_then_comments_oldest_first():
    out = T.tickets_from("CHG-1 created\nRMG-5", PR_URL, [
        {"body": "CHG-2 created", "html_url": PR_URL + "#issuecomment-1"},
        {"body": "CHG-2 approved", "html_url": PR_URL + "#issuecomment-2"},
    ])
    assert out == {
        "chg": {"id": "CHG-2", "state": "approved", "source": "comment", "url": PR_URL + "#issuecomment-2"},
        "rmg": {"id": "RMG-5", "state": "", "source": "body", "url": PR_URL},
    }


def test_two_tickets_in_one_text_the_later_wins():
    out = T.tickets_from("CHG-1 created\nCHG-2 created", PR_URL, [])
    assert out["chg"]["id"] == "CHG-2" and out["rmg"] is None


def test_a_prefix_nobody_named_is_none_not_missing():
    assert T.tickets_from("nothing here", PR_URL, []) == {"chg": None, "rmg": None}


class _Comment:
    def __init__(self, body, n, when):
        self.body, self.html_url, self.created_at = body, f"{PR_URL}#issuecomment-{n}", when


class _GitHub:
    def __init__(self, body="", comments=(), fail=None):
        self.body, self.comments, self.fail, self.reads = body, list(comments), fail, 0

    def get_repo(self, full):
        gh = self

        class _Repo:
            def get_pull(self, n):
                gh.reads += 1
                if gh.fail:
                    raise gh.fail
                return SimpleNamespace(body=gh.body, html_url=PR_URL, get_issue_comments=lambda: list(gh.comments))
        return _Repo()


def test_pr_tickets_reads_the_pr_once_per_ten_minutes(monkeypatch):
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    gh = _GitHub("CHG-1 created", [_Comment("CHG-1 approved", 1, t0)])
    first = T.pr_tickets(REPO, 7, github=gh)
    assert first["chg"] == {"id": "CHG-1", "state": "approved", "source": "comment",
                            "url": PR_URL + "#issuecomment-1"}
    gh.body, gh.comments = "CHG-2 created", []
    assert T.pr_tickets(REPO, 7, github=gh)["chg"]["id"] == "CHG-1" and gh.reads == 1, "served from the cache"
    assert T.pr_tickets(REPO, 7, github=gh, fresh=True)["chg"]["id"] == "CHG-2" and gh.reads == 2
    assert T.pr_tickets(REPO, 8, github=gh)["chg"]["id"] == "CHG-2" and gh.reads == 3, "another PR is another read"

    clock = {"now": 1000.0}
    monkeypatch.setattr(T.time, "monotonic", lambda: clock["now"])
    T.pr_tickets(REPO, 7, github=gh, fresh=True)
    clock["now"] += 601
    T.pr_tickets(REPO, 7, github=gh)
    assert gh.reads == 5, "an entry older than ten minutes is read again"


def test_comments_are_read_oldest_first_whatever_order_github_lists_them():
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    gh = _GitHub("", [_Comment("CHG-1 approved", 2, t0 + dt.timedelta(hours=1)),
                      _Comment("CHG-1 created", 1, t0)])
    assert T.pr_tickets(REPO, 7, github=gh)["chg"]["state"] == "approved"


def test_a_pr_that_cannot_be_read_is_an_error_dict_never_a_raise_and_never_cached():
    gh = _GitHub(fail=RuntimeError("502 bad gateway"))
    out = T.pr_tickets(REPO, 7, github=gh)
    assert out["error"].startswith("RuntimeError")
    gh.fail = None
    gh.body = "CHG-1"
    assert T.pr_tickets(REPO, 7, github=gh)["chg"]["id"] == "CHG-1"
    assert "error" in T.pr_tickets("", 7, github=gh)


def test_columns_are_empty_strings_when_a_ticket_is_absent_or_the_read_failed():
    assert T.columns({"chg": {"id": "CHG-1", "state": "approved"}, "rmg": None}) == {
        "chg": "CHG-1", "chg_state": "approved", "rmg": "", "rmg_state": ""}
    assert T.columns({"error": "x"}) == {"chg": "", "chg_state": "", "rmg": "", "rmg_state": ""}
    assert T.columns(None) == {"chg": "", "chg_state": "", "rmg": "", "rmg_state": ""}


# ----- the event-log note and the requester's bell --------------------------------------

def _tickets(chg_id="CHG-1", state="created"):
    return {"chg": {"id": chg_id, "state": state, "source": "comment", "url": PR_URL + "#issuecomment-1"},
            "rmg": None}


def test_the_first_chg_on_a_pending_pr_is_recorded_and_told_to_the_requester():
    out = T.note_first_seen(REPO, 7, _tickets(), requested_by="me@example.com", what="svc-a:2.0.0")
    assert out == {"recorded": True, "notified": True}
    (row,) = events_store.history("deploy")["rows"]
    assert (row["event_type"], row["scope"], row["chg"], row["chg_state"], row["pr_number"]) == (
        "tickets", f"deploy:{REPO}#7", "CHG-1", "created", 7)
    assert "rmg" not in row, "an absent ticket is a NULL column, not an empty string"
    (item,) = N.list_for("me@example.com")["items"]
    assert item["kind"] == "chg" and item["title"] == "CHG-1 created for release svc-a:2.0.0"
    assert item["link"] == PR_URL + "#issuecomment-1" and item["scope"] == f"chg:{REPO}#7"


def test_a_state_change_is_a_new_row_but_not_a_new_bell_item():
    T.note_first_seen(REPO, 7, _tickets(), requested_by="me@example.com", what="svc-a:2.0.0")
    out = T.note_first_seen(REPO, 7, _tickets(state="approved"), requested_by="me@example.com", what="svc-a:2.0.0")
    assert out == {"recorded": True, "notified": False}
    rows = events_store.history("deploy", scope=f"deploy:{REPO}#7")["rows"]
    assert [r["chg_state"] for r in rows] == ["approved", "created"]
    assert N.unread_count("me@example.com") == 1


def test_nothing_new_writes_nothing():
    T.note_first_seen(REPO, 7, _tickets(), requested_by="me@example.com", what="svc-a:2.0.0")
    assert T.note_first_seen(REPO, 7, _tickets(), requested_by="me@example.com", what="svc-a:2.0.0") == {
        "recorded": False, "notified": False}
    assert len(events_store.history("deploy")["rows"]) == 1
    assert T.note_first_seen(REPO, 8, {"chg": None, "rmg": None}, requested_by="me@example.com", what="x") == {
        "recorded": False, "notified": False}
    assert T.note_first_seen(REPO, 9, {"error": "502"}, requested_by="me@example.com", what="x") == {
        "recorded": False, "notified": False}


def test_a_new_chg_id_on_the_same_pr_is_told_again_as_the_same_bell_item():
    T.note_first_seen(REPO, 7, _tickets("CHG-1"), requested_by="me@example.com", what="svc-a:2.0.0")
    out = T.note_first_seen(REPO, 7, _tickets("CHG-2"), requested_by="me@example.com", what="svc-a:2.0.0")
    assert out["notified"]
    (item,) = N.list_for("me@example.com")["items"]
    assert item["title"].startswith("CHG-2") and item["event_type"] == "updated"


def test_no_requester_means_a_record_but_no_bell():
    out = T.note_first_seen(REPO, 7, _tickets(), requested_by="", what="svc-a:2.0.0")
    assert out == {"recorded": True, "notified": False}


def test_note_first_seen_never_raises(monkeypatch):
    monkeypatch.setattr(events_store, "history", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    out = T.note_first_seen(REPO, 7, _tickets(), requested_by="me@example.com", what="x")
    assert out["recorded"] is False and "down" in out["error"]
