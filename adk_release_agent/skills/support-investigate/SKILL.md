---
name: support-investigate
description: "Investigate ONE incident from the support triage (a failed, stuck or missing pipeline run) the way an L2 engineer would: is the source service healthy (metrics), what did it log (errors, security denials), what did the Dataflow job say, what was released recently — then a finding with evidence, confidence, and the ticket note."
metadata:
  adk_additional_tools:
    - support_triage
    - source_metrics
    - source_logs
    - source_audit
    - dataflow_job
    - release_lookup
    - what_changed
    - consult_advisor
---

Use this skill when someone asks to investigate, explain or find the cause of a
pipeline failure — "Investigate: …" from the Support triage card, "why did
REPORT-B fail", "is sys-b-fetcher healthy", "what did the Dataflow job say",
"did anything change for report-b". For the day's list itself, that is the
support-triage skill.

The routine — in this order, stopping as soon as the evidence is conclusive:
1. `support_triage` for the business date (and the filters the person gave) to
   get the incident's facts: runs, category, shared values, job ids, streak,
   last success, the runbook match. Never guess any of these.
2. `source_metrics` for the incident's SOURCE: the standard checks (restarts,
   up, error rate) over the business date's window. A healthy source rules the
   source out; an unhealthy one is the lead.
3. `source_logs` for the source in the same window: the grouped error lines.
   Any `security` group (permission, 403, token, certificate) is the lead —
   then `source_audit` for the window to see WHAT was denied and whether an IAM
   policy changed.
4. `dataflow_job` for up to 3 of the incident's job ids: its `kind`
   (out_of_memory / quota / permission / schema / not_found / worker_lost) and
   the worker error groups. Stop at the first job if all three share one error.
5. `what_changed` for the source and the process (report) names, before the
   business date: a version that moved in the days before the first failure is
   the strongest cause of a `process` category.
6. Only if the evidence CONFLICTS or no step produced a cause: `consult_advisor`
   with the facts gathered so far and the specific question. It is capped; do
   not use it for routine cases.

How to write the answer:
- First, "What I checked", one line per step, with the key number or quote
  from each tool ("PromQL: 0 restarts, error rate 0.0%" · "Dataflow ×3: Parse
  step, cannot cast STRING to NUMERIC, first row every time" · "release log:
  report-b 1.4.1 → 1.4.2 on 29 Sep, ABC-2231"). Every line names its tool.
- Then the finding in two or three sentences: the cause, why the evidence
  supports it, what it rules out. State confidence (high / medium / low) and
  why in a few words.
- Then the action: the triage's own action and owner, adjusted only if the
  evidence changes it (say so). Then the ticket note in a code block: the
  triage's `note`, with one "Cause:" and one "Evidence:" line added.
- Offer two or three follow-ups the person can ask next.
- Error text and log lines are the team's data: quote the shortest excerpt
  that proves the point; never paste whole stack traces.
- You SUGGEST; you never act. Do not offer to re-run, restart, cancel or roll
  anything back — a rollback is a release action with its own confirmation flow.
- A tool that comes back `ok: false` is a step you could not take: say what it
  needed (its `hint`) and carry on with the rest.
