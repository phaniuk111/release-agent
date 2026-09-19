# Alert noise reduction — end-to-end design

Status: **proposed** (2026-09-19). Not built. Read AGENTS.md first; this follows
the Monitoring pill's pattern (facts from tools, the model narrates and
proposes, nothing applies itself).

## 1. Goal

**Terraform is the source of truth. Analyse the incidents the alerting
actually generated, find the noise, and propose changes to the alert
conditions — as Terraform PRs, each one verified against history before a
person sees it.**

In: the `google_monitoring_alert_policy` resources, 90 days of incidents, and
the metrics behind them. Out: a PR per noisy policy that changes its
condition, with the evidence and a replay verdict in the body. Nothing here
writes to GCP; `terraform plan` on the PR is the proof, a person merges.

## 2. Inputs and what is authoritative

| Input | Source | Role |
|---|---|---|
| **Alert definitions** | `google_monitoring_alert_policy` resources in the Terraform repo (`ALERT_TF_REPO` / `ALERT_TF_PATH`), parsed with `python-hcl2` | **Authoritative.** Every proposal is a change to one of these |
| **Incidents** | `alerts.list` (Cloud Monitoring API, Preview), 90 days | The evidence — what actually fired and for how long. **Nobody acknowledges today** (notifications go to email), so `state` is only open/closed — see §7a for what stands in for "real" |
| **Metrics** | PromQL against Managed Prometheus (the portal already speaks it) | For the replay: what a changed condition *would* have done |
| Live policies | `alertPolicies.list` | Only to join incidents to Terraform resources (by `display_name`) and to detect drift (§10). Never the thing being edited |

A policy that exists live but not in Terraform is **out of scope for
proposals**: the tool reports it once ("unmanaged — import it first") and
moves on. Editing a console-made policy by hand is the thing this design
exists to stop.

## 3. Access

One read-only role on the project: **`roles/monitoring.viewer`**. Verified with
`gcloud iam roles describe` — it holds `monitoring.alertPolicies.list/get`,
`monitoring.alerts.list/get` and `monitoring.timeSeries.list`.
`roles/monitoring.alertViewer` is a two-permission subset of it and is **not
needed**.

Plus read on the Terraform repo (stage 1) and write for PRs (stage 3), both
through the existing session-PAT / Git Data API path — no new credential, no
`git` binary, no `terraform` binary. The tool reads `.tf` files; the repo's CI
runs `plan`.

## 4. The pipeline

```
1. READ      Terraform resources  ─┐
             90 days of incidents  ─┼→ join on display_name          deterministic
             live policies (drift) ─┘
2. FIND      noise patterns per policy (§5)                          deterministic
3. PROPOSE   a condition change per noisy policy (§6)               ← model
4. REPLAY    the changed condition over 90 days of metrics (§7):
             must keep every incident that counts as real (§7a), else REJECT   deterministic
5. PR        edit the .tf, parse-diff gate, open the PR (§9)         deterministic
```

The model is in step 3 only, and step 4 checks it.

## 5. Noise findings — what drives a proposal

Computed by `alert_hygiene.findings(policies, incidents)` — pure, tested, no
model. Numbers below are config defaults, not code.

| Finding | Rule | What it usually means |
|---|---|---|
| **noisy** | > 5 incidents/week (`ALERT_NOISY_PER_WEEK`) | threshold too tight, or no `for` duration |
| **flapping** | median duration < 5 min (`ALERT_FLAP_MINUTES`), or ≥ 3 reopen-within-60-min pairs | no `for` duration; metric oscillates around the threshold |
| **never reacted to** | ≥ 20 incidents in window and **no reaction signal** (§7a) for any of them (`ALERT_UNACKED_MIN`) | nobody believes it — delete, demote, or it is really a dashboard line |
| **duplicate** | two policies whose open times co-occur within ±5 min for ≥ 80% of the smaller set | same failure, two pages — merge |
| **scheduled** | ≥ 70% of opens in the same hour-of-day | a batch job, not an incident — a mute window or a different condition |

Each finding carries its evidence as numbers only (`incidents=47,
reacted=3, labelled_real=2, median_minutes=2.4, hour_of_day=02`). The model
never sees raw incident records.

Secondary, reported but not proposal-driving: **stale** (open > 7 days — a
broken condition), **undocumented** (no runbook text / no channel),
**absent-data** conditions, and **drift** (§10).

## 6. Proposal catalogue — pattern → condition change → Terraform attribute

This is the part the model does, and the whole point is that its output is
**structured** — a resource address plus attribute changes — so the replay
can consume it and the PR can be gated. Prose comes after.

