"""Control-table rows from plain ``log.info``, on GKE and on Dataflow alike.

Both platforms produce rows. Call :func:`setup` once per process (a GKE
service at start-up, a Dataflow step in ``DoFn.setup()``), then log each row
with the standard library:

    import logging
    from control_table_log import setup, row_fields
    setup(service="risk-fetcher")                      # once
    log = logging.getLogger(__name__)
    log.info("control-table row", extra=row_fields(row))   # per row, as it is written

``setup`` attaches Google's Cloud Logging handler, which sends through the
Logging API — not stdout. That is what makes the two platforms the same:
GKE would parse a printed JSON line but under its own log name, and a
Dataflow worker's own logging lands the row as a STRING inside
``jsonPayload.message``; the handler bypasses both and writes log
``control-table`` with the row as the JSON payload from either place.
``row_fields`` keys the row by the agent's ROLE names (tools/support/config.py)
plus the ``kind`` marker every query filters on. The entry's timestamp is the
moment of the call, so log the row when it is written. Flush before a
worker exits (``teardown``). Needs google-cloud-logging; the worker's service
account needs roles/logging.logWriter.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

LOG_NAME = "control-table"
KIND = "control-table-row"
_DONE = ("SUCCEEDED", "COMPLETED")
_FAILED = ("FAILED", "ERROR")
_handler: Any = None


def setup(service: str, project: str | None = None, level: int = logging.INFO) -> None:
    """Attach the Cloud Logging handler to the root logger, once."""
    global _handler
    if _handler is not None:
        return
    import google.cloud.logging
    from google.cloud.logging.handlers import CloudLoggingHandler

    client = google.cloud.logging.Client(project=project)
    _handler = CloudLoggingHandler(client, name=LOG_NAME, labels={"service": service, "kind": KIND})
    root = logging.getLogger()
    root.addHandler(_handler)
    if root.level > level:
        root.setLevel(level)


def row_fields(row: dict[str, Any]) -> dict[str, Any]:
    """The ``extra`` for one row: its fields as the JSON payload."""
    status = str(row.get("status") or "")
    payload = {**row, "kind": KIND, "failed": status in _FAILED, "done": status in _DONE}
    for key in ("event_at", "updated_at"):
        when = row.get(key)
        if isinstance(when, dt.datetime):
            payload[key] = (when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
                            ).isoformat().replace("+00:00", "Z")
    return {"json_fields": payload}


def flush() -> None:
    """Send what is still queued — call before a process or worker exits."""
    if _handler is not None:
        _handler.flush()
