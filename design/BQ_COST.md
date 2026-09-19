# BigQuery cost optimisation — agentic design

Status: **proposed** (2026-09-19). Not built. Read AGENTS.md first; this follows
the Monitoring pill's pattern (facts from tools, the model narrates and
proposes, nothing applies itself) and the alert-noise design
(`ALERT_HYGIENE.md`) for the shape of the loop.

## 1. Goal, and what "agentic" adds

Find the queries and tables costing the most in the team's **dedicated**
BigQuery project, work out *why*, propose a cheaper form, and **prove it with
a dry run** before anyone sees it — then, on the next run, notice whether the
suggestion was taken up and whether the cost actually fell.

A report can rank cost. What makes this agentic is three things a report
cannot do:

1. **Investigation is dynamic.** For each top item the model decides what to
   look at next — stages, the table's partitioning, Google's recommendation,
   the referenced columns — through tools, instead of a fixed pipeline
   fetching everything for everything.
2. **Every proposal is verified, and retried once on failure.** A rewrite
   that dry-runs no cheaper, or changes the result schema, goes back to the
   model with the numbers; a second failure is reported as "tried, not
   cheaper", never as advice.
3. **The loop closes.** Findings and proposals are recorded; the next run
   measures the same query shape again. A suggestion that was adopted shows
   as a measured saving; one ignored three runs running stops being repeated
   and is listed once under "still open". The tool learns what the team
   acts on without any model being trained.

The portal **writes nothing to BigQuery** beyond its own append-only findings
table, and runs no query that is not a dry run or an `INFORMATION_SCHEMA`
read.

## 2. Access — verified with `gcloud iam roles describe`

| Need | Role | Why this one |
|---|---|---|
| Every principal's jobs, slot usage, reservations | `roles/bigquery.resourceViewer` | the smallest role with `bigquery.jobs.listAll` (16 read-only permissions, no table data) |
| Table schemas, partitioning, clustering, `INFORMATION_SCHEMA.TABLES/COLUMNS/PARTITIONS` | `roles/bigquery.metadataViewer` | `tables.get/list`, `datasets.get` — metadata, not data |
| Run `INFORMATION_SCHEMA` queries and **dry runs** | `roles/bigquery.jobUser` | `jobs.create`. This role can run real queries too — the tool's own guard (§9) is what keeps it to dry runs |

**Not available: the recommender roles.** So `INFORMATION_SCHEMA.RECOMMENDATIONS`
/ `INSIGHTS` (which need `recommender.*` permissions) are out. Everything
below is `INFORMATION_SCHEMA` only — table-change advice is *derived* from
`JOBS` + `PARTITIONS` + `COLUMNS`, never read from Google's recommender.

**Not granted:** `bigquery.dataViewer`. The tool — and therefore the model —
never reads a row of any table. Dry runs return bytes and result *schema*,
not data. The project is dedicated to the team, so scope is the whole
project: no dataset filter, no cross-team concern.

## 3. Inputs

