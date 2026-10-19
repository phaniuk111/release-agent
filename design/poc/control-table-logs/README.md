# PoC: the control table as JSON logs, queried with SQL, on a dashboard

The Support triage agent reads a BigQuery control table: one row per workflow
run, with a business date, a status, the source, system, process, book and
entity it ran for, and an error when it failed. This PoC sends the same rows
to Cloud Logging as structured JSON, asks Log Analytics about them in SQL, and
puts the answers on a Cloud Monitoring dashboard — so support can see "what
failed today, where, how widely" without opening BigQuery or the portal.

Nothing here names a real table or column: the JSON fields are the agent's
ROLE names (`tools/support/config.py`), so one query and one dashboard fit any
deployment's table.

## Files

| File | What it is |
|---|---|
| `log_rows.py` | Writes rows as log entries (stdlib only, `entries:write` with your gcloud token). Generates a realistic two-day sample, or takes your own rows as JSON lines keyed by role. |
| `query.sql` | The Log Analytics question: failed and running runs per business date × source × system, books hit, an example error. |
| `dashboard.json` | A Cloud Monitoring dashboard built from SQL charts: two scorecards, a donut by source, failures per hour, the table above, and the failed rows themselves. A `Source` variable filters every widget. |

`__PROJECT__` in `query.sql` and `dashboard.json` is substituted at use; the
project id is never committed.

## Run it

```bash
P=<your project>
# once: Log Analytics on the bucket, and a BigQuery link so SQL also works from the terminal
gcloud logging buckets update _Default --location=global --enable-analytics
gcloud logging links create control_table_logs --bucket=_Default --location=global

python3 log_rows.py --project "$P"                                   # 80 sample rows, 2 business dates
sed "s/__PROJECT__/$P/g" query.sql | bq query --nouse_legacy_sql     # or paste into Logs Explorer > Log Analytics
sed "s/__PROJECT__/$P/g" dashboard.json > /tmp/dash.json && gcloud monitoring dashboards create --config-from-file=/tmp/dash.json
```

To log the real table, add one step to the job that writes it, or a small
reader that selects the day's rows and maps columns to roles the way
`SUPPORT_COLUMNS` does, then calls `log_rows.py --from rows.jsonl`.

## What each entry looks like

```json
{"kind": "control-table-row", "date": "2026-10-19", "run_id": "…", "event_id": "EVT-…",
 "system": "TRADE-A", "source": "risk-fetcher", "process": "REPORT-RISK",
 "unit": "BOOK-004", "scope": "ENTITY-UK", "job_id": "2026-…", "members": 0,
 "status": "FAILED", "failed": true,
 "error": "Dataflow job failed: worker ran out of memory", "error_kind": "RESOURCE"}
```

Every row is logged at INFO, as the pipeline logs them: the row's `status`
says what failed, and nothing — the queries, the dashboard, the logs panel —
reads the severity. (`failed` is a convenience flag the sample writer adds;
nothing depends on it either.)

## What we learned

- **Timestamps must be live.** Log Analytics indexes an entry by its own
  `timestamp`; entries stamped before the bucket's upgrade are readable in Logs
  Explorer but never appear in SQL. A backdated batch (the first version of
  this script) returned nothing for an hour of head-scratching.
- **Rows show up in SQL in about a minute.** Fresh entries were queryable
  within 90 s through the linked dataset.
- **The dashboard API takes SQL directly**: any chart's data set can carry
  `timeSeriesQuery.opsAnalyticsQuery.sql`, and a value-only dashboard
  variable can be filled by SQL and used as `@Source` with
  `IF(@Source = '*', TRUE, …)`, because SQL does not know the `*` wildcard.
- **A SQL chart also needs `dimensions` and `measures`** naming the result
  columns, or the widget says only "Request contains an invalid argument"
  (the table widget needs neither). Found by letting the console save a
  chart and reading the JSON back. The rules that held up:
  - a scorecard needs a TIMESTAMP dimension — return the newest timestamp
    beside the number (`TIMESTAMP_TRUNC(MAX(timestamp), MINUTE) AS ts`);
  - a string dimension needs `columnType` and a `maxBinCount`;
  - a measure is a `sum` of a numeric column, or a `count` of rows;
  - one series per value (a breakdown) is only allowed with `count`, so a
    per-source chart queries the raw rows and lets the chart bin them.
- **100,000 rows is fine.** Loading them took 190 s (batches of 500, eight
  in parallel); every widget's query over them answers in 3–6 s.
- **Cost:** nothing above the free tier — Cloud Logging's first 50 GiB a
  month, Log Analytics and dashboards are free; the linked dataset is a view.

## Where this could go

- The pipeline logs each row as it writes it (one line in the writer), and the
  dashboard is live without a reader.
- A log-based alert on `jsonPayload.failed=true` grouped by source, with the
  portal's Auto-triage finding linked from the notification.
- Auto-triage's own findings logged the same way, so the dashboard shows the
  cause and the owner next to the count.
