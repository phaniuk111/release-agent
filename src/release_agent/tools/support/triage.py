"""Support triage: a first look at a workflow control table.

Many pipelines log one row per run (or per status change) in a control table:
a business date, a run id, a status, often an error message and a job id. This
reads that table — READ-ONLY, a few fixed statements through tools/bq_guard.py,
in which BigQuery does the counting and only problems come back — and
answers what a person on support asks first:

    what failed for this business date, and how widely (one upstream system?
    one process everywhere? one unit?) · the distinct errors behind it · new or
    recurring, and when it last worked · retried already? · stuck runs ·
    work that ran on the previous date but not this one · output volume that
    collapsed on a run that "succeeded".

Nothing here knows any real table. The table, its columns and its status words
are configuration (SUPPORT_TABLE / SUPPORT_COLUMNS / SUPPORT_*_STATUSES), kept
in a private values file or .env — so this module, its tests and its docs carry
only the generic ROLES (config.py).

    triage(business_date="") -> the report dict (the card renders it as is),
                                with `incidents`: the L1 view — per problem a
                                priority, known-issue match, one action (wait /
                                retrigger / check / escalate), steps, an owner
                                and a ticket note
    for_model(report)        -> the same, without error text unless
                                SUPPORT_ERRORS_TO_MODEL (the team's data)
    Query(table, columns)    -> the statements (BigQuery counts; problems only
                                come back), each read through bq_guard
    build_report(...)        -> PURE: those results → the report
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...config import settings
from . import queries
from .config import DIMENSIONS, ConfigError, _disabled, _names, _permission_hint, enabled, parse_columns, parse_table
from .incidents import PriorityRules, incidents, load_runbook, parse_critical, parse_owners
from .report import _to_date, build_report


def triage(business_date: str = "") -> dict[str, Any]:
    """Read the control table for ``business_date`` (ISO; empty = the latest
    date in the table up to today) plus the lookback window before it, and
    analyse it. Errors come back as {"ok": False, "error": …} — never raised."""
    if not enabled():
        return _disabled()
    try:
        table_ref = parse_table(settings.support_table)
        columns, labels = parse_columns(settings.support_columns)
    except ConfigError as e:
        return {"ok": False, "error": str(e)}
    wanted = None
    if business_date.strip():
        wanted = _to_date(business_date.strip())
        if wanted is None:
            return {"ok": False, "error": f"{business_date!r} is not a date (use YYYY-MM-DD)."}
    now = datetime.now(timezone.utc)
    try:
        got = queries._gather(table_ref, columns, wanted, now)
    except Exception as e:  # BigQuery's own errors carry the useful text
        msg = f"{type(e).__name__}: {str(e)[:300]}"
        out = {"ok": False, "error": msg}
        hint = _permission_hint(msg)
        if hint:
            out["hint"] = hint
        return out
    report = build_report(roles=list(columns), cob=got["cob"], previous=got["previous"], dates=got["dates"],
                          rows=got["rows"], now=now, detail_keys=_names(settings.support_detail_keys),
                          errors=got["errors"])
    start, end = got["window"]
    out = {"ok": True, **report,
           "date_label": settings.support_date_label,
           "labels": {r: labels[r] for r in DIMENSIONS if r in labels},
           "window": {"start": start.isoformat(), "end": end.isoformat()},
           "bytes_processed": got["budget"].bytes,
           "queries": got["budget"].queries,
           "scanned_at": now.replace(microsecond=0).isoformat(),
           "job_url": settings.support_job_url}
    runbook, problem = load_runbook(settings.support_runbook_file)
    if problem:
        out["notes"].append(problem)
    out["runbook_entries"] = len(runbook)
    rules = PriorityRules(high_count=settings.support_high_count, medium_count=settings.support_medium_count,
                          critical=parse_critical(settings.support_critical))
    out["incidents"] = incidents(out, labels=out["labels"], runbook=runbook,
                                 owners=parse_owners(settings.support_owners), rules=rules)
    return out


def for_model(report: dict[str, Any]) -> dict[str, Any]:
    """What the chat model may see. Without SUPPORT_ERRORS_TO_MODEL the error
    text is the team's data and stays out: errors become "error #n" with their
    counts, categories and spread — the card still shows the text."""
    if not report.get("ok"):
        return report
    out = dict(report)
    if settings.support_errors_to_model:
        out["incidents"] = [{k: v for k, v in inc.items() if k != "note_without_error"}
                            for inc in report.get("incidents") or []]
        return out
    out["errors"] = [{**e, "signature": f"error #{i + 1}", "sample": None}
                     for i, e in enumerate(report.get("errors") or [])]
    out["incidents"] = [{**{k: v for k, v in inc.items() if k not in ("note_without_error", "error_text")},
                         "note": inc["note_without_error"]}
                        for inc in report.get("incidents") or []]
    out["error_text"] = "withheld (SUPPORT_ERRORS_TO_MODEL is off) — the Support triage card shows it"
    out.pop("job_url", None)
    return out