| Source | View / API | Used for |
|---|---|---|
| Query cost | `JOBS_BY_PROJECT` (180 days): `total_slot_ms`, `total_bytes_billed`, `cache_hit`, `referenced_tables`, `query_info.query_hashes.normalized_literals`, `query_info.performance_insights`, `job_stages` | the ranking, and the *why* |
| Slot supply | `RESERVATIONS`, `ASSIGNMENTS`, `JOBS_TIMELINE_BY_PROJECT` | slot-hours need a denominator; contention needs a timeline |
| Table layout | `TABLES`, `TABLE_OPTIONS`, `COLUMNS`, `PARTITIONS`, `TABLE_STORAGE` | is the filter column the partition column; is the table clustered; how big; expiry |
| Write side | `WRITE_API_TIMELINE`, `STREAMING_TIMELINE` | unbatched writes, ingestion errors (the portal's own writes are in `STREAMING_TIMELINE`, legacy `insertAll`) |
| The verifier | `jobs.query` with `dryRun=true` | bytes a query *would* process, and its result schema — without running it |

All per-region: `region-europe-west3` in the enterprise. Ordering is
`slot_hours` on reservations, `gb_billed` on-demand (`BQ_COST_BILLING`).

## 4. The loop

```
1. SCAN         top N query shapes by cost, 14 days; storage and write findings   deterministic
2. INVESTIGATE  per top item the agent calls tools as needed:
                  bq_query_detail   stages, insights, referenced tables, p50/p95 bytes
                  bq_table_layout   partition/cluster/size/expiry of each referenced table
                  bq_prune_estimate what partitioning/clustering on a column would have pruned   ← model chooses what to look at
3. PROPOSE      a structured change: a rewritten SQL, or a table change
                (partition / cluster / expiry), with one sentence of reasoning  ← model
4. VERIFY       rewrite   -> dry run: bytes before vs after, result schema before vs after
                table change -> PARTITIONS/JOBS evidence only: bytes per run vs table
                              size, the filter column, partition stats if any     deterministic, ESTIMATED
5. RETRY        not cheaper, or schema changed unintentionally -> once more with
                the numbers; then give up and say so                            ← model, bounded
6. REPORT       3-5 items, biggest measured saving first, each with the evidence
7. TRACK        record shape, cost, proposal, verdict; next run compares        deterministic
```

The model is in steps 2, 3 and 5 only. Steps 1, 4, 6 and 7 are code.

## 5. The verifier — what a dry run can and cannot prove

`jobs.query(dryRun=true)` returns `totalBytesProcessed` and the **result
schema** without executing. From that, deterministically:

| Check | Verdict |
|---|---|
| parses and plans | else **rejected** ("invalid") |
| bytes after < bytes before | else **rejected** ("not cheaper", with both numbers) |
| result schema identical | **equivalent shape** — same columns, same types |
| result schema differs | **changes the result** — allowed only if the proposal declared it (dropping `SELECT *` to named columns is the common, legitimate case) and the report says so in bold |
| referenced tables ⊆ original's | else **rejected** — a rewrite must not read something new |

What it cannot prove: **row-level equivalence.** A filter rewritten wrongly
can return different rows with identical schema and fewer bytes. The report
states this on every rewrite, so review is a person's job — the tool makes
the *cost* claim measured and the *equivalence* claim explicit, never
implied. Bytes are also not slot-time: on reservations the saving is
reported as bytes with a note, not as slot-hours.

Table changes (partition, cluster, expiry) cannot be dry-run, and Google's
recommender is not available here. They are proposed from `INFORMATION_SCHEMA`
evidence alone, and always marked **estimated**:

- **partition candidate**: a table read by ≥ N runs whose `JOBS.referenced_tables`
  bytes ≈ the table's full size each time, unpartitioned (`TABLES` /
  `TABLE_OPTIONS`), and whose sample SQL filters on a `DATE`/`TIMESTAMP`
  column (`COLUMNS`) — the estimate is "each run reads the whole table
  (X GB × runs); partitioned by that column it would read the matching
  partitions". If the table is *already* partitioned but the query still
  scans everything, `PARTITIONS` gives real per-partition bytes and the
  estimate is much tighter: the rewrite (a partition-column filter) can then
  be **dry-run** and becomes measured.
- **cluster candidate**: repeated equality/`IN` filters on a low-cardinality
  column of a large partitioned table — estimated only; no dry run can show
  clustering's effect.
- **expiry candidate**: `TABLE_STORAGE` with no `expiration` and no read in
  the window (`JOBS.referenced_tables`) — this one *is* exact: bytes × price.

The gap this leaves is honest: without the recommender, partition/cluster
advice is the tool's inference from access patterns, not Google's model of
them. Rewrites of the *query* remain fully measured by dry run either way.

## 6. Where the model is — and is not

**Is:** choosing what to investigate for a top item (step 2); writing the
rewrite or naming the table change (step 3); retrying with feedback (step 5);
the report's sentences.

**Is not:** ranking cost (§4 step 1 — arithmetic over `JOBS`); deciding a
proposal is good (§5 — the dry run does); estimating any number (every
figure in the report is a tool result or a subtraction of two); reading any
table's data (no permission exists).

Skill rules (`skills/bq-cost/SKILL.md`), non-negotiable: every number from
the payload; biggest measured saving first; three to five items then stop;
each item ends in an action; a rewrite whose schema changed says so first;
a quiet fortnight says "nothing significant".

## 7. The closed loop — memory across runs

Append-only table in the team's dataset, same discipline as
`release_intents`:

```
bq_cost_findings
  run_ts TIMESTAMP, qhash STRING, kind STRING (query|table|storage|write),
  cost_before_bytes INT64, cost_before_slot_ms INT64, proposal JSON,
  verdict STRING, bytes_after INT64, times_reported INT64
```

On each run, for every `qhash` reported before:

