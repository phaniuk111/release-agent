# Alert hygiene — end-to-end design

Status: **proposed** (2026-09-19). Not built. Read AGENTS.md first; this follows
the Monitoring pill's pattern (facts from tools, the model narrates and
proposes, nothing applies itself).

## 1. What it is, in one paragraph

A read-only analysis of the team's Cloud Monitoring alerting: which policies
fire most, which flap, which nobody ever acts on, which are duplicated,
undocumented or scheduled noise — computed deterministically from 90 days of
incident history. For the noisy ones the model proposes a concrete rewrite
(threshold, `for` duration, absence handling, burn-rate condition), and a
**replay** re-runs the proposal over the real metrics and rejects any change
that would have hidden an incident a human acknowledged. Surviving proposals
become a PR against the **Terraform that defines the policies**. A person
merges; `terraform plan` on the PR is the final proof. Nothing here writes to
GCP.

## 2. Why this and not a metrics memo

A memo has no action at the end. This has one per finding, and it has ground
truth: an **acknowledged** incident is "this was real"; one that auto-closed
untouched is "this was noise". That labelled signal is what lets an agentic
loop check its own work instead of guessing.

## 3. Scope

Only what the team owns. With Terraform available the cleanest rule comes
first: **a policy is ours if it is defined under our Terraform path**
(`ALERT_TF_PATH`, e.g. `monitoring/alerts/`). The live policy is matched to its
resource by `display_name` (Terraform sets it) and confirmed by the
`user_labels` Terraform applies.

Fallbacks, OR'd, for anything not (yet) in Terraform:

- a condition's query names the owned namespace (`namespace="<ours>"` /
  `namespace_name="<ours>"`),
- a user label `team=<ours>` (configurable key),
- a display-name prefix (the portal writes `Dev Portal: …`).

Incidents inherit scope from their policy; a policy outside scope is invisible,
not "someone else's noise" in our report. Config: `ALERT_TF_REPO`,
`ALERT_TF_PATH`, `ALERT_SCOPE_NAMESPACE`, `ALERT_SCOPE_LABEL`,
`ALERT_SCOPE_PREFIX`.

## 4. Access — what the portal's service account needs

| Data | API | Permission | Role |
|---|---|---|---|
| Policy definitions | `projects.alertPolicies.list/get` | `monitoring.alertPolicies.list/get` | `roles/monitoring.viewer` |
| Incident history | `projects.alerts.list/get` (**Preview**) | `monitoring.alerts.list/get` | `roles/monitoring.viewer` |
| Metrics for replay | PromQL (`monitoring.googleapis.com`) | `monitoring.timeSeries.list` | `roles/monitoring.viewer` |

**`roles/monitoring.alertViewer` is NOT required.** Verified with
`gcloud iam roles describe`: it holds exactly `monitoring.alerts.get` and
`monitoring.alerts.list`, both of which `roles/monitoring.viewer` already
includes (35 permissions, GA). One role, read-only, is the whole ask.

Plus **read** access to the Terraform repo (stage 1: definitions and drift)
and **write** access to it for stage 3 PRs — both through the existing session
PAT / Git Data API path, so no new credential and no `git` binary. No
Terraform binary in the image either: the tool reads `.tf` files; it never runs
`terraform`. The PR's own CI runs `plan`.

## 5. Data

### 5.1 Policies — `alertPolicies.list`, normalised

```
name, display_name, enabled, snoozed, user_labels,
conditions[]: {name, kind: promql|threshold|absence|mql, query, threshold,
               comparison, duration, evaluation_interval},
documentation: bool (any runbook text), notification_channels: int,
created, last_modified
```

`kind` matters later: only `promql` conditions can be replayed in v1 (that is
what the portal itself writes via `monitoring.alert_policy`). Others are
analysed from incidents only and say so.

### 5.1a Terraform — the source of truth for definitions

