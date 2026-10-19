#!/usr/bin/env python3
"""PoC: the control table's rows as structured JSON logs in Cloud Logging.

Each run row becomes one log entry whose jsonPayload carries the agent's ROLE
names (date, run_id, status, system, source, process, unit, scope, job_id,
error, members, event_id — tools/support/config.py), never a real column name,
so the same query and dashboard work for any deployment's table. Log Analytics
then answers "what failed today, where, how widely" in SQL (query.sql) and the
dashboard (dashboard.json) draws it.

    python3 log_rows.py --project <id> [--log control-table] [--days 2] [--rows 40]
    python3 log_rows.py --project <id> --from rows.jsonl      # your own rows

Writes through the Logging API's entries:write with the token from
`gcloud auth print-access-token` — stdlib only, one request per 100 rows.
With --from, each JSON line is one row already keyed by role. Rows are
generated deterministically (seed) so a rerun is the same picture.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

SYSTEMS = ["TRADE-A", "TRADE-B", "TRADE-C"]
SOURCES = ["risk-fetcher", "pnl-fetcher", "positions-fetcher"]
PROCESSES = ["REPORT-RISK", "REPORT-PNL", "REPORT-POS"]
UNITS = [f"BOOK-{n:03d}" for n in range(1, 13)]
SCOPES = ["ENTITY-UK", "ENTITY-DE", "ENTITY-SG"]
ERRORS = [
    ("Dataflow job failed: worker ran out of memory", "RESOURCE"),
    ("Upstream feed returned 0 rows", "DATA"),
    ("Timeout waiting for the market data service", "UPSTREAM"),
]


def synthetic(days: int, rows: int, seed: int = 7) -> list[dict]:
    """Rows over the last ``days`` business dates: mostly done, a stuck few,
    and a cluster of failures sharing one source on the latest date — the
    shape the dashboard should make obvious."""
    rnd = random.Random(seed)
    today = dt.date.today()
    out: list[dict] = []
    for d in range(days):
        date = (today - dt.timedelta(days=d)).isoformat()
        # The latest date is still in flight: its last book has not started yet,
        # so "still to process" has something to count.
        units = UNITS[:-1] if d == 0 else UNITS
        for i in range(rows):
            system, source = rnd.choice(SYSTEMS), rnd.choice(SOURCES)
            row = {
                "date": date, "run_id": str(uuid.UUID(int=rnd.getrandbits(128))),
                "event_id": f"EVT-{date}-{i:04d}", "system": system, "source": source,
                "process": rnd.choice(PROCESSES), "unit": rnd.choice(units), "scope": rnd.choice(SCOPES),
                "job_id": f"2026-{rnd.getrandbits(32):08x}", "members": rnd.randint(100, 5000),
                "status": "SUCCEEDED", "error": "", "error_kind": "",
            }
            r = rnd.random()
            if d == 0 and source == "risk-fetcher" and r < 0.6:      # today's cluster
                row.update(status="FAILED", error=ERRORS[0][0], error_kind=ERRORS[0][1], members=0)
            elif r < 0.05:
                msg, kind = rnd.choice(ERRORS[1:])
                row.update(status="FAILED", error=msg, error_kind=kind, members=0)
            elif r < 0.09:
                row.update(status="RUNNING", members=0)
            out.append(row)
    return out


def entries(rows: list[dict], project: str, log: str, spread_minutes: float = 0.0) -> list[dict]:
    """One entry per row. Timestamps are NOW (a second apart) unless
    ``spread_minutes`` backdates them: Log Analytics indexes an entry by its
    own timestamp and shows nothing stamped before the bucket's upgrade, so a
    backdated batch can be read by Logs Explorer and invisible to SQL."""
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for n, row in enumerate(rows):
        failed = row.get("status") in ("FAILED", "ERROR")
        payload = {**row, "failed": failed, "kind": "control-table-row"}
        out.append({
            "logName": f"projects/{project}/logs/{log}",
            "resource": {"type": "global", "labels": {"project_id": project}},
            # INFO for every row, as the pipeline logs them: what failed is in the
            # row's status, and nothing downstream reads the severity.
            "severity": "INFO",
            "jsonPayload": payload,
            "timestamp": (now - dt.timedelta(seconds=(spread_minutes * 60 or 1) * (len(rows) - n) / len(rows))
                          ).isoformat().replace("+00:00", "Z"),
            "labels": {"poc": "control-table"},
        })
    return out


def write(batch: list[dict], token: str) -> None:
    """One entries:write call; a 429 (the per-minute write quota) or a 5xx is
    retried with a growing pause, so a big load slows down rather than fails."""
    req = urllib.request.Request(
        "https://logging.googleapis.com/v2/entries:write",
        data=json.dumps({"entries": batch, "partialSuccess": False}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST")
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 5:
                raise
            time.sleep(2 ** attempt)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--log", default="control-table")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--rows", type=int, default=40, help="rows per business date")
    ap.add_argument("--from", dest="source", help="JSON lines of rows keyed by role")
    ap.add_argument("--spread-minutes", type=float, default=0.0,
                    help="backdate the batch over this many minutes (see entries(); 0 = now)")
    ap.add_argument("--batch", type=int, default=500, help="entries per API call")
    ap.add_argument("--workers", type=int, default=8, help="parallel API calls")
    a = ap.parse_args()
    if a.source:
        rows = [json.loads(line) for line in open(a.source) if line.strip()]
    else:
        rows = synthetic(a.days, a.rows)
    token = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True,
                           check=True).stdout.strip()
    todo = entries(rows, a.project, a.log, a.spread_minutes)
    started = time.monotonic()
    batches = [todo[i:i + a.batch] for i in range(0, len(todo), a.batch)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
        for _ in pool.map(lambda b: write(b, token), batches):
            pass
    failed = sum(1 for r in rows if r.get("status") in ("FAILED", "ERROR"))
    print(f"wrote {len(todo)} entries to projects/{a.project}/logs/{a.log} "
          f"({failed} failed rows) in {time.monotonic() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