- cost fell by ≥ 30% and the shape changed → **adopted**: reported once as a
  measured saving ("`events_rollup`: 380 GB → 2.1 GB per run, 96 runs —
  saving 36 TB/fortnight"), then retired;
- cost unchanged after 3 reports → **still open**: listed once, at the
  bottom, not re-argued;
- new shape → investigated as usual.

This is what stops the weekly report becoming the same five lines nobody
reads — the list is always what is *new* or *changed*, and adoptions are
counted, which is the only metric that says the tool is worth its cost.

## 8. Tools

| Tool | Deterministic? | Returns |
|---|---|---|
| `bq_cost_scan(days=14, top=10)` | yes | ranked shapes + storage/write findings + open recommendations; cached like `/api/monitoring` |
| `bq_query_detail(qhash)` | yes | stages, `performance_insights`, referenced tables, runs, byte percentiles, one sample SQL |
| `bq_table_layout(table)` | yes | partitioning, clustering, size, row count, expiry, last modified |
| `bq_prune_estimate(table, column)` | yes | estimated bytes a partition/cluster on `column` would prune, from `PARTITIONS` + `JOBS` — marked estimated |
| `bq_dry_run(sql)` | yes | `{ok, bytes, schema, referenced_tables, error}` — never executes |
| `bq_propose(qhash, evidence)` | **model** | the structured proposal (§4 step 3) |
| `bq_findings(qhash=None)` | yes | history from `bq_cost_findings` — what was suggested before and what happened |

HTTP: `GET /api/bq-cost/report`, `GET /api/bq-cost/{qhash}`. Pill **BQ cost
report** in the *Check* group (or a fourth group if it stays a distinct
audience), `PREVIEW_FEATURES`-gated. Weekly: a `CronJob` in the Helm chart
running the same scan, posting the report to a Teams/email channel and
writing `bq_cost_findings`.

## 9. Guardrails — the ones that matter because `jobUser` can run real queries

- **Every query the tool issues is either a dry run or an `INFORMATION_SCHEMA`
  read.** Enforced in one function through which all SQL passes: it sets
  `dryRun=True` unless the statement's only `FROM` is an `INFORMATION_SCHEMA`
  view — checked on the tokenised statement, no regex, and tested. No DDL,
  no DML, no `CREATE`, ever.
- **Budget per run**: `INFORMATION_SCHEMA` reads bill a 10 MB minimum each;
  the scan is capped at `BQ_COST_MAX_QUERIES` (40) and the report states its
  own cost ("this report read 0.4 GB").
- **No data**: no `dataViewer`; the model is given metadata and numbers only.
- **The retry is bounded** (one), and a rejected proposal cannot be shown as
  a recommendation.
- **Findings table is append-only**, empty dataset disables it cleanly.
- The portal's own queue reads are in the data (they were the sandbox's top
  shape); they are reported like any other, not hidden.

## 10. Config (helm `values.yaml → config:`)

```
BQ_COST_PROJECT: ""              # empty = feature off (default: BQ_PROJECT)
BQ_COST_REGION: "region-europe-west3"
BQ_COST_BILLING: "reservations"  # or "on-demand": decides slot_hours vs gb_billed ordering
BQ_COST_DAYS: "14"
BQ_COST_TOP: "10"
BQ_COST_MAX_QUERIES: "40"        # INFORMATION_SCHEMA reads per run (10 MB minimum each)
BQ_COST_DATASET: ""              # for bq_cost_findings; empty = no memory across runs
PREVIEW_FEATURES: "monitoring,bq-cost"
```

## 11. Rollout — each stage stands alone

| Stage | Delivers | Effort |
|---|---|---|
| **0 — the check** | the two scan queries run once by hand in the enterprise project: top 10 shapes, running-now, unpartitioned large tables read often, tables with no expiry | 1 hour, no code |
| **1 — scan + report** | §3 reads, §4 step 1, a narrated report; no proposals | 1 day |
| **2 — investigate + verify** | the per-item tools, `bq_dry_run`, and the model's rewrite with the §5 verifier | +1–2 days |
| **3 — closed loop** | `bq_cost_findings`, adoption tracking, the weekly CronJob | +1 day |

Stop after stage 0 if the top ten are small or already known. The sandbox
run on 2026-09-19 found the portal's own `SELECT *` billing the 10 MB minimum
240 times against an 18 KB table — a cent, but the right kind of finding.

## 12. Open questions — answer before stage 1

1. **Reservations or on-demand?** Decides the ordering and how savings are
   expressed.
2. **Where do the expensive queries come from** — scheduled queries, Airflow
   DAGs, dashboards, ad hoc? Decides whether a rewrite has a file to go to
   (an optional PR later, off by default as in the alert design) or is
   handed to a person.
3. Are any of the expensive shapes **the portal's own**? They will show up;
   worth deciding up front that they are reported like everything else.

## 13. File map (when built)

```
src/release_agent/tools/bq_cost.py         scan · detail · layout · recommendations · dry_run · findings   (~350 lines)
src/release_agent/tools/bq_guard.py        the one function all SQL passes through: dry-run unless INFORMATION_SCHEMA-only
adk_release_agent/tools.py                 the wrappers in §8
adk_release_agent/skills/bq-cost/SKILL.md
src/release_agent/static/core/bq_cost.js   wording for the report rows (shared with the Backstage port)
tests/test_bq_cost_scan.py                 ranking on synthetic JOBS rows
tests/test_bq_guard.py                     every non-INFORMATION_SCHEMA statement is forced to dry run; DDL/DML refused
tests/test_bq_verifier.py                  the §5 verdict table
bigquery/bq_cost_findings.schema.json      the memory table (append-only)
```

No new dependency — `google-cloud-bigquery` is already in the image for the
release queue.