`google_monitoring_alert_policy` resources under `ALERT_TF_PATH`, fetched from
`ALERT_TF_REPO` through the GitHub contents API and parsed with `python-hcl2`
(pure Python; parses HCL2 to dicts — no regex, no Terraform binary). Per
resource: `display_name`, `combiner`, `conditions[]` (the
`condition_prometheus_query_language` / `condition_threshold` /
`condition_absent` blocks — `query`, `threshold_value`, `comparison`,
`duration`, `evaluation_interval`), `documentation.content`, `user_labels`,
`notification_channels`, `enabled`, plus the file and resource address for the
PR later. Variables and locals are resolved only when literal; a value that
comes from a `var.` is reported as such, not guessed.

Live API (§5.1) and Terraform are then **joined on `display_name`**, which
yields finding 9 (drift) for free — and means a rewrite is always proposed
against the file that actually produces the policy, not against a live object
someone may have edited in the console.

### 5.2 Incidents — `alerts.list`, normalised

```
policy_name, condition_name, open_time, close_time (null if open),
state: open|acknowledged|closed, acknowledged: bool, resource_labels,
metric_labels, summary
```

`alerts.list` is Preview and its retention is set by alerting quotas, not
promised. So the first thing the tool does each run is **snapshot** new
incidents into BigQuery — append-only, same discipline as `release_intents`:

```
alert_incidents (BQ, append-only, INSERT only)
  incident_id STRING, policy_name, condition_name, open_time TIMESTAMP,
  close_time TIMESTAMP, state STRING, acknowledged BOOL, labels JSON,
  summary STRING, captured_at TIMESTAMP
```

Latest row per `incident_id` wins (an open incident is re-captured when it
closes). Dataset: the team's own (`ALERT_BQ_DATASET`, default = `BQ_DATASET`).
Empty dataset = snapshot disabled, analysis still runs on the live API.

### 5.3 GMP managed rules (if any)

Prometheus-style rules do not create Cloud Monitoring incidents. Their history
is the synthetic series `ALERTS{alertstate="firing"}` — read through the same
PromQL path. Episodes are reconstructed from contiguous `1` samples. Reported
alongside, flagged `source: gmp`.

## 6. Findings engine — pure, tested, no model

`tools/alert_hygiene.py::findings(policies, incidents, now)` → list of
`{kind, policy, evidence, severity}`. Defaults are config, not code.

| # | Finding | Rule (default) |
|---|---|---|
| 1 | **noisy** | > `ALERT_NOISY_PER_WEEK` (5) incidents/week over the window |
| 2 | **flapping** | median incident duration < `ALERT_FLAP_MINUTES` (5), or ≥ 3 reopen-within-60-min pairs |
| 3 | **never acted on** | ≥ `ALERT_UNACKED_MIN` (20) incidents in window, `acknowledged` = 0 |
| 4 | **stale** | an incident open > 7 days (past auto-close) — a broken condition, not an outage |
| 5 | **duplicate** | two policies whose open times co-occur within ±5 min for ≥ 80% of the smaller set |
| 6 | **undocumented** | no `documentation`, or 0 notification channels, or `enabled=false` for > 30 days |
| 7 | **scheduled** | ≥ 70% of opens fall in the same hour-of-day (batch job, not an incident) |
| 8 | **absent data** | condition is `absence`, or a PromQL condition whose `needs` returned nothing in the window (same rule as MONITOR_CHECKS: "nothing to measure" is never "ok") |
| 9 | **drift** | live policy with no Terraform resource (console-made, unmanaged); Terraform resource with no live policy (never applied); or the two differ in threshold / duration / query / enabled (edited in the console, will be reverted by the next `apply`) |

Finding 9 is the one a bank auditor asks about, and it needs no model and no
incidents — Terraform plus `alertPolicies.list` is enough. It ships in stage 1.

Each finding carries its evidence as numbers (`incidents=47, acknowledged=3,
median_minutes=2.4`) — the model is given these, never the raw records.

## 7. Replay verifier — deterministic

