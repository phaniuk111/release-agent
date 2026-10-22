# Control-table dashboard from the workflow's own logs

For a workflow that logs each status change of a control-table row with one
logger, the row serialised to JSON text in `jsonPayload.message` — from GKE
containers (log `stdout`) and Dataflow workers (log
`dataflow.googleapis.com/worker`) alike. Nothing changes in either: the
dashboard parses the text in SQL (Log Analytics) and draws it in Cloud
Monitoring.

## Build and create it

```bash
python3 workflow_state_dashboard.py \
  --columns "date=<cob column>,run_id=<run column>,status=<status column>,unit=<book column>,source=<source column>,..." \
  --logger <logger class> --done COMPLETED,SUCCEEDED --failed FAILED,ERROR > dashboard.json
sed "s/__PROJECT__/<project>/g" dashboard.json > dash.json
gcloud monitoring dashboards create --config-from-file=dash.json
```

`--columns` takes the `role=column` form of the portal's `SUPPORT_COLUMNS`.
Required roles: `date`, `run_id`, `status`, `unit`. Optional: `system`,
`source`, `process`, `scope`, `error`, `members`, `event_at`, `updated_at` (a
missing one reads as NULL; `updated_at` falls back to the log entry's own
timestamp). Keep the real column names, logger class and status words in your
own values, not in a public repo. The project's `_Default` log bucket needs
Log Analytics switched on (free):

```bash
gcloud logging buckets update _Default --location=global --enable-analytics
```

## Or start from the template

`dashboard.template.json` is the same dashboard already built, with
placeholders: `__PROJECT__`, `__LOGGER__`, one `*_COLUMN` per role
(`COB_COLUMN`, `RUN_ID_COLUMN`, `STATUS_COLUMN`, `BOOK_COLUMN`,
`SYSTEM_COLUMN`, `SOURCE_COLUMN`, `PROCESS_COLUMN`, `ENTITY_COLUMN`,
`ERROR_COLUMN`, `MEMBERS_COLUMN`, `EVENT_TIME_COLUMN`, `WRITE_TIME_COLUMN`)
and the status words (`DONE_STATUS_1/2`, `FAILED_STATUS_1/2`). Replace them,
for example with one `sed -e "s/COB_COLUMN/<your column>/g" -e …` per name,
then `gcloud monitoring dashboards create --config-from-file=dash.json`. The
builder above does the same in one step and checks the names.

## How the SQL reads the logs

Every widget starts from one block:

```sql
WITH r AS (
  SELECT timestamp, SAFE.PARSE_JSON(JSON_VALUE(json_payload.message)) AS m
  FROM `PROJECT.global._Default._AllLogs`
  WHERE log_name IN ('projects/PROJECT/logs/stdout', 'projects/PROJECT/logs/dataflow.googleapis.com%2Fworker')
    AND JSON_VALUE(json_payload.logger) = '<logger class>')
```

`JSON_VALUE` takes `message` out as text, `PARSE_JSON` turns it back into an
object, and `SAFE.` skips a line that is not JSON instead of failing the
query. The block then renames the columns to roles, keeps each run's latest
status and each book's latest run (one line per status change would
otherwise count a run several times), and finds the books that ran on the
previous COB but have not started on the chosen one.

## Widgets

Failed runs, books processed, books still to process (in flight + failed +
not started), median minutes per processed book, runs by latest status, the
books table (to-do first, with minutes taken and the last error), where it
failed (source × system), and the failed log lines. `COB` and `Source` filters
apply to all of them; with no COB chosen the scorecards show the latest one.

Tested in a sandbox with 148 lines in this format from two GKE containers and
one Dataflow-shaped source: every query, and the console, agreed with the data.
Two Monitoring quirks it already handles: a SQL chart needs `dimensions` and
`measures` (a scorecard a TIMESTAMP dimension), and `ROWS` is a reserved word.
