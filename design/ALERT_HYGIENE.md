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
(Optional, only if log-match alerting policies should be *replayed* rather
than just counted: `roles/logging.viewer` — §5a.)

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

**The primary question is: which policies fire repeatedly and auto-close on
their own?** That combination — frequent *and* self-resolving — is the
definition of noise here, and the report leads with it. Everything else in
this section is secondary detail.

Auto-close is read per policy (`alertStrategy.autoClose`, default 7 days) and
used two ways:

- an incident that closed **well before** the auto-close duration closed
  because the condition cleared — the metric recovered on its own;
- an incident that closed **at** the auto-close duration closed because data
  stopped arriving, not because anything recovered — that is an absent-data
  or dead-target case, reported separately, never counted as "self-healed".

Ranking: **noise score = incidents per week × share that self-closed within
`ALERT_FLAP_MINUTES`**. Top of the list is a policy that fires often and
clears itself almost every time — the one to fix first.

`alert_hygiene.findings(policies, incidents)` — pure, tested, no model.
Numbers are config defaults.

| Finding | Rule | What it usually means |
|---|---|---|
| **repetitive** | > 5 incidents/week (`ALERT_NOISY_PER_WEEK`), or the same policy reopening within 60 min ≥ 3 times | threshold too tight, or no `for` duration |
| **self-closing** | median duration < 5 min (`ALERT_FLAP_MINUTES`) and closed by the condition clearing (not by auto-close timeout) | the metric oscillates around the threshold — a hold or a wider aggregation window fixes it |
| **short-lived only** | ≥ 20 incidents in the window and none counted as real (§7a) (`ALERT_UNACKED_MIN`) | the alert never describes anything that lasts — delete, demote, or it is a dashboard line |
| **duplicate** | two policies whose open times co-occur within ±5 min for ≥ 80% of the smaller set | same failure, two notifications — merge |
| **scheduled** | ≥ 70% of opens in the same hour-of-day | a batch job, not an incident |

Each finding carries evidence as numbers only (`incidents=47,
per_week=3.8, self_closed_pct=94, median_minutes=2.4, real=3,
hour_of_day=02`). The model never sees raw incident
records.

Secondary, reported but not suggestion-driving: **stale** (open > 7 days — a
broken condition), **undocumented** (no runbook text / no notification
channel), **disabled** policies still present, **absent-data** conditions.

### 5a. Log-based metrics — where most of the noise usually is

The team alerts largely on log-based metrics (`logging.googleapis.com/user/…`),
one set per service. Two consequences the tool is built around.

**They produce the classic noise shapes.** A counter over error log lines is
sparse and spiky, so the same four mistakes appear again and again — and each
has a specific fix the catalogue (§6) already carries:

| Pattern on a log-based metric | Why it fires | Suggested change |
|---|---|---|
| threshold `> 0` on an error-line counter | one log line, one incident | a real threshold (rate or count over a window), plus a `duration` |
| alignment period 60 s on a sparse metric | a single burst crosses it | longer `alignmentPeriod`, `rate` over 5–15 min |
| metric-absence condition on a log metric | the service was merely quiet (no traffic at night) | threshold on `up`/request rate instead, or a time window |
| per-line matching on a message that is retried | every retry is a new line | count distinct correlation ids, or alert on the outcome metric |

**They are templated per service, so noise is repeated N times.** Fourteen
services with the same `error_count > 0` policy produce fourteen noisy
policies from one decision. The tool therefore adds a finding:

| Finding | Rule | What it means |
|---|---|---|
| **templated** | ≥ 3 policies whose conditions are identical except for a service/label value | one condition shape, copied — fix it once in the template |
| **co-firing across services** | templated policies opening together (≥ 80% co-occurrence) | a shared dependency, not fourteen incidents — one alert on the dependency replaces them |

