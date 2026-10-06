"""The CARE release file is edited in place: only the values that change move.

A person reviews the release as a diff of this file, so a rewrite that also
reformats it (json.dumps) hides the change in noise — collapsing the blank
lines of an empty list, escaping every "—", dropping the final newline."""
import difflib
import json

import pytest

from release_agent.tools.json_splice import splice_top_level, top_level_spans

# The committed file as its owners lay it out — including the odd spacing
# before prl1_only's colon and the blank lines inside the empty list.
COMMITTED = """{
  "release_name": "<TEAM> CARE Release - 2026.09.24",
  "start_date": "2026-09-24 10:00:00",
  "end_date": "2026-09-25 23:00:00",
  "change_initiator": "someone@example.com",
  "change_summary": "<TEAM> CARE Release - 2026.09.24",
  "change_description": "Swagger fix — and memory heap improvements.",
  "change_reason": "Fixes the swagger page.",
  "associated_risk": "Low risk. Changes include swagger fix and memory heap improvements.",
  "consequence": "The change introduces minor changes and bug fixes across <TEAM> services.",
  "user_service_impact": "No user impact is expected. The change improves memory management and API reliability across <TEAM> services.",
  "prl1_only" : [


  ],
  "artefact": [
    "https://artifactory.example.com/docker/example-ds/svc-a:5.0.463",
    "https://artifactory.example.com/docker/example-ds/svc-b:5.0.458"
  ]
}
"""


def _changed_lines(old, new):
    diff = difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=0)
    return [line for line in diff if line[:1] in "+-" and line[:3] not in ("+++", "---")]


def test_only_the_edited_lines_change():
    doc = json.loads(COMMITTED)
    new = splice_top_level(COMMITTED, {
        **doc,
        "release_name": "<TEAM> CARE Release - 2026.10.01",
        "change_reason": "Tunes the heap.",
        "artefact": ["https://artifactory.example.com/docker/example-ds/svc-a:5.0.470",
                     "https://artifactory.example.com/docker/example-ds/svc-b:5.0.458",
                     "https://artifactory.example.com/docker/example-ds/svc-c:1.2.3"],
    })
    assert _changed_lines(COMMITTED, new) == [
        '-  "release_name": "<TEAM> CARE Release - 2026.09.24",',
        '+  "release_name": "<TEAM> CARE Release - 2026.10.01",',
        '-  "change_reason": "Fixes the swagger page.",',
        '+  "change_reason": "Tunes the heap.",',
        '-    "https://artifactory.example.com/docker/example-ds/svc-a:5.0.463",',
        '-    "https://artifactory.example.com/docker/example-ds/svc-b:5.0.458"',
        '+    "https://artifactory.example.com/docker/example-ds/svc-a:5.0.470",',
        '+    "https://artifactory.example.com/docker/example-ds/svc-b:5.0.458",',
        '+    "https://artifactory.example.com/docker/example-ds/svc-c:1.2.3"',
    ]
    assert list(json.loads(new)) == list(doc), "key order is the file's"


def test_an_unchanged_empty_list_and_the_final_newline_keep_their_bytes():
    doc = json.loads(COMMITTED)
    new = splice_top_level(COMMITTED, {**doc, "release_name": "R2", "prl1_only": []})
    assert '"prl1_only" : [\n\n\n  ],' in new, "blank lines inside the empty list survive"
    assert new.endswith("}\n")
    assert splice_top_level(COMMITTED, doc) == COMMITTED, "nothing changed, not one byte"
    no_newline = COMMITTED.rstrip("\n")
    assert splice_top_level(no_newline, {"release_name": "R2"}).endswith("]\n}"), \
        "a file without a final newline stays without one"


def test_a_changed_list_is_one_item_per_line_under_its_key():
    new = splice_top_level(COMMITTED, {"prl1_only": ["svc-a", "svc-b"]})
    assert '  "prl1_only" : [\n    "svc-a",\n    "svc-b"\n  ],' in new
    assert splice_top_level('{"a": [1], "b": 2}', {"a": []}) == '{"a": [], "b": 2}'


def test_a_key_the_file_does_not_have_is_refused_never_added():
    with pytest.raises(ValueError, match="df_images"):
        splice_top_level(COMMITTED, {"release_name": "R2", "df_images": []})


def test_non_ascii_prose_is_written_as_itself():
    new = splice_top_level(COMMITTED, {"consequence": "Heap fix — no API change → faster"})
    assert '"consequence": "Heap fix — no API change → faster",' in new
    assert "\\u2014" not in new and "\\u2192" not in new


@pytest.mark.parametrize("text", ["", "{", '{"a": 1,}', "[1, 2]", '"just a string"', "{} {}"])
def test_anything_but_a_json_object_is_refused(text):
    with pytest.raises(ValueError):
        splice_top_level(text, {})


def test_a_duplicate_key_is_refused():
    with pytest.raises(ValueError, match="duplicate"):
        splice_top_level('{"a": 1, "a": 2}', {"a": 3})


def test_spans_are_the_top_level_members_only():
    text = '{"a": {"b": 1}, "c" : [ "x" ] }'
    spans = top_level_spans(text)
    assert set(spans) == {"a", "c"}
    start, end, _ = spans["c"]
    assert text[start:end] == '[ "x" ]'
    assert splice_top_level(text, {"a": {"b": 1}, "c": ["x"]}) == text


def test_windows_line_endings_are_kept():
    text = '{\r\n  "a": [\r\n    1\r\n  ]\r\n}\r\n'
    assert splice_top_level(text, {"a": [2, 3]}) == '{\r\n  "a": [\r\n    2,\r\n    3\r\n  ]\r\n}\r\n'