For a `promql` condition, `replay(condition, proposal, window)`:

1. Build the proposed condition: `query` with the new threshold / comparison,
   and the new `duration`.
2. Evaluate it over the window as a range query at the policy's
   `evaluation_interval` (the portal already speaks PromQL to GMP; range
   queries are `/api/v1/query_range`, or a subquery when the window is short).
3. Reconstruct **episodes**: runs where the condition holds for ≥ `duration`.
4. Compare: `episodes_before` (actual incidents), `episodes_after`
   (replayed), and — the gate — every **acknowledged** incident must overlap a
   replayed episode. If any does not, the proposal is **rejected** with the
   incident it would have hidden.

Output: `{would_fire: 6, before: 47, acknowledged_kept: 3/3, verdict: accept}`.

Limits, stated in the output rather than hidden: threshold/MQL/absence
conditions are not replayed in v1 (the proposal is offered unverified and
labelled so); replay cannot know about incidents that never fired because a
threshold was too *loose* — it only defends against over-tightening.

## 8. Where the model is — exactly two places

1. **Proposing the rewrite** (`propose_alert_rewrite`): given one policy's
   condition, its findings and their numbers, write the concrete change and one
   sentence of reasoning. Kinds of change it may propose: threshold, `for`
   duration, evaluation interval, absence → threshold, threshold → SLO
   burn-rate, delete/disable, merge with a duplicate. It returns a **structured
   proposal**, not prose — the replay consumes it.
2. **Explaining** — the report narrative and the PR description.

It is never the detector (§6 is), never the judge of what was real
(`acknowledged` is), and never the applier (there is no code path).

Skill `skills/alert-hygiene/SKILL.md` rules, non-negotiable:
- every number comes from the tool payload; no estimated savings;
- three to five items, biggest first, each ending in an action;
- a proposal the replay rejected is never shown as a recommendation — it is
  shown as "tried, would have hidden incident #…";
- a quiet window reads as "nothing significant", not manufactured findings.

## 9. Tools, API, UI

ADK tools (thin wrappers, `features`-gated like `monitoring_checks`):

| Tool | Deterministic? | Returns |
|---|---|---|
| `alert_report(days=90)` | yes | findings + per-policy evidence, cached like `/api/monitoring` |
| `alert_detail(policy)` | yes | the policy, its incidents timeline, its findings |
| `propose_alert_rewrite(policy)` | **model** | structured proposal |
| `replay_alert(policy, proposal)` | yes | the §7 verdict |
| `draft_alert_pr(policy, proposal)` | yes (text by model) | PR url |

HTTP: `GET /api/alerts/report`, `GET /api/alerts/{policy}`; the rest through
chat. Pill: **Alert report** in the *Check* group, `PREVIEW_FEATURES`-gated so
it can run against real data with two people before anyone else sees it.
Chat follow-ups: "why does #2 flap", "what would 85% do to #1", "draft the PR
for #1".

Weekly (optional, stage 1+): a `CronJob` in the Helm chart calling the same
function — snapshots incidents to BQ and posts the top five to a Teams/email
channel. Reuses the chart; no new auth story.

## 10. PR drafting (stage 3) — against the Terraform

The proposal (§8) names a resource address and the attributes to change
(`threshold_value`, `duration`, `evaluation_interval`, `query`, `enabled`).
The edit is applied to the `.tf` file and then **verified structurally**:
parse the file before and after with `python-hcl2` and assert that exactly
the intended attributes of exactly that resource changed — anything else in
the diff rejects the edit. (`python-hcl2` does not write HCL back, so the text
edit is targeted; the parse-diff is what makes it safe.)

The PR body carries: the finding and its numbers, the replay verdict
(`47 → 6 incidents, 3/3 acknowledged kept`), the incidents it keeps, and a
link to each. The repo's own CI runs `terraform plan`; the plan is the final
proof and it is a person who merges. If a policy is unmanaged (finding 9) the
tool does not invent a resource for it — it reports that first, because
"import it into Terraform" is the actual fix.

