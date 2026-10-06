---
name: bq-cost
description: "Answer BigQuery cost questions: what is costing us most in BigQuery, why a specific query is expensive, whether a proposed rewrite would be cheaper, and which tables should have expiry, partitioning or clustering."
metadata:
  adk_additional_tools:
    - bq_cost_scan
    - bq_query_detail
    - bq_table_layout
    - bq_prune_estimate
    - bq_dry_run
    - bq_verify_rewrite
    - bq_findings
---

Use this skill for BigQuery cost questions: "what is costing us most in
BigQuery", "why is this query expensive", "would this rewrite be cheaper",
"which tables should have expiry/partitioning". The project is the team's own
dedicated project, so scope is the whole project — no dataset filter, no
cross-team concern.

How to answer:
- "What's costing us most?" / a periodic check → `bq_cost_scan`. Report the
  ranked query shapes plus the storage and write findings.
- "Why is X expensive?" (a qhash from a scan row) → `bq_query_detail`, then
  `bq_table_layout` and/or `bq_prune_estimate` for the tables it reads — the
  model chooses what to look at for that item, not a fixed pipeline that
  fetches everything for everything.
- "Would this be cheaper?" (a rewrite the user gives you, or one you propose
  yourself) → `bq_verify_rewrite`, never a plain `bq_dry_run` alone — only the
  verifier produces an accept/reject verdict.
- "Should this table be partitioned/clustered/expired?" → `bq_table_layout` +
  `bq_prune_estimate`.
- "Did we act on last time's suggestions?" → `bq_findings`.

The agentic loop, for each top shape from a scan:
1. `bq_query_detail(qhash)` — stages, insights, referenced tables, sample SQL.
2. As needed: `bq_table_layout` for a referenced table's partitioning and
   clustering, `bq_prune_estimate` for what a column would have pruned.
3. PROPOSE a rewrite yourself (or a table change) from that evidence, with one
   sentence of reasoning.
4. TEST it. A rewrite goes through `bq_verify_rewrite`. A table change
   (partition/cluster/expiry) cannot be dry-run — back it with
   `bq_prune_estimate` / `bq_table_layout` evidence instead, and label it
   estimated.
5. If `bq_verify_rewrite` rejects the rewrite, revise it ONCE using the
   numbers it returned (bytes before/after, the schema diff, any new tables)
   and test again. A second rejection is reported as "tried, not cheaper" — a
   rejected rewrite is NEVER presented as advice, and is not tried a third
   time.

Rules (non-negotiable):
- Every number in the report comes from a tool's payload. Never estimate a
  number yourself — `bq_prune_estimate` and table-change proposals already
  come back marked `estimated` / `measured: false`; keep that label when you
  repeat them, never upgrade an estimate to a fact.
- Order findings with the biggest MEASURED saving first. Estimated
  table-change candidates and "still open" items from `bq_findings` belong at
  the bottom, listed once, not mixed in by size and not re-argued.
- Report three to five items, then stop — this is a digest, not a dump of
  everything the scan found.
- Every item ends in an action: the exact rewritten SQL to apply, or the exact
  table change (partition / cluster / expiry) to make. A finding with nothing
  to do next is not worth the line.
- A rewrite whose result schema changed says so FIRST, in bold, before the
  saving figure — never bury a schema change under the number.
- Every accepted rewrite repeats the verifier's own note: row-level
  equivalence is NOT proven, only cost and schema — say this every time, not
  just the first.
- Table changes (partition, cluster, expiry) are always ESTIMATED — no dry run
  can prove one. Label them as such; never present one as measured.
- A quiet run — nothing new, nothing changed, no candidates worth raising —
  is reported as "nothing significant", not padded out with old items to look
  busy.
- Always state the report's own cost (`report_cost_bytes` from
  `bq_cost_scan`) as a SIZE — "this report read 0.4 GB" — never as a dollar
  figure that rounds to $0.00: the scan itself is billed `INFORMATION_SCHEMA`
  reads and the size is the honest number.
- When a payload carries a `hint` (or a section's `storage_hint` /
  `writes_hint` / `storage_reads_hint`), repeat THAT explanation — it is the measured reason, e.g. a
  hidden dataset denying a region-wide view, a missing role, or a region with
  nothing in it. Never replace it with a role of your own choosing: if the
  hint does not name a role to grant, do not tell the person to grant one.
- A sample query that carries `@parameters` (`@days`, `@start_ts`, …) cannot
  be priced as it stands, and the failure is USUALLY SILENT: BigQuery often
  accepts the dry run and reports 0 bytes, as if the predicate were not there
  — which reads as a free query, or as a 100% saving. It sometimes answers
  "Undeclared query parameters" instead. The verifier catches both and returns
  reason `parameterised`. So: substitute a sensible literal (14 for a day
  window, the window's start for a timestamp), measure that, and STATE the
  substitution next to the verdict; never report a parameterised query's own
  0 bytes as a cost or a saving.
- A rewrite that could not be verified — the dry run failed, or it was
  rejected — is reported as "not verified: <reason>", and the general
  best-practice behind it (e.g. "avoid SELECT *") is offered only as an
  unverified next step, clearly separated from the measured findings.
- A storage finding whose `reads` is null means the read-count probe itself
  failed (`storage_reads_error` says why) — NOT that the table is unread.
  Never turn that into "nobody queries this, expire it": say the reads are
  unknown and why.
- You never read table data — there is no `dataViewer` role here. If asked for
  row counts by value, sample rows, or "what's actually in this table", say
  plainly that these tools have no data access, only metadata and cost.
