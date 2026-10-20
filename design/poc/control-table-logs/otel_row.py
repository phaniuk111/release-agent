"""A control-table row through an OpenTelemetry Collector you do not own.

A cluster's shared Collector (Google-built or not) usually forwards OTLP
logs to Cloud Logging as they come; it will not add a transform for one
team. So the producer makes the entry right at the source: the row IS the
log body (a map, which the Google exporter writes as the JSON payload) and
one attribute names the log. No Collector change, no logging bridge — so
the role name ``process`` is fine here (the stdlib bridge reserves it).

    from otel_row import row_logger, emit_row
    logger = row_logger(service="pnl-pipeline", endpoint="otel-collector.observability:4317")   # once
    emit_row(logger, row)                                                                        # per row

Needs opentelemetry-sdk and opentelemetry-exporter-otlp-proto-grpc. The
Collector needs an OTLP receiver and a logs pipeline ending in the
``googlecloud`` exporter — check its ConfigMap for ``pipelines: logs:``.
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any

LOG_NAME = "control-table"
KIND = "control-table-row"


def row_logger(service: str, endpoint: str, insecure: bool = True) -> Any:
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    provider = LoggerProvider(resource=Resource.create({"service.name": service}))
    provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=insecure)))
    logger = provider.get_logger("control-table")
    logger.provider = provider          # for flush() at shutdown
    return logger


def emit_row(logger: Any, row: dict[str, Any]) -> None:
    """One row, as it is written. Never raises into the pipeline."""
    try:
        from opentelemetry._logs import SeverityNumber

        body = {**row, "kind": KIND}
        for key in ("event_at", "updated_at"):
            when = row.get(key)
            if isinstance(when, dt.datetime):
                body[key] = (when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
                             ).isoformat().replace("+00:00", "Z")
        logger.emit(timestamp=time.time_ns(), severity_number=SeverityNumber.INFO, severity_text="INFO",
                    body=body, attributes={"gcp.log_name": LOG_NAME})
    except Exception:  # noqa: BLE001 — see docstring
        pass


def flush(logger: Any) -> None:
    try:
        logger.provider.force_flush()
    except Exception:  # noqa: BLE001
        pass