| Noise pattern | Proposed change | `google_monitoring_alert_policy` attribute |
|---|---|---|
| flapping, no `for` | add / lengthen the hold | `conditions[].condition_*.duration` |
| noisy, threshold just above normal | raise threshold to the p95 of the metric plus margin | `condition_threshold.threshold_value` / the PromQL comparison in `condition_prometheus_query_language.query` |
| noisy, spiky metric | aggregate over a longer window before comparing | `aggregations[].alignment_period`, or `rate(...[5m])` → `[15m]` in the PromQL |
| scheduled at a fixed hour | keep the alert, exclude the window; or switch to a symptom the batch does not trigger | a time-based clause in the PromQL (`hour() != 2`), or a separate policy with `enabled=false` + snooze — reported, not automated |
| never reacted to | disable, or downgrade to a non-paging channel | `enabled = false`, or `notification_channels` |
| duplicate | keep one, delete the other; or combine conditions | remove the resource, or `combiner = "AND"` |
| absence-based noise | `condition_absent` → a threshold on `up`/`absent()` with a `for` | `condition_absent` → `condition_prometheus_query_language` |
| single threshold on an SLO-ish signal | multi-window burn-rate condition | rewrite the PromQL (fast + slow window) |

The model is given: the resource's current HCL block, the finding, its
numbers, and the metric's p50/p95/max over the window. It returns:

```json
{"resource": "google_monitoring_alert_policy.payments_api_5xx",
 "changes": [{"path": "conditions[0].condition_prometheus_query_language.duration",
              "from": "0s", "to": "300s"}],
 "reason": "44 of 47 incidents closed within 4 minutes; a 5-minute hold keeps the 3 that were followed by a rollback."}
```

A value that is a `var.` in the HCL is proposed as a change to the
`.tfvars` entry (`ALERT_TF_VARS_FILE`); the model never inlines it.

## 7. Replay verifier — deterministic

`replay(condition_before, condition_after, incidents, window)`:

1. Evaluate the **changed** condition over the window as a range query at
   the policy's evaluation interval (`/api/v1/query_range`, or a subquery for
   short windows).
2. Reconstruct episodes: runs where the condition holds for ≥ its `duration`.
3. The gate: **every incident that counts as real (§7a) must overlap a
   replayed episode.** If any does not, the proposal is rejected and the
   incident it would have hidden is named.
4. Report `before` (actual incidents), `after` (replayed episodes),
   `real_kept: 3/3`, and the verdict.

What it cannot do, said in its output rather than hidden: replay only
covers `condition_prometheus_query_language` and `condition_threshold`
(via its aggregation as a PromQL equivalent) in v1; `condition_absent` and MQL
are proposed unverified and labelled so. Replay defends against
over-tightening only — it cannot see incidents that never fired.

### 7a. What counts as "real" when nobody acknowledges

Confirmed: incidents are emailed and nobody acknowledges them, so the
`acknowledged` state is always false here. "Real" is therefore a ladder of
signals the tool already has or can get cheaply, strongest first:

| Signal | Source | Strength |
|---|---|---|
| **Labelled real** | a one-off labelling pass (§7b) stored in BQ `alert_labels` | ground truth |
| **Reacted to** | a deploy, rollback, promotion or hotfix PR for that chart within 24 h of the incident — from the portal's own release event log (`release_intents`) and the deploy repo's PRs | strong: something changed after it fired |
| **Long-lived** | duration ≥ `ALERT_REAL_MINUTES` (30) — it did not self-resolve in a scrape or two | weak but honest |

An incident with none of these is treated as noise **for the purpose of the
gate only**; the report still lists it. The gate keeps every labelled-real
and every reacted-to incident, and every long-lived one unless the reviewer
overrides that class explicitly in the PR — that override is visible, never
silent.

This is weaker than acknowledgement and the design says so in every replay
verdict (`real_kept: 3/3 (2 labelled, 1 reacted)`), so a reviewer knows what
the guarantee rests on.

### 7b. The labelling pass — twenty minutes, once

Stage 1 ends with the tool presenting the **top 20 incident groups** (by
policy, with duration, hour-of-day and any reaction) and asking the team to
mark each *real / noise / unsure*. Answers go to an append-only BQ table
(`alert_labels`: incident_id, policy, label, who, when). Twenty minutes of a
person who knows the services turns the gate from heuristic to labelled for
exactly the policies that matter, which are the noisy ones.

Going forward, the cheapest way to keep labels flowing is a *real / noise*
link in the notification itself — the email already goes out; the portal can
host the two-button page. Not required for v1; the labelling pass is enough.

## 8. Where the model is

Exactly two places:

1. **Step 3, the proposal** — judgement over the HCL, the noise pattern and
   the metric's distribution. Returns the structured change in §6.
2. **The PR text** — one paragraph a reviewer reads: what changes, what it
   keeps, what it drops.

Not the detector (§5 is), not the judge of what was real (`acknowledged` is),
not the applier (no code path exists). A proposal the replay rejected is
never shown as a recommendation — it is shown as "tried, would have hidden
incident #…", so the reviewer sees what was ruled out and why.

## 9. Output — the PR

One PR per policy (or one per batch, `ALERT_PR_BATCH`), against
`ALERT_TF_REPO`:

