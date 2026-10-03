"""The control table and how its columns map onto roles.

The triage logic never names a column: it works on ROLES (the business date, a
run, its status, the upstream system, the unit …) and this module turns the
configured mapping (SUPPORT_TABLE / SUPPORT_COLUMNS, or the chart's
`supportTable:` block) into what the queries use — checked character by
character, because it is written into SQL.
"""
from __future__ import annotations

from typing import Any

from ...config import settings

# role -> what it means. date, run_id and status are required; every other role
# is optional and only switches on the checks that need it.
ROLES: dict[str, str] = {
    "date": "the business date of the run (a DATE column — the table's partition key, ideally)",
    "run_id": "one execution of the workflow",
    "status": "the run's current status",
    "updated_at": "when the row was written — orders a run's rows, ages stuck runs",
    "event_at": "when the triggering event arrived",
    "event_id": "the business event that triggered the run — retries share it",
    "job_id": "the job the run started (e.g. a Dataflow job)",
    "error": "a human-readable error message",
    "details": "structured error details (JSON)",
    "members": "how many members/rows the run produced",
    "system": "the upstream system the data comes from",
    "source": "the component or feed that fetched the data",
    "process": "the report type or process being run",
    "unit": "the unit the run is for (a book, an account, a portfolio)",
    "scope": "a wider grouping of units (an entity, a region)",
}
REQUIRED_ROLES = ("date", "run_id", "status")
# Grouping roles, in the order a pattern is reported: an upstream system first
# (the widest likely cause), a single unit last (the narrowest).
DIMENSIONS = ("system", "source", "process", "scope", "unit")
# The identity of a piece of work across dates (event ids and run ids change).
_KEY_ROLES = ("system", "source", "process", "unit", "scope")

# How many rows any one list in a report carries (stuck, missing, retries …).
_MAX_LIST = 50

_IDENT = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_PROJECT_CHARS = _IDENT | frozenset("-")


class ConfigError(ValueError):
    """SUPPORT_TABLE / SUPPORT_COLUMNS cannot be used as given."""


# ----- configuration ------------------------------------------------------------

def _names(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _is_column(name: str) -> bool:
    return bool(name) and not name[0].isdigit() and all(c in _IDENT for c in name)


def parse_table(raw: str) -> str:
    """'project.dataset.table' (each part optionally backticked) → the quoted
    reference the query uses. Every part is checked character by character —
    the reference is written into the SQL, so nothing but BigQuery's own
    identifier characters may reach it."""
    text = (raw or "").replace("`", "").strip()
    parts = text.split(".")
    if len(parts) != 3 or not all(parts):
        raise ConfigError("SUPPORT_TABLE must be 'project.dataset.table'.")
    project, dataset, table = parts
    if not all(c in _PROJECT_CHARS for c in project):
        raise ConfigError("SUPPORT_TABLE: the project id has characters BigQuery does not allow.")
    if not all(c in _IDENT for c in dataset):
        raise ConfigError("SUPPORT_TABLE: the dataset id has characters BigQuery does not allow.")
    if not all(c in _PROJECT_CHARS for c in table):
        raise ConfigError("SUPPORT_TABLE: the table name has characters this tool does not accept.")
    return f"`{project}.{dataset}.{table}`"


def parse_columns(raw: str) -> tuple[dict[str, str], dict[str, str]]:
    """'role=column[:Label],…' → ({role: column}, {role: label}). Unknown roles,
    a column named twice for one role, or a column name that is not a plain
    identifier are refused; the three required roles must be there."""
    columns: dict[str, str] = {}
    labels: dict[str, str] = {}
    for item in _names(raw):
        role, eq, rest = item.partition("=")
        role = role.strip()
        column, _, label = rest.partition(":")
        column = column.strip()
        if not eq or role not in ROLES:
            raise ConfigError(f"SUPPORT_COLUMNS: unknown role {role!r} — use one of {', '.join(ROLES)}.")
        if role in columns:
            raise ConfigError(f"SUPPORT_COLUMNS: role {role!r} is mapped twice.")
        if not _is_column(column):
            raise ConfigError(f"SUPPORT_COLUMNS: {role}= needs a plain column name (letters, digits, underscore).")
        columns[role] = column
        labels[role] = label.strip() or column
    missing = [r for r in REQUIRED_ROLES if r not in columns]
    if missing:
        raise ConfigError(f"SUPPORT_COLUMNS: map the required role(s) {', '.join(missing)}.")
    return columns, labels


def enabled() -> bool:
    return bool(settings.support_table.strip())


def data_project() -> str:
    """The project the pipelines' DATA lives in — where the control table is,
    and so where its Dataflow jobs run and where a denial on its tables or
    buckets is audited. Read off SUPPORT_TABLE itself, so a deployment whose
    services run in one project and whose data lives in another names the data
    project once (in the table), not once per tool. Falls back to BQ_PROJECT,
    then the main project."""
    try:
        return parse_table(settings.support_table).strip("`").split(".", 1)[0]
    except ConfigError:
        return (settings.bq_project or settings.gcp_project or "").strip()


def _disabled() -> dict[str, Any]:
    return {"ok": False, "disabled": True,
            "error": "Support triage is disabled (SUPPORT_TABLE unset)."}


def _permission_hint(error: str) -> str | None:
    low = error.lower()
    if "access denied" in low or "permission" in low or "403" in low:
        return ("This account needs BigQuery Data Viewer on the control table and BigQuery "
                "Job User on the project the query runs in (SUPPORT_PROJECT).")
    return None
