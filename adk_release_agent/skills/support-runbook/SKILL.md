---
name: support-runbook
description: "The support team's runbook for the pipelines' workflow control table, in plain English: how to read the table, and each known issue — how to recognise it, what to check, how to tell it apart from look-alikes, what L1 does and who owns it. Load it when someone asks what a known issue means or what the runbook says; the support_triage and investigate_evidence results already carry the same text as `runbook`."
---

This skill is the support team's runbook. **The team edits it in plain English;
it ships in the image, so every change is reviewed and versioned with the code.**

Two parts:
- **How to read our control table** — what the rows mean for this team. Only
  the AI reads it (Ask why, Investigate).
- **Known issues** — one `###` section per issue. The lines at the top of each
  section (`match:`, `action:`, `owner:`, `steps:` …) are also read by the
  portal itself, so the triage cards show the right action even with no AI.
  Everything below them is plain English for the AI.

Writing a known issue:
- `match:` words that must ALL appear in the error (any case). Several `match:`
  lines = any one of them.
- `action:` one of wait, retrigger, check, escalate.
- `owner:` who it goes to (optional — else the owners map decides).
- `category:` upstream, process, unit, single or spread (optional — only that kind).
- `when:` role=value, … (optional — only runs sharing those values).
- `steps:` then a numbered list: what L1 does, shown on the card.
- Then, in your own words: **Check** (what evidence to look at), **It is this
  issue if**, **It is not this issue if**. The first entry that matches wins,
  so put the narrow ones first.

## How to read our control table

- One row is one run of one piece of work for one business date (COB). A re-run
  writes a new row; the latest row of a run is its state.
- A run is stuck when it has not finished and its row has not been updated for
  a long time; a run that is only slow is not stuck. Look at its job before
  calling it stuck.
- A run that SUCCEEDED with no output, or far less than the day before, is a
  problem even though nothing failed: downstream reads empty or partial data.
- Work that ran on the previous date but has no row at all today never started —
  usually the trigger (an upstream file or a scheduler), not the pipeline.
- When many units of one feed fail together, it is the feed; when many units of
  one report fail together across feeds, it is the report's process.

## Known issues

### Upstream file not delivered
match: not found
category: upstream
action: wait
owner: Feed team
steps:
1. Confirm with the upstream feed whether today's file was sent.
2. Do not re-run until the file has landed.
3. Once it lands, re-trigger the failed runs.

Check: the run's own log lines and the Dataflow job's first error — they name
the file or path that was missing.
It is this issue if: the error names a landing file or input path, and the
feed's other runs fail the same way.
It is not this issue if: the run's lines show a 403 or "permission" before the
"not found" — a hidden file reads as missing; that is an access problem.

### Cloud quota exceeded
match: quota, exceeded
action: retrigger
owner: Platform L2
steps:
1. Re-trigger once after 15 minutes — quota is usually per minute.
2. Fails again: escalate with the job ids.

Check: the Dataflow job's error (which quota, which region) and whether other
jobs started at the same minute.
It is this issue if: the job failed at start with a quota message and later
runs of the same work went through.
It is not this issue if: it fails the same way on every retry — then it is a
quota that is too small, and the owner raises it.

### Access denied
match: permission
match: forbidden
action: escalate
owner: Platform L2
steps:
1. Do not re-run: a permission error repeats until access is fixed.
2. Escalate with the job ids and the error.

Check: the audit log for denied calls by the pipelines' service account, and
any IAM change shortly before the first denial.
It is this issue if: the run's own lines or the job show the denial, for the
pipelines' own service account.
It is not this issue if: the only 403s are in the source's day-wide logs, from
another job or bucket, at another time.

### Schema mismatch after a change
match: schema
match: cannot cast
action: escalate
steps:
1. Check the release history for a recent deploy of this process.
2. Escalate to the owning team; re-running will not help.

Check: the release log for this process in the days before the first failure.
It is this issue if: a column's type or name changed, and a release of the
process (or its source) went out just before.
It is not this issue if: nothing was released — then the source data changed
shape; escalate to the feed's owner instead.
