# Alert noise reduction — end-to-end design

Status: **proposed** (2026-09-19). Not built. Read AGENTS.md first; this follows
the Monitoring pill's pattern (facts from tools, the model narrates and
proposes, nothing applies itself).

## 1. Goal

**Look at the alerting as it is in the console: the policies that exist and
the incidents they actually raised over 90 days. Find the noise, suggest
changes to the alert conditions, and test each suggestion against the real
metrics before showing it.**

The portal **writes nothing** — not to GCP, not to any repo. Its output is a
suggestion: the condition change, the evidence for it, and the replay result
showing what the change would have done. Terraform is where the team applies
it; the portal does not read Terraform and does not compare against it.

## 2. Inputs — all from the Cloud Monitoring API

| Input | API | Role |
|---|---|---|
| **Alert policies** as they are live | `alertPolicies.list` | The conditions being analysed — exactly what the console shows |
| **Incidents raised** | `alerts.list` (v3, Preview), last 90 days | Every incident the alerting opened: policy, condition, open/close time, labels, summary. The complete record — no mailbox, no email access |
| **Metrics** | PromQL against Managed Prometheus (the portal already speaks it) | For the replay: what a changed condition *would* have done |

Nothing else. No Terraform read, no drift comparison — the console is the
view, and what it shows is what gets analysed.

### 2a. Scope — one team in a multi-tenant project

The project is shared, so `roles/monitoring.viewer` can see every tenant's
policies and incidents. The tool must **never** hold, show, label or reason
about another team's alerts. Scope is applied at the earliest point the API
allows, and everything outside it is dropped before anything else runs:

| Layer | How it is scoped |
|---|---|
| Policies | **server-side**, in the `alertPolicies.list` `filter` — the API supports `user_labels.<key>`, `display_name`, `name`, `enabled`, `notification_channels`. Out-of-scope policies are never returned |
| Incidents | `alerts.list` has **no filter parameter**, so it is filtered client-side to incidents whose `policy` is one of the in-scope policy names — and the rest are discarded on read, never stored, never logged |
| Metrics | the policy's own PromQL already carries the team's labels; the replay adds nothing |
| Labels (§7b) | only in-scope incident groups are ever offered for labelling |

The scope rules, any of which may be set, combined with OR into one API
filter:

```
ALERT_SCOPE_LABEL:   "team=payments"     -> user_labels.team='payments'
ALERT_SCOPE_PREFIX:  "payments-"         -> display_name=starts_with('payments-')
ALERT_SCOPE_MATCH:   "payments"          -> display_name=has_substring('payments')   (case-insensitive)
ALERT_SCOPE_POLICIES: "a,b,c"            -> name='…/a' OR name='…/b' …            (explicit allow-list)
```

A user label is the cleanest, because it does not depend on naming discipline
and Terraform can set it on every policy in one place (`user_labels`); prefix
and substring exist for policies made before that discipline. **With no scope
key set the feature refuses to run** in a multi-tenant project — analysing
everything is never the default. `ALERT_SCOPE_NAMESPACE` (a namespace named
in the condition's PromQL) remains as a further client-side narrowing.

## 3. Access

One read-only role on the project: **`roles/monitoring.viewer`**. Verified with
`gcloud iam roles describe`: it holds `monitoring.alertPolicies.list/get`,
`monitoring.alerts.list/get` and `monitoring.timeSeries.list`.
`roles/monitoring.alertViewer` is a two-permission subset and is **not needed**.

That is the whole ask. No repo access, no `git`, no `terraform` binary.

## 4. The pipeline

```
1. READ      live policies + 90 days of incidents, joined by policy name    deterministic
2. FIND      noise patterns per policy (§5)                                 deterministic
3. SUGGEST   a structured condition change per noisy policy (§6)           ← model
4. TEST      replay the changed condition over 90 days of metrics (§7);
             a change that hides a real incident is REJECTED                deterministic
5. SHOW      the change + evidence + replay verdict; a person applies it
```

The model is in step 3 only, and step 4 tests it. Step 5 is a report, not a
write.

## 5. Noise findings — what drives a suggestion

`alert_hygiene.findings(policies, incidents)` — pure, tested, no model.
Numbers are config defaults.

| Finding | Rule | What it usually means |
|---|---|---|
| **noisy** | > 5 incidents/week (`ALERT_NOISY_PER_WEEK`) | threshold too tight, or no `for` duration |
| **flapping** | median duration < 5 min (`ALERT_FLAP_MINUTES`), or ≥ 3 reopen-within-60-min pairs | no `for` duration; the metric oscillates around the threshold |
| **short-lived only** | ≥ 20 incidents in the window and none counted as real (§7a) (`ALERT_UNACKED_MIN`) | the alert never describes anything that lasts — delete, demote, or it is a dashboard line |
| **duplicate** | two policies whose open times co-occur within ±5 min for ≥ 80% of the smaller set | same failure, two notifications — merge |
| **scheduled** | ≥ 70% of opens in the same hour-of-day | a batch job, not an incident |

Each finding carries evidence as numbers only (`incidents=47, real=3,
median_minutes=2.4, hour_of_day=02`). The model never sees raw incident
records.

Secondary, reported but not suggestion-driving: **stale** (open > 7 days — a
broken condition), **undocumented** (no runbook text / no notification
channel), **disabled** policies still present, **absent-data** conditions.

## 6. Suggestion catalogue — pattern → condition change

The model's output is **structured** — the policy, the condition, and the
field changes — so the replay can test it and the change can be shown
precisely. Prose comes after. Field names are the alert policy's own (what the
API and the console use); the Terraform attribute is the same word.

