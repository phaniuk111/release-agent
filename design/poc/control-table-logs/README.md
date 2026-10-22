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
| `control_table_log.py` | `setup(service)` once, then plain `log.info(..., extra=row_fields(row))` per row — from a GKE service or a Dataflow step alike. Google's logging handler sends the row through the Logging API as the JSON payload of log `control-table`, with a `service` label. Needs `google-cloud-logging`. |
| `otel_row.py` | The producer-side OpenTelemetry route for a Collector you cannot configure: the row is the OTLP log body and a `gcp.log_name` attribute names the log, so a plain Collector writes the JSON payload with no transform. |
| `otel-collector.yaml` | The third way in: an OpenTelemetry Collector that takes OTLP logs and writes them to Cloud Logging as the same entries — a `transform` step copies the row's attributes into the body, which the Google exporter stores as the JSON payload. |
| `workflow_state_dashboard.py` | The dashboard for a workflow that logs each status change of a row as a JSON **string** in `jsonPayload.message` (GKE `stdout` and Dataflow worker logs alike). Generic: the column names come as `role=column` (the `SUPPORT_COLUMNS` form) with the logger class and status words, from your own values. |
| `dashboard.json` | A Cloud Monitoring dashboard built from SQL charts: failed runs, books hit, books processed and books still to process for the chosen COB, a donut by source, the books with their latest status (to-do first), failures per hour, the table above, and the failed rows themselves. `COB` and `Source` variables filter every widget; with no COB chosen the scorecards show the latest date. |

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

## Books processed and still to process

The control table only holds runs that have started, so the dashboard works
it out from the rows: one row per book on the chosen COB with its LATEST
run's status (retries share the book).