The report **collapses a template into one row** ("`error_count > 0` × 14
services — 312 incidents/90d, 91% self-closed") with one suggestion, tested
by replaying it across all fourteen. A per-service list would hide the fact
that it is one problem.

**Replay works for them.** Managed Prometheus exposes Cloud Monitoring
metrics to PromQL as `logging_googleapis_com:user_<name>`, the same way the
portal's existing checks already read `composer_googleapis_com:…`. Two rules
the existing monitoring code already learned apply here: log-based counters
are DELTA metrics, so the replay must use windowed expressions (`increase`,
`sum_over_time`) never instant selectors, and a window with no log lines is
"no data", never zero.

**Log-match alerting policies** (`conditionMatchedLog`, no metric behind
them) are a different thing: they do not auto-close on the condition
clearing, and replaying one means re-running its log filter over the
period. Reported for repetition and volume in v1; replay for them is
optional and needs `roles/logging.viewer` (`ALERT_LOG_REPLAY`), off by
default.

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

### 7a. What counts as "real" — measured, never judged

Nobody acknowledges incidents and nobody is in a position to label them, so
"real" is defined by **objective signals only** — things the API and the
metrics can measure. No person, and no model, decides per incident. An
incident counts as real if **any** of these holds (all thresholds are config):

| Signal | Rule (default) | Why it means "probably real" |
|---|---|---|
| **long-lived** | duration ≥ `ALERT_REAL_MINUTES` (30) | it did not self-resolve within a scrape or two |
| **large** | the metric exceeded the threshold by ≥ `ALERT_REAL_MAGNITUDE` (2×) at its peak during the incident | a blip crosses a line by a hair; a failure blows through it |
| **co-firing** | ≥ `ALERT_REAL_COFIRE` (2) other in-scope policies opened within ±10 min | real failures trip several symptoms; noise trips one |
| **not self-healing** | the metric did not return under the threshold on its own within `ALERT_FLAP_MINUTES` — i.e. it stayed bad until something changed | a spike that recovers alone is the classic false positive |

Everything else is treated as noise **for the gate only** — the report still
lists it, and every replay verdict says which signals each kept incident
rests on (`real_kept: 3/3 — 2 long-lived, 1 large+co-firing`).

This is deliberately conservative in the direction that matters: an incident
only has to satisfy **one** signal to be protected, so a suggestion is
rejected the moment it would hide anything that lasted, or was big, or came
with company. The cost is that fewer suggestions survive than a labelled
gate would allow. That is the right trade when no one can vouch for the
labels.

The thresholds themselves are the only judgement in the system, and they are
config — set once, visible in `values.yaml`, the same for every policy.

### 7b. Labelling — optional, later, if someone ever can

If a service owner is ever able to review, the tool can show the top
incident groups and record *real / noise* into an append-only BQ table
(`alert_labels`); a label then becomes a fifth, strongest signal. Not
required, not part of any stage, and nothing waits on it.

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
ALERT_REAL_MINUTES: "30"           # duration that counts as real on its own
ALERT_REAL_MAGNITUDE: "2"          # peak / threshold ratio that counts as real
ALERT_REAL_COFIRE: "2"             # other in-scope policies opening within ±10 min
ALERT_LOG_REPLAY: "false"          # replay conditionMatchedLog policies too — needs roles/logging.viewer
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
- No human judgement in the loop: "real" is four measured signals with
  config thresholds (§7a); the only thing it may store is an optional incident
  snapshot, append-only, in the team's own dataset, in-scope policies only.
- Tenant isolation: policies are filtered in the API call; incidents outside
  the in-scope policy set are dropped on read and never reach the model, the
  report, the labels or the logs. No scope configured = the feature refuses
  to run.

## 12. Rollout — each stage stands alone

| Stage | Delivers | Effort |
|---|---|---|
| **0 — the check** | pull 90 days once as a one-off script, **with the same scope filter**: top five noisy policies with count, incidents/week, median duration, hour-of-day | 1 hour, no code in the repo |
| **1 — findings** | §2 read, §5 findings, the §7a signals; chat + an optional *Alert noise* pill (preview-gated) | 1–2 days |
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
adk_release_agent/tools.py                 wrappers: alert_report, alert_detail, what_if_alert, suggest_alert_change
adk_release_agent/skills/alert-noise/SKILL.md
src/release_agent/static/core/alerts.js    wording for the report rows (shared with the Backstage port)
tests/test_alert_findings.py               the rules on synthetic incident sets
tests/test_alert_replay.py                 episodes; the gate on each §7a rung
bigquery/alert_incidents.schema.json       optional incident snapshot (append-only)
```

No new dependency.

## 15. Research addendum — 2026-09-20

The question: 10–15 alerts a day, mostly log-based — can an agent classify
them, help tune them, and read the actual logs? Facts verified this session
(`gcloud iam roles describe`, live API calls, current docs):

- **Roles.** `roles/monitoring.viewer` carries `monitoring.alerts.list/get`,
  `alertPolicies.list/get`, `timeSeries.list`, `metricDescriptors.list`.
  Reading the log lines behind an incident needs `roles/logging.viewer`
  (`logging.logEntries.list`) — read-only, and the only addition to §3.
- **`projects.alerts.list` (v3)** takes `filter`, `orderBy` (`openTime` /
  `closeTime`), `pageSize` ≤ 1000 and a page token valid 72 h; reachable with
  ADC. History retention is still unstated — measure it on the first run (§13).
- **Two different "log-based" alerts, two different fixes.**
  - A policy on a **log-based metric** (`logging.googleapis.com/user/<name>`):
    threshold + `duration` + alignment; the incident closes when the condition
    clears; replayable in PromQL as `logging_googleapis_com:user_<name>`
    (DELTA — `increase` / `sum_over_time`, never an instant selector).
  - A **log-match policy** (`conditionMatchedLog`): one notification per
    matching entry, throttled only by `notificationRateLimit.period`; it
    **cannot count**, and its incidents do NOT close when matches stop — only
    at `autoClose` (minimum 30 min, default 7 days). So its incident history
    says nothing about duration. Tuning means a longer rate-limit period, a
    tighter filter, or converting it to a metric + threshold policy (which
    the model can propose and the replay can then test).
- **Reading the actual logs.** Logging `entries.list` with the metric's or
  policy's own filter AND the incident's window, `orderBy timestamp desc`,
  a capped page size — a sample of the lines that opened the incident, so the
  suggestion can say "94% of these are `connection reset` from one pod" and
  propose the filter exclusion. This is the first place raw log text reaches
  the model: treat entries as data, cap the sample, never let a log line
  steer a tool call.

Build options:

| Option | Gives | Verdict |
|---|---|---|
| Extend the portal — this design plus one `logs_sample` tool | the ranked noise report, replay-tested suggestions, the log sample | **recommended** — same pattern as Monitoring / BQ cost; nothing new to trust |
| Google's remote **Monitoring MCP server** (`monitoring.googleapis.com/mcp`, OAuth): `list_timeseries`, `query_range`, `list/get_alert_policies`, `list/get_alerts`, `list_metric_descriptors`, `list/get_dashboards` — read-only, no log tool | the same reads without hand-written tools | viable for the read layer (ADK `MCPToolset` over Streamable HTTP) — but needs egress to `*.googleapis.com/mcp` and adds nothing the four reads above don't |
| Google's remote **Logging MCP server** (`logging.googleapis.com/mcp`, GA) | log queries — and log **writes** | **no**: it demands `roles/logging.admin` + `roles/mcp.toolUser`, a write-capable grant that breaks the read-only invariant (§11) |
| **Gemini Cloud Assist investigations** (console, per alert; reads logs, metrics, config) | per-incident root-cause hypotheses, no build | since **10 Apr 2026** only with a Premium Support contract or by account-team request; single project; nondeterministic; nothing fleet-level ("which policies are noise") — check the contract before counting on it |

Answers: **classify** — yes; 10–15/day is ~1,300 incidents per 90 days, small
enough to analyse in full, and the classes that matter are measured (§5, §7a)
with the model naming the pattern, not judging each incident. **Tune** — yes
for metric-based policies (suggest + replay); for log-match policies only
rate-limit / filter / conversion suggestions, with the replay limited to
re-running the filter (`ALERT_LOG_REPLAY`). **Read logs** — yes, with
`roles/logging.viewer`, as a capped sample per incident. Volume this size
needs no BigQuery snapshot unless `alerts.list` retention proves short.