For policies not in Terraform at all (console-made), the tool prints the
changed policy JSON and the exact `gcloud` command instead. Still nothing
applied.

## 11. Config (helm `values.yaml → config:`)

```
ALERT_SCOPE_NAMESPACE: ""        # [FEATURE] empty = feature off
ALERT_SCOPE_LABEL: "team="
ALERT_SCOPE_PREFIX: "Dev Portal: "
ALERT_HISTORY_DAYS: "90"
ALERT_NOISY_PER_WEEK: "5"
ALERT_FLAP_MINUTES: "5"
ALERT_UNACKED_MIN: "20"
ALERT_BQ_DATASET: ""             # default BQ_DATASET; empty = no snapshot
ALERT_TF_REPO: ""                # owner/repo holding the Terraform; empty = no drift check, no PRs
ALERT_TF_PATH: "monitoring/alerts/"
ALERT_TF_REF: "main"
PREVIEW_FEATURES: "monitoring,alert-hygiene"
```

## 12. Safety invariants (additions to AGENTS.md when built)

- Read-only against GCP: the service account gets `roles/monitoring.viewer`
  and nothing else; no tool has a GCP write path, so `MutationGuardPlugin`
  has nothing new to block.
- The only write is a PR to GitHub, through the existing session-PAT path.
- `alert_incidents` is append-only, like `release_intents`; empty dataset
  disables it cleanly.
- The replay gate is not overridable from chat: a rejected proposal cannot be
  turned into a PR.
- A Terraform edit that changes anything beyond the named attributes of the
  named resource is discarded before a commit exists (parse-diff gate).
- The tool never runs `terraform`; `plan`/`apply` stay with the repo's CI and
  its reviewers.

## 13. Rollout — each stage stands alone

| Stage | Delivers | Effort |
|---|---|---|
| 0 — the check | pull 90 days once, by hand; show the top five with acknowledged-vs-ignored counts | 1 hour, no code |
| 1 — report | §5 (incl. Terraform read + drift) + §6 + pill + skill (no proposals) | 2 days |
| 2 — verifier | §7 + `propose_alert_rewrite` | +1 day |
| 3 — PRs | §10 | +1 day |

Stop after stage 0 if nobody is surprised.

## 14. Open questions (answer before stage 1)

1. **Do people acknowledge incidents?** If not, finding 3 and the §7 gate lose
   their ground truth; the verifier degrades to duration/flapping heuristics.
   Check the last 90 days' `acknowledged` count first.
2. **How much of the alerting is in Terraform, and how many values are
   `var.`s?** Everything in Terraform gets drift detection and PRs; anything
   console-made gets commands instead. A threshold that is a variable is
   proposed as a change to the `.tfvars`, which needs that file's path too.
3. **Do any alerts route through PagerDuty?** Its analytics already cover
   findings 1–3; the value here would then be 7, 8 and the verifier.
4. **`alerts.list` retention** — measured on the first run; if short, the BQ
   snapshot (§5.2) is stage 1's first task, not an option.

## 15. File map (when built)

```
src/release_agent/tools/alert_hygiene.py   policies · incidents · findings · replay   (~300 lines, mirrors monitoring.py)
src/release_agent/tools/release_queue.py   pattern for the BQ snapshot writer
adk_release_agent/tools.py                 five wrappers
adk_release_agent/skills/alert-hygiene/SKILL.md
src/release_agent/static/palette.js        one pill, Check group
src/release_agent/static/core/alerts.js    wording for the report rows (shared with the Backstage port)
tests/test_alert_findings.py               the eight rules on synthetic incident sets
tests/test_alert_replay.py                 episodes, the acknowledged gate
bigquery/alert_incidents.schema.json       the snapshot table
src/release_agent/tools/tf_alerts.py       read google_monitoring_alert_policy from HCL (python-hcl2), parse-diff gate
pyproject.toml                             + python-hcl2 (pure Python)
```