| Noise pattern | Suggested change | Condition field |
|---|---|---|
| flapping, no `for` | add / lengthen the hold | `duration` |
| noisy, threshold just above normal | raise the threshold to the metric's p95 plus margin | `thresholdValue`, or the comparison inside the PromQL `query` |
| noisy, spiky metric | aggregate over a longer window before comparing | `aggregations[].alignmentPeriod`, or `rate(...[5m])` → `[15m]` in the PromQL |
| scheduled at a fixed hour | exclude the window, or alert on a symptom the batch does not trigger | a time clause in the PromQL — suggested, never automated |
| short-lived only | disable, or route to a non-paging channel | `enabled`, or `notificationChannels` |
| duplicate | keep one; or combine the conditions | remove one policy, or `combiner = AND` |
| absence-based noise | absence condition → a threshold with a `for` | `conditionAbsent` → `conditionPrometheusQueryLanguage` |
| single threshold on an SLO-ish signal | multi-window burn-rate condition | rewrite the PromQL (fast + slow window) |

The model is given the policy's current condition, the finding, its numbers,
and the metric's p50/p95/max over the window. It returns:

```json
{"policy": "payments-api-5xx",
 "condition": "5xx rate",
 "changes": [{"field": "duration", "from": "0s", "to": "300s"}],
 "reason": "44 of 47 incidents closed within 4 minutes; a 5-minute hold keeps all 3 that lasted longer."}
```

## 7. The test — replay, deterministic

`replay(condition_after, incidents, window)`:

1. Evaluate the **changed** condition over the window as a range query at the
   policy's evaluation interval (`/api/v1/query_range`, or a subquery for
   short windows).
2. Reconstruct episodes: runs where the condition holds for ≥ its `duration`.
3. The gate: **every incident that counts as real (§7a) must overlap a
   replayed episode.** Otherwise the suggestion is rejected and the incident
   it would have hidden is named.
4. Report `before` (actual incidents), `after` (replayed episodes),
   `real_kept: 3/3`, the verdict.

Also exposed directly, because "test them" should not require a model:
**`what_if(policy, changes)`** — the same replay on a change *you* type
("what would a 10-minute hold do to #2?"). The most useful tool in the set,
and it has no LLM in it.

Stated in every verdict, not hidden: replay covers PromQL conditions and
metric-threshold conditions (via their aggregation as a PromQL equivalent) in
v1; absence and MQL suggestions are unverified and labelled so. Replay
defends against over-tightening only — it cannot see incidents that never
fired.

### 7a. What counts as "real" — nobody acknowledges, so:

Incidents are emailed and never acknowledged, so `state` is always
open/closed. "Real" is a ladder, strongest first, and every verdict says
which rung it stands on (`real_kept: 3/3 — 2 labelled, 1 long-lived`):

| Rung | Signal | Source |
|---|---|---|
| **labelled** | a person marked it *real* | the labelling pass (§7b), append-only in BigQuery — in the portal, no email involved |
| **long-lived** | duration ≥ `ALERT_REAL_MINUTES` (30) — it did not self-resolve in a scrape or two | `alerts.list` |

An incident on neither rung is treated as noise for the gate only; the report
still lists it. Weaker than acknowledgement, and said so — the reviewer
always sees what a "keep" rests on.

### 7b. The labelling pass — twenty minutes, once, in the portal

Stage 1 ends with the tool listing the **top 20 incident groups** (policy,
count, median duration, hour-of-day) and asking someone who knows the services
to mark each *real / noise / unsure*, in chat or the pill. Answers go to an
append-only BQ table (`alert_labels`: incident_group, policy, label, who,
when). Nothing else is needed for ground truth.

## 8. Where the model is

Exactly two places:

1. **Step 3, the suggestion** — judgement over the condition, the noise
   pattern and the metric's distribution; returns the structured change in §6.
2. **The narrative** — the report's sentences and the reason line on each
   suggestion.

Not the detector (§5 is), not the judge of what was real (§7a is), not the
applier (there is nothing to apply). A suggestion the replay rejected is shown
as "tried, would have hidden incident #…" — never as a recommendation.

## 9. Output — what a person sees

Per noisy policy, in chat or the pill:

- the finding and its numbers;
- the suggested change, field by field, as the policy has it in the console
  (`duration: 0s → 300s`), plus the same change written as the Terraform
  attribute (`duration = "300s"`) as a convenience — a snippet, not a diff
  against any file;
- the replay verdict — `47 → 6 incidents; real kept 3/3 (2 labelled,
  1 long-lived)`;
- the rejected alternatives and why.

The team applies it in Terraform through its own workflow. The portal never
edits a policy, never opens a PR, never commits.

## 10. Config (helm `values.yaml → config:`)

```
ALERT_HISTORY_DAYS: "90"
ALERT_NOISY_PER_WEEK: "5"
ALERT_FLAP_MINUTES: "5"
ALERT_UNACKED_MIN: "20"
ALERT_REAL_MINUTES: "30"
# --- scope: REQUIRED (at least one) — the project is multi-tenant ---
ALERT_SCOPE_LABEL: ""            # "team=payments"  -> user_labels.team='payments'   (preferred)
ALERT_SCOPE_PREFIX: ""           # display_name=starts_with(...)
ALERT_SCOPE_MATCH: ""            # display_name=has_substring(...), case-insensitive
ALERT_SCOPE_POLICIES: ""         # comma list of policy names — explicit allow-list
ALERT_SCOPE_NAMESPACE: ""        # further narrowing: conditions whose PromQL names this namespace
ALERT_BQ_DATASET: ""             # labels (and an optional incident snapshot); empty = labels in memory only
PREVIEW_FEATURES: "monitoring,alert-noise"
```

At least one scope key is required; with none set the tool refuses to run
rather than analyse every tenant's alerts. `alerts.list` is Preview with quota-set
retention; if the first run shows it short, an append-only incident snapshot
into `ALERT_BQ_DATASET` (same discipline as `release_intents`) is the first
stage-1 task.

## 11. Safety invariants

- Read-only: `roles/monitoring.viewer` and nothing else. No tool has a write
  path to GCP or to any repo.
- The replay gate is not overridable from chat: a rejected suggestion is
  never presented as one.
- The only thing it stores is labels (and optionally incidents), append-only,
  in the team's own dataset — and only for in-scope policies.
- Tenant isolation: policies are filtered in the API call; incidents outside
  the in-scope policy set are dropped on read and never reach the model, the
  report, the labels or the logs. No scope configured = the feature refuses
  to run.

## 12. Rollout — each stage stands alone

| Stage | Delivers | Effort |
|---|---|---|
| **0 — the check** | pull 90 days once as a one-off script, **with the same scope filter**: top five noisy policies with count, incidents/week, median duration, hour-of-day | 1 hour, no code in the repo |
| **1 — findings** | §2 read, §5 findings, §7b labelling; chat + an optional *Alert noise* pill (preview-gated) | 1–2 days |
| **2 — test** | §7 `what_if` — type a change, see what it would have done | +1 day |
| **3 — suggestions** | §6 model suggestions, each replay-tested | +1 day |

Stage 2 before 3 on purpose: the deterministic test is worth more than the
suggestions, and it lets the team try their own ideas first. Stop after
stage 0 if the top five surprise nobody.

## 13. Open questions — answer before stage 1

1. **`alerts.list` retention** — measured on the first run.
2. **Which scope key fits your policies today** — do they carry a `team`
   user label, or is it a naming prefix? (A label is preferred; if it is
   missing, adding `user_labels = { team = "…" }` in Terraform is a one-line
   change per policy and makes scoping exact from then on.)

## 14. File map (when built)

```
src/release_agent/tools/alert_hygiene.py   policies · incidents · findings · replay · what_if   (~300 lines, mirrors monitoring.py)
adk_release_agent/tools.py                 wrappers: alert_report, alert_detail, what_if_alert, suggest_alert_change, label_incidents
adk_release_agent/skills/alert-noise/SKILL.md
src/release_agent/static/core/alerts.js    wording for the report rows (shared with the Backstage port)
tests/test_alert_findings.py               the rules on synthetic incident sets
tests/test_alert_replay.py                 episodes; the gate on each §7a rung
bigquery/alert_labels.schema.json          the labelling pass (append-only)
```

No new dependency.
