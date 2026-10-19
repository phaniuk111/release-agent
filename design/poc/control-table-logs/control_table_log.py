"""One writer for control-table rows, for every service that produces them.

A GKE service and a Dataflow pipeline both write rows. Scraped stdout differs
by platform (GKE parses JSON into jsonPayload under the platform's log name;
a Dataflow worker's logging lands as a string inside jsonPayload.message), so
instead both call this: the Cloud Logging API directly, one log name, one
payload shape, the row's own timestamp. The dashboard then needs no
per-platform branch, and `resource`/`labels` still say which service wrote
the row.

    from control_table_log import ControlTableLog
    log = ControlTableLog(service="risk-fetcher")          # once per process / DoFn.setup()
    log.row({"date": "2026-10-19", "run_id": "…", "status": "FAILED", ...})   # per row
    log.flush()                                             # before the process exits

Rows are keyed by the agent's ROLE names (tools/support/config.py): date,
run_id, status, event_at, updated_at, event_id, job_id, error, details,
members, system, source, process, unit, scope. Writes go through the
client's background transport: batched, off the caller's thread, retried.
Needs google-cloud-logging.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import google.cloud.logging
from google.cloud.logging_v2.resource import Resource

LOG_NAME = "control-table"
KIND = "control-table-row"
_DONE = ("SUCCEEDED", "COMPLETED")
_FAILED = ("FAILED", "ERROR")


def _ts(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, str) and value:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


class ControlTableLog:
    def __init__(self, service: str, project: str | None = None, client: Any = None) -> None:
        self.service = service
        self.client = client or google.cloud.logging.Client(project=project)
        self.logger = self.client.logger(LOG_NAME)
        # "global" on purpose: the same resource from a pod and from a Dataflow
        # worker, so one query reads both; the service name is a label.
        self.resource = Resource(type="global", labels={"project_id": self.client.project})

    def row(self, row: dict[str, Any]) -> None:
        """Log one control-table row as it is written. Never raises into the
        pipeline: a logging failure is not a reason to fail the run."""
        try:
            status = str(row.get("status") or "")
            payload = {**row, "kind": KIND, "failed": status in _FAILED, "done": status in _DONE}
            for key in ("event_at", "updated_at"):
                when = _ts(row.get(key))
                if when is not None:
                    payload[key] = when.isoformat().replace("+00:00", "Z")
            # The entry's timestamp is the row's write time, so Log Analytics
            # orders and bins by when the status really changed.
            stamp = _ts(row.get("updated_at")) or dt.datetime.now(dt.timezone.utc)
            self.logger.log_struct(payload, severity="INFO", timestamp=stamp, resource=self.resource,
                                   labels={"service": self.service, "kind": KIND})
        except Exception:  # noqa: BLE001 — see docstring
            pass

    def flush(self) -> None:
        try:
            self.client.flush_handlers()
        except Exception:  # noqa: BLE001
            pass
