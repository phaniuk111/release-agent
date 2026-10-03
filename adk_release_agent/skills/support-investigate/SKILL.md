---
name: support-investigate
description: "Investigate ONE incident from the support triage (a failed, stuck or missing pipeline run) the way an L2 engineer would: is the source service healthy (metrics), what did it log (errors, security denials), what did the Dataflow job say, what was released recently — then a finding with evidence, confidence, and the ticket note."
metadata:
  adk_additional_tools:
    - investigate_evidence
    - support_triage
    - source_metrics
    - source_logs
    - source_audit
    - dataflow_job
    - run_logs
    - release_lookup
    - what_changed
    - consult_advisor
---

Use this skill when someone asks to investigate, explain or find the cause of a
pipeline failure — "why did REPORT-B fail", "is sys-b-fetcher healthy", "what did
the Dataflow job say", "did anything change for report-b". For the day's list
itself, that is the support-triage skill.

The Investigate button on the Support triage card does NOT come here: the portal
collects the evidence in code and writes the finding in one model step. If the
person's thread already holds such a finding, it is given to you under the
instructions — answer follow-ups from it first, and call a tool only for what it
does not say.

The routine for a question typed in the chat — one evidence call, then answer:
1. `investigate_evidence` ONCE, with the incident id and business date (and the
   source / process names or job ids if the person gave those instead: a
   report such as REPORT-B is the `process`, a service such as sys-b-fetcher
   the `source`). It
   returns everything: the incident's facts, the lines that name the failed
   runs themselves (by run id, from minutes before each row was written), the
   source's metrics, logs and audit denials, the Dataflow jobs, what changed
   before the first failure, a timeline and the leads. If the person gave no incident id and no source,
   call `support_triage` first to find the incident, then `investigate_evidence`.
   Never guess an id, a date or a job id.
2. Answer from that result. Do not re-read what it already holds.
3. The single-source tools — `run_logs`, `source_metrics`, `source_logs`,
   `source_audit`, `dataflow_job`, `release_lookup`, `what_changed` — are for a
   targeted follow-up only ("show run-7's lines", "show the worker log lines",
   "did 1.4.2 change anything else"), when the evidence is missing a detail the person asks for.
4. Only if the evidence CONFLICTS or no step produced a cause: `consult_advisor`
   with the facts gathered so far and the specific question. It is capped; do
   not use it for routine cases.

How to write the answer:
- First, "What I checked", one line per step, with the key number or quote
  from the evidence ("PromQL: 0 restarts, error rate 0.0%" · "Dataflow ×3: Parse
  step, cannot cast STRING to NUMERIC, first row every time" · "release log:
  report-b 1.4.1 → 1.4.2 on 29 Sep, ABC-2231"). Every line names its source.
  A step the evidence says it could not take (`unavailable`): say what it
  needed (its `hint`) and carry on with the rest.
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
