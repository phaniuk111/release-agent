"""The runbook skill (tools/support/runbook.py): the portal reads each known
issue's header lines, the AI reads the whole text — one file, in the image."""
from __future__ import annotations

from release_agent.tools.support import runbook


def write(tmp_path, body):
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: support-runbook\ndescription: x\n---\n\n" + body)
    return path


def test_the_shipped_runbook_skill_loads_cleanly():
    entries, problems = runbook.load()
    assert problems == [] and len(entries) >= 4
    titles = [e["title"] for e in entries]
    assert "Upstream file not delivered" in titles and "Cloud quota exceeded" in titles
    for e in entries:
        assert e["action"] in runbook.ACTIONS and e["match"] and e["steps"]
    text = runbook.text()
    assert not text.startswith("---") and "## How to read our control table" in text
    assert "It is not this issue if" in text


def test_known_issues_parse_header_lines_steps_and_alternatives(tmp_path):
    path = write(tmp_path, """
## How to read our control table
match: this section is prose — never an issue

## Known issues

### Feed late
- match: not found, landing
- match: no files matched
- category: upstream
- when: system=SYS-A, process=REPORT-B
- action: Wait
- owner: Feed team
- steps:
  1. Ask the feed.
  2) Do not re-run.
  - Re-trigger once it lands.

Check: the run's own lines.
action: escalate        (prose later in the section never overrides the header)

### Quota
match: quota
action: retrigger
steps: Re-trigger once.
""")
    entries, problems = runbook.load(path)
    assert problems == []
    feed, quota = entries
    assert feed == {"title": "Feed late", "match": [["not found", "landing"], ["no files matched"]],
                    "action": "wait", "steps": ["Ask the feed.", "Do not re-run.", "Re-trigger once it lands."],
                    "escalate_to": "Feed team", "category": "upstream",
                    "when": {"system": "SYS-A", "process": "REPORT-B"}}
    assert quota["steps"] == ["Re-trigger once."] and quota["escalate_to"] is None


def test_a_match_line_needs_all_its_words_and_any_line_will_do():
    entry = {"match": [["not found", "landing"], ["no files matched"]]}
    assert runbook.matches(entry, "file not found in landing zone")
    assert not runbook.matches(entry, "file not found")
    assert runbook.matches(entry, "java.io: no files matched spec")


def test_an_issue_the_portal_cannot_use_is_skipped_and_named(tmp_path):
    path = write(tmp_path, """
## Known issues

### No match line
action: wait

### Bad action
match: x
action: reboot

### Bad category
match: y
action: check
category: weather

### Good
match: z
action: check
""")
    entries, problems = runbook.load(path)
    assert [e["title"] for e in entries] == ["Good"]
    assert problems == ["runbook issue 'No match line' has no match: line — skipped",
                        "runbook issue 'Bad action': action must be one of wait, retrigger, check, escalate — skipped",
                        "runbook issue 'Bad category': category must be one of upstream, process, unit, single, "
                        "spread — skipped"]


def test_a_missing_skill_is_a_problem_not_an_exception(tmp_path):
    entries, problems = runbook.load(tmp_path / "nope.md")
    assert entries == [] and problems == ["The runbook skill could not be read (nope.md)."]
    assert runbook.text(tmp_path / "nope.md") == ""
