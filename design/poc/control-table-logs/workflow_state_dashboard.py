#!/usr/bin/env python3
"""Control-table dashboard for rows logged as a JSON STRING in jsonPayload.message.

For a workflow that logs each status change of a control-table row with one
logger, the row serialised into `message` — GKE containers (log `stdout`) and
Dataflow workers (log `dataflow.googleapis.com/worker`) alike. Every widget
starts from one shared SQL block: find the entries by logger, parse the
string once (SAFE.PARSE_JSON — a line that is not JSON is skipped, not an
error), rename the table's columns to roles, then take each run's LATEST
status and each book's latest run (every status change is its own line).

Nothing deployment-specific lives in this file. The column names come in the
same `role=column` form as SUPPORT_COLUMNS, with the logger class and the
status words, from your own values — keep those out of public repos.

    python3 workflow_state_dashboard.py \\
      --columns "date=<cob column>,run_id=<run column>,status=<status column>,unit=<book column>,source=<…>,…" \\
      --logger <logger class> --done COMPLETED,SUCCEEDED --failed FAILED,ERROR > dashboard.json
    sed "s/__PROJECT__/<project>/g" dashboard.json > dash.json
    gcloud monitoring dashboards create --config-from-file=dash.json

Roles: date, run_id, status and unit are required; system, source, process,
scope, error, members, event_at and updated_at are optional — a missing one
reads as NULL, and updated_at falls back to the log entry's own timestamp.
Needs Log Analytics on the project's _Default log bucket.
"""
from __future__ import annotations

import argparse
import json

REQUIRED = ("date", "run_id", "status", "unit")
OPTIONAL = ("system", "source", "process", "scope", "error", "members", "event_at", "updated_at")
P = "__PROJECT__"


def parse_columns(text: str) -> dict[str, str]:
    """'role=column,…' → {role: column}. Only plain identifiers: the names are
    spliced into SQL, so anything else is refused."""
    cols: dict[str, str] = {}
    for item in text.split(","):
        role, _, column = item.strip().partition("=")
        role, column = role.strip(), column.strip()
        if not role:
            continue
        if role not in REQUIRED + OPTIONAL:
            raise SystemExit(f"unknown role {role!r} — use {', '.join(REQUIRED + OPTIONAL)}")
        if not column or not all(ch.isalnum() or ch == "_" for ch in column):
            raise SystemExit(f"{role}= needs a plain column name (letters, digits, underscore)")
        cols[role] = column
    missing = [r for r in REQUIRED if r not in cols]
    if missing:
        raise SystemExit(f"map the required role(s): {', '.join(missing)}")
    return cols


def _words(text: str) -> str:
    """'A,B' → "('A', 'B')" for SQL; plain status words only."""
    items = [w.strip() for w in text.split(",") if w.strip()]
    if not items or any(not all(ch.isalnum() or ch in "_-" for ch in w) for w in items):
        raise SystemExit(f"status words {text!r}: letters, digits, _ and - only")
    return "(" + ", ".join(f"'{w}'" for w in items) + ")"