- **Processed** = books whose latest run is in a done status
  (`SUCCEEDED`, `COMPLETED` — the values file's `done:` list).
- **Still to process** = books seen on the COB whose latest run is not done
  (failed, running) **plus** books that ran on the previous COB and have not
  appeared yet today ("not started"). If the business keeps an expected list
  of books, swap the previous-COB universe for that list.

The sample writer holds one book back on the latest date so the not-started
case shows: 9 processed + 2 not done + 1 not started = 12 books.

## Time taken per book

Timing comes from the row, not from the log entry: `event_at` (the role for
"when the triggering event arrived") and `updated_at` ("when the row was
written"). A real writer copies both from the row. Per book on the chosen
COB: **took** = latest successful row's `updated_at` − earliest `event_at`
(retries included, so it is the time the business waited, not one run's
duration); it is blank until the book's latest run is done. The dashboard
shows the median and the slowest book, the COB end to end (first event in →
last book done), and a `took_min` column in the books table.

If the rows carry neither timestamp, the log entry's own `timestamp` stands
in for `updated_at` and the duration is from the first logged row — still
right when every status change is logged as it happens.

## Writing from GKE and Dataflow

Both produce rows. Dataflow does write a JSON payload for every worker log
line — but its fields are the worker's (`job`, `step`, `worker`, `logger`,
`thread`), and the row you log lands as a *string* inside
`jsonPayload.message`. Proved on a real one-worker job (2026-10-20), where
the same row was logged three ways from a `DoFn`:

| In the worker | What arrived |
|---|---|
| `log.info(json.dumps(row))` | `jsonPayload.message` = the JSON **string** |
| `log.info("…", extra=row_fields)` | the message text only — the `extra` fields were **dropped** |
| `print(json_line)` on stdout | `jsonPayload.message` = the line as a string — **not** parsed into fields as GKE would |

So nothing a worker does with normal logging makes the row's fields payload
fields. GKE is the opposite: its agent parses a printed JSON line into real
fields, under its own log name. Two ways to one dashboard:

**1. Keep each platform's default logging (no code change beyond the line).**
Every query here reads both shapes: a field, else the same field parsed out
of the worker's message —
`COALESCE(JSON_VALUE(json_payload.status), JSON_VALUE(PARSE_JSON(JSON_VALUE(json_payload.message)), '$.status'))`
— and looks in both logs (`control-table` and `dataflow.googleapis.com%2Fworker`).
Tested with a worker-shaped row under the real Dataflow log name: every
widget read it. Costs: `PARSE_JSON` on every worker line that carries the
`kind` marker, the entry's time is the worker's (log the row when you write
it and it is the same to the second), and a change to the row's fields means
touching both halves of each `COALESCE`.

**2. One handler on both (`control_table_log.py`).** `setup(service)` attaches
Google's Cloud Logging handler, then plain `log.info("…", extra=row_fields(row))`
sends the row through the Logging API as the JSON payload of log
`control-table` from a pod or a Dataflow worker alike, with a `service`
label. Tested from a process standing in for a Dataflow step. Costs: one
setup line per producer, `google-cloud-logging` on the worker, and
`roles/logging.logWriter` for the Dataflow worker's service account.

Both work today. 2 keeps the queries simple and the timestamps the row's own;
1 keeps producers untouched. The queries support both at once, so you can
start with 1 and move producers to 2 one at a time.

## OpenTelemetry instead

Works too, and fits a shop that already runs the Collector (the portal sends
its traces that way). The producer keeps plain `log.info` with the row in
`extra`; the OpenTelemetry logging bridge (`LoggingHandler`) turns `extra`
into attributes and sends OTLP to a Collector, whose `googlecloud` exporter
writes Cloud Logging. Two things learned by running it (`otel-collector.yaml`):

- **Attributes become entry labels, not the payload.** Out of the box the
  row arrived as `labels.status`, `labels.unit`, … with the message as
  `textPayload`, and `resource.type` `generic_task`. The Collector's
  `transform` processor fixes that in one statement —
  `set(body, attributes) where attributes["kind"] == "control-table-row"` —
  and the Google exporter then writes the body as the JSON payload: the
  dashboard read the row with no query change.
- **`process` is a reserved name on a Python log record**, so it cannot go in
  `extra`; send it as `process_type` (or any other name) on this route. The
  Cloud Logging handler route has no such limit because the row travels
  inside `json_fields`.

**A shared Collector you cannot configure** (the Google-built one a platform
team runs in the cluster): make the entry right at the source instead —
`otel_row.py` emits the row as the OTLP log *body* with a `gcp.log_name`
attribute. Tested through a Collector with no transform at all: the row
arrived as the JSON payload of log `control-table`, `process` included (no
logging bridge, so no reserved name). The Collector only needs an OTLP
receiver and a logs pipeline ending in the `googlecloud` exporter; look for
`pipelines: logs:` in its ConfigMap. The Google-built Collector's GKE
deployment carries traces, metrics and logs.

Cost: a Collector to run (on GKE a DaemonSet or the managed one; a Dataflow
worker can reach one over the network), against no extra moving part on the
other two routes.

## When the row is logged as a string by your own logger

Many teams already log the row — every status change, as it happens — with
one logger whose `message` is the row serialised to JSON text. Then nothing
needs to change in GKE or Dataflow: `workflow_state_dashboard.py` builds a
dashboard that parses that text in SQL. Every widget starts from one block —

```sql
WITH r AS (
  SELECT timestamp, SAFE.PARSE_JSON(JSON_VALUE(json_payload.message)) AS m
  FROM `PROJECT.global._Default._AllLogs`
  WHERE log_name IN ('projects/PROJECT/logs/stdout', 'projects/PROJECT/logs/dataflow.googleapis.com%2Fworker')
    AND JSON_VALUE(json_payload.logger) = '<logger class>')
```

— then renames the columns to roles from the `--columns` mapping, keeps each
run's latest status and each book's latest run (one line per status change
would otherwise count a run several times), and works out the books that ran
on the previous COB but have not started on the chosen one. Widgets: failed
runs, books processed, books still to process, median minutes per book, runs
by status, the books table (to-do first), where it failed, and the failed
lines. Tested in a sandbox with 148 lines from two GKE containers and one
Dataflow-shaped source: every query, and the console, agreed with the data.

## Telling these rows apart from other services' logs

Other services write JSON too, so every query keys on two markers that only
these rows carry, and you need just one of them to hold:

- **the log name** — the writer sends to `logs/control-table`
  (`log_name = 'projects/P/logs/control-table'` in SQL,
  `logName="projects/P/logs/control-table"` in Logs Explorer). Use this when
  the writer can pick its log (the Cloud Logging client's `logger('control-table')`);
- **a field in the payload** — `kind: "control-table-row"`
  (`JSON_VALUE(json_payload.kind) = 'control-table-row'` / `jsonPayload.kind="control-table-row"`).
  Use this when the rows ride a shared log, such as a container's stdout or
  a Dataflow worker's log, where the log name is the platform's.

A field another service might also use (`status`, `date`) is never the
filter. If the rows should also live apart — their own retention, their own
readers — route them with a sink into their own bucket and query that
bucket's view instead:

```bash
gcloud logging buckets create control_table --location=global --enable-analytics --retention-days=400
gcloud logging sinks create control-table-rows logging.googleapis.com/projects/$P/locations/global/buckets/control_table \
  --log-filter='jsonPayload.kind="control-table-row"'
# then FROM `P.global.control_table._AllLogs`
```

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