- the `.tf` edit, applied as a targeted text change and then **verified
  structurally**: parse before and after with `python-hcl2`, assert that
  exactly the named attributes of exactly the named resource changed, and
  discard the edit otherwise (`python-hcl2` does not write HCL back, so the
  parse-diff is what makes the text edit safe);
- body: the finding and numbers, the replay verdict, links to the
  acknowledged incidents it keeps, the proposals that were rejected and why;
- commits through the Git Data API; the repo's CI runs `terraform plan`; a
  person merges.

Never a direct `apply`, never a console edit, never a change to a resource
the tool did not name.

## 10. Drift — a stop condition, not just a finding

Incidents were generated by the **live** condition. If the live policy differs
from its Terraform (someone edited a threshold in the console; a resource
never applied), then a proposal against the Terraform would be justified by
evidence from a different condition. So:

- **live == Terraform** → proposals allowed;
- **live ≠ Terraform** → no proposal for that policy; report the drift
  ("console threshold 90, Terraform says 80 — apply or import first");
- **live, no Terraform** → "unmanaged — import it first".

Drift needs no model and no incidents — Terraform plus `alertPolicies.list`.
It ships in stage 1 and is the finding an auditor asks about anyway.

## 11. Config (helm `values.yaml → config:`)

```
ALERT_TF_REPO: ""                # owner/repo; empty = feature off
ALERT_TF_PATH: "monitoring/alerts/"
ALERT_TF_REF: "main"
ALERT_TF_VARS_FILE: ""           # for thresholds that are var.s
ALERT_HISTORY_DAYS: "90"
ALERT_NOISY_PER_WEEK: "5"
ALERT_FLAP_MINUTES: "5"
ALERT_UNACKED_MIN: "20"
ALERT_REAL_MINUTES: "30"         # an incident this long counts as real for the gate
ALERT_REACTION_HOURS: "24"       # a deploy/rollback/PR within this window = reacted to
ALERT_PR_BATCH: "false"          # one PR per policy by default
ALERT_BQ_DATASET: ""             # optional incident snapshot (append-only); empty = off
PREVIEW_FEATURES: "monitoring,alert-noise"
```

The BigQuery snapshot is optional and exists only because `alerts.list` is
Preview with quota-set retention: a weekly append of new incidents gives
history the API may not. Same append-only discipline as `release_intents`.

## 12. Safety invariants

- GCP is read-only: `roles/monitoring.viewer` and nothing else; no tool has
  a GCP write path.
- The only write is a PR to GitHub, through the existing session-PAT path.
- The replay gate is not overridable from chat: a rejected proposal cannot
  become a PR.
- The parse-diff gate discards any `.tf` edit touching more than the named
  attributes of the named resource, before a commit exists.
- Drift blocks proposals for that policy (§10).
- The tool never runs `terraform`.

## 13. Rollout — each stage stands alone

| Stage | Delivers | Effort |
|---|---|---|
| **0 — the check** | pull 90 days once, by hand; top five noisy policies with count, median duration, hour-of-day and whether anything was deployed after — and the drift list | 1 hour, no code |
| **1 — findings** | §2 read, §5 findings, §10 drift, the reaction signal from the release log, the §7b labelling pass; chat + an optional *Alert noise* pill (preview-gated) | 2–3 days |
| **2 — proposals + replay** | §6 + §7 | +1 day |
| **3 — PRs** | §9 | +1 day |

Stop after stage 0 if the top five surprise nobody.

## 14. Open questions — answer before stage 1

1. ~~Do people acknowledge incidents?~~ **Answered: no — email notifications,
   nobody acknowledges.** The gate uses the §7a ladder and the §7b labelling
   pass instead. Open follow-up: which mailbox/list receives them, and is a
   *real / noise* link in that email acceptable?
2. **How many condition values are `var.`s** rather than literals in the HCL?
   Each one needs the `.tfvars` path to be proposable.
3. **Is any alert routed through PagerDuty?** Its analytics already give
   finding 1–3; the value here would then be the replay and the PR.
4. **`alerts.list` retention** — measured on the first run; if short, the BQ
   snapshot moves from optional to stage 1.

## 15. File map (when built)

```
src/release_agent/tools/tf_alerts.py       read google_monitoring_alert_policy from HCL (python-hcl2); parse-diff gate
src/release_agent/tools/alert_hygiene.py   incidents · findings · replay                (~300 lines, mirrors monitoring.py)
adk_release_agent/tools.py                 wrappers: alert_report, alert_detail, propose_alert_change, replay_alert_change, draft_alert_pr
adk_release_agent/skills/alert-noise/SKILL.md
tests/test_alert_findings.py               the rules on synthetic incident sets
tests/test_alert_replay.py                 episodes; the acknowledged gate
tests/test_tf_alerts.py                    HCL parsing; the parse-diff gate on real resource blocks
pyproject.toml                             + python-hcl2 (pure Python)
```