def build(cols: dict[str, str], logger: str, done: str = "COMPLETED,SUCCEEDED",
          failed: str = "FAILED,ERROR") -> dict:
    if not logger or not all(ch.isalnum() or ch in "._$" for ch in logger):
        raise SystemExit("logger: a class name (letters, digits, . _ $)")
    L, DONE, FAILED = logger, _words(done), _words(failed)

    def col(role: str, cast: str = "") -> str:
        if role not in cols:
            return f"CAST(NULL AS {cast or 'STRING'})"
        value = f"JSON_VALUE(m.{cols[role]})"
        return f"SAFE_CAST({value} AS {cast})" if cast else value

    logs = f"('projects/{P}/logs/stdout', 'projects/{P}/logs/dataflow.googleapis.com%2Fworker')"
    source_filter = f"IF(@Source = '*', TRUE, {col('source')} = @Source)" if "source" in cols else "TRUE"
    base = f"""WITH r AS (
  SELECT timestamp, SAFE.PARSE_JSON(JSON_VALUE(json_payload.message)) AS m
  FROM `{P}.global._Default._AllLogs`
  WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
    AND log_name IN {logs} AND JSON_VALUE(json_payload.logger) = '{L}'),
entries AS (
  SELECT {col('date')} AS cob, {col('run_id')} AS run_id, {col('status')} AS status,
         {col('unit')} AS book, {col('system')} AS system, {col('source')} AS source,
         {col('process')} AS process, {col('scope')} AS entity,
         {col('error')} AS error, {col('members', 'INT64')} AS members,
         {col('event_at', 'TIMESTAMP')} AS event_at,
         COALESCE({col('updated_at', 'TIMESTAMP')}, timestamp) AS updated_at
  FROM r WHERE m IS NOT NULL AND {source_filter}),
picked AS (SELECT IF(@COB = '*', MAX(cob), @COB) AS cob FROM entries),
runs AS (
  SELECT run_id, ANY_VALUE(cob) AS cob, ANY_VALUE(book) AS book, ANY_VALUE(source) AS source, ANY_VALUE(system) AS system,
         ARRAY_AGG(status ORDER BY updated_at DESC LIMIT 1)[OFFSET(0)] AS status,
         MIN(COALESCE(event_at, updated_at)) AS started, MAX(updated_at) AS ts,
         ARRAY_AGG(error IGNORE NULLS ORDER BY updated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS error
  FROM entries GROUP BY run_id),
books AS (
  SELECT book, ARRAY_AGG(status ORDER BY ts DESC LIMIT 1)[OFFSET(0)] AS status, MIN(started) AS started,
         MAX(ts) AS ts, COUNT(*) AS runs, ARRAY_AGG(error IGNORE NULLS ORDER BY ts DESC LIMIT 1)[SAFE_OFFSET(0)] AS last_error
  FROM runs WHERE cob = (SELECT cob FROM picked) GROUP BY book),
prev AS (SELECT DISTINCT book FROM runs WHERE cob = (SELECT MAX(cob) FROM runs WHERE cob < (SELECT cob FROM picked))),
not_started AS (SELECT book FROM prev WHERE book NOT IN (SELECT book FROM books))
"""
    SUM = {"type": "sum"}
    TS = [{"column": "ts", "columnType": "TIMESTAMP", "timeBinSize": "3600s"}]
    at_picked = "WHERE cob = (SELECT cob FROM picked)"

    def oq(sql: str) -> dict:
        return {"timeSeriesQuery": {"opsAnalyticsQuery": {"sql": sql}}}

    def tile(x, y, w, h, title, widget) -> dict:
        return {"xPos": x, "yPos": y, "width": w, "height": h, "widget": {"title": title, **widget}}

    def score(x, y, title, select, measure, thresholds=None) -> dict:
        # A SQL scorecard needs a TIMESTAMP dimension beside its number.
        sc = {**oq(base + select), "dimensions": TS, "measures": [{"column": measure, "aggregationFunction": SUM}]}
        if thresholds:
            sc["thresholds"] = thresholds
        return tile(x, y, 12, 10, title, {"scorecard": sc})

    def amber_red(amber, red) -> list:
        return [{"value": amber, "color": "YELLOW", "direction": "ABOVE"},
                {"value": red, "color": "RED", "direction": "ABOVE"}]

    tiles = [
        score(0, 0, "Failed runs — chosen COB (latest if none)",
              f"SELECT TIMESTAMP_TRUNC(MAX(ts), MINUTE) AS ts, COUNTIF(status IN {FAILED}) AS failed FROM runs {at_picked}",
              "failed", amber_red(0, 9)),
        score(12, 0, "Books processed",
              f"SELECT TIMESTAMP_TRUNC(MAX(ts), MINUTE) AS ts, COUNTIF(status IN {DONE}) AS processed FROM books", "processed"),
        score(24, 0, "Books still to process (in flight + failed + not started)",
              f"SELECT TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MINUTE) AS ts, "
              f"(SELECT COUNTIF(status NOT IN {DONE}) FROM books) + (SELECT COUNT(*) FROM not_started) AS pending",
              "pending", amber_red(0, 9)),
        score(36, 0, "Median minutes per processed book",
              f"SELECT TIMESTAMP_TRUNC(MAX(ts), MINUTE) AS ts, "
              f"APPROX_QUANTILES(TIMESTAMP_DIFF(ts, started, MINUTE), 2)[OFFSET(1)] AS median_min FROM books WHERE status IN {DONE}",
              "median_min"),
        tile(0, 10, 16, 14, "Runs by latest status", {"pieChart": {"chartType": "DONUT", "showLabels": True, "dataSets": [{
            **oq(base + f"SELECT status, COUNT(*) AS n FROM runs {at_picked} GROUP BY status"),
            "dimensions": [{"column": "status", "columnType": "STRING", "maxBinCount": 10,
                            "sortColumn": "n", "sortOrder": "SORT_ORDER_DESCENDING"}],
            "measures": [{"column": "n", "aggregationFunction": SUM}]}]}}),
        tile(16, 10, 32, 14, "Books — latest status and minutes taken (to-do first)", {"timeSeriesTable": {"dataSets": [oq(
            base + f"SELECT book, status, IF(status IN {DONE}, TIMESTAMP_DIFF(ts, started, MINUTE), NULL) AS took_min, runs, "
                   f"started AS first_event, ts AS last_update, last_error FROM books "
                   f"UNION ALL SELECT book, 'NOT STARTED', NULL, 0, NULL, NULL, NULL FROM not_started "
                   f"ORDER BY IF(status IN {DONE}, 1, 0), book LIMIT 500")]}}),
        tile(0, 24, 48, 14, "Where it failed (source × system)", {"timeSeriesTable": {"dataSets": [oq(
            base + f"SELECT cob, source, system, COUNTIF(status IN {FAILED}) AS failed_runs, "
                   f"COUNT(DISTINCT IF(status IN {FAILED}, book, NULL)) AS books_hit, COUNT(*) AS runs, "
                   f"ANY_VALUE(IF(status IN {FAILED}, error, NULL)) AS example_error FROM runs {at_picked} "
                   f"GROUP BY cob, source, system HAVING failed_runs > 0 ORDER BY failed_runs DESC LIMIT 200")]}}),
        tile(0, 38, 48, 14, "The failed lines themselves", {"logsPanel": {
            "filter": f"(logName=\"projects/{P}/logs/stdout\" OR logName=\"projects/{P}/logs/dataflow.googleapis.com%2Fworker\") "
                      f"AND jsonPayload.logger=\"{L}\" AND jsonPayload.message:\"FAILED\" AND jsonPayload.message=~\"${{COB}}\"",
            "resourceNames": [f"projects/{P}"]}}),
    ]

    def values_of(role: str) -> str:
        return (f"SELECT DISTINCT JSON_VALUE(SAFE.PARSE_JSON(JSON_VALUE(json_payload.message)).{cols[role]}) AS v "
                f"FROM `{P}.global._Default._AllLogs` WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY) "
                f"AND log_name IN {logs} AND JSON_VALUE(json_payload.logger) = '{L}'")

    filters = [{"filterType": "VALUE_ONLY", "templateVariable": "COB", "valueType": "STRING",
                **oq(f"SELECT v AS cob FROM ({values_of('date')}) WHERE v IS NOT NULL ORDER BY cob DESC LIMIT 100")}]
    filters.append({"filterType": "VALUE_ONLY", "templateVariable": "Source", "valueType": "STRING",
                    **(oq(f"SELECT v AS source FROM ({values_of('source')}) WHERE v IS NOT NULL ORDER BY source LIMIT 100")
                       if "source" in cols else {"stringArray": {"values": ["*"]}})})
    return {"displayName": "Control table — workflow-state logs", "dashboardFilters": filters,
            "mosaicLayout": {"columns": 48, "tiles": tiles}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--columns", required=True, help="role=column,… (the SUPPORT_COLUMNS form)")
    ap.add_argument("--logger", required=True, help="the logger class whose lines carry the rows")
    ap.add_argument("--done", default="COMPLETED,SUCCEEDED", help="statuses that mean a run finished well")
    ap.add_argument("--failed", default="FAILED,ERROR", help="statuses that mean a run failed")
    a = ap.parse_args()
    print(json.dumps(build(parse_columns(a.columns), a.logger, a.done, a.failed), indent=2))


if __name__ == "__main__":
    main()
