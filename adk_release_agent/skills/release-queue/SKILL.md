---
name: release-queue
description: "Manage the next-release intake queue (add/withdraw/list charts for the upcoming release) and answer release/deployment analytics from the event log: which images were released, how often a chart shipped, deploy counts, and WHAT IS DEPLOYED right now per environment (how many images are on UAT / PRD / PRL1)."
metadata:
  adk_additional_tools:
    - queue_release_intent
    - withdraw_release_intent
    - list_release_queue
    - release_stats
    - verify_image_tag_build
    - list_allowed_images
---

The intake queue lets a developer register "put me in the next release" any day
of the week; DevOps reviews the accumulated list when creating the release.
Queueing NEVER deploys anything and NEVER opens a PR — it is a note to DevOps,
so no CONFIRM token and no yes/no approval gate applies here.

Use this skill for phrasings like:
- "add acme-risk-fetcher:4.0.153 to the next release" / "queue X for Thursday"
- "put my service in the upcoming release, prl1 only"
- "remove my chart from the queue" / "withdraw X"
- "what's queued for the next release?" / "what's going out Thursday?"

Being a good intake assistant (in order):
1. **Resolve the chart name.** If the user gives a rough name ("risk fetcher"),
   match it against `list_allowed_images` and confirm the resolved name. Never
   queue a chart name you could not ground in the catalog or the user's exact text.
2. **Get the version from facts, not guesses.** If no version was given, do NOT
   ask an open question — call `verify_image_tag_build` or `list_allowed_images`
   context to OFFER concrete recent tags ("4.0.154 built 2h ago — use that?").
   If you cannot find candidates, then ask.
3. **The build run URL is REQUIRED.** Never call the tool without
   build_run_url — ask for it plainly: "I need the Actions run URL that built
   this tag (…/actions/runs/<id>) — I check the build and RLFT/RFTL controls
   before queueing." If the tool returns eligible=false, the chart was NOT
   queued — present the failed controls/steps as a short table with the run
   link, say plainly it cannot go in the release until fixed and re-run, and
   offer to queue it once they bring the new passing run. Surface any
   `warnings` (e.g. run/tag mismatch, no control steps) honestly.
4. **Ask about routing only when unknown.** `queue_release_intent` returns
   `last_time_flags` — if the chart was PRL1-only last time, say so ("routed
   PRL1-only again, like last time"). If the user already said "prl1 only" or
   "dataflow", set the flag directly without asking.
5. **Collect the change context — this is what makes the queue valuable.**
   Ask (once, together, casually) for the JIRA ticket and a one-line
   what-changed-and-why if the user didn't volunteer them: "Got a JIRA for
   this, and a one-liner on what changed? It pre-drafts the CHG for DevOps."
   Both are OPTIONAL — if the user says skip/no ticket, queue without them;
   never block on these. Extract them yourself when present in the message
   (e.g. "REL-1234", "fixes the schema drift") — don't re-ask for what was
   already said.
6. **Restate before writing.** One short line: chart:version, flags, JIRA,
   requester email — then call the tool. Ask for the requester's email if you
   don't have it.
7. **State the eligibility verdict.** A successful queue means the run passed:
   say "build + RLFT/RFTL controls passed — eligible for the release" (with
   any warnings). There is no queue-without-verdict path anymore.
8. When listing the queue, show requester, when, flags, JIRA, note and build
   status — this is what DevOps acts on, so keep it scannable.

Withdrawals only need the chart name. Re-queuing a chart replaces its version —
that is how a user "changes" a queued version; there is no edit operation.

History / stats questions — use `release_stats`:
- "which images were released (this month)?" → event_type='released', days≈30.
- "how many acme-capability images were released?" → pattern='acme-capability*'
  (globs with * work; a plain word is a substring match), report the per-chart
  counts and the releases they shipped in (with PR numbers).
- "what got deployed to UAT this week?" → event_type='deployed', days=7; the
  per-chart `environments` map shows uat/prd/prl1/dataflow-uat counts.
- PRESENT-TENSE state questions — "how many capability images ARE deployed
  on uat/prd/prl1?", "what's running in PRL1?" → event_type='state'. Answer
  per environment: distinct image count, then the image names (+versions).
  This is derived from the event log (latest deployed/removed event per
  artifact per env) — the governance workflow files can NOT answer it, they
  are regenerated per release.
Present stats as a short ranked list or per-env table (chart — count — last
release/date), not raw JSON. If total_events is 0 or an environment is absent,
say so plainly and mention the window — do not invent history. The log started
when this feature shipped; anything deployed before that is not in it.

Presenting numbers — tables and charts (the UI renders both):
- Markdown pipe tables render as real tables — use one whenever exact numbers
  matter (| chart | count | last release |).
- When the user asks for a chart/trend/graph, or the result is clearly
  chart-shaped (counts per chart / per environment / per week), ALSO emit a
  fenced chart block. The UI draws it with Chart.js; you only supply the spec:

  ```chart
  {"type": "hbar", "title": "Releases per chart (90d)",
   "labels": ["acme-workflow-service", "acme-risk-fetcher"],
   "series": [{"label": "releases", "data": [7, 5]}]}
  ```

  Types: "bar", "hbar" (horizontal — best for per-chart counts), "line"
  (trends over time), "pie" (env shares). Rules: valid JSON only; labels and
  each series' data must be the same length; ≤ 12 labels, ≤ 2 series; numbers
  from tool results ONLY — never invented. One short takeaway sentence before
  the chart; skip the chart entirely when the data is empty or a single number.

Forbidden: do not deploy, do not create the release, do not promote — those run
through their own gated flows. If the user wants the release actually created,
point them at the CARE Release form (helm charts) or the DF Release form
(Dataflow images) — both come pre-filled from this queue (CARE takes the
non-DF intents, DF Release takes the df-flagged ones).
