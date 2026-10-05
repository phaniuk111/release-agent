"""Find the control table's mapping by itself: only SUPPORT_TABLE is required.

A proof of concept should not start with sixteen lines of column mapping. When
SUPPORT_COLUMNS is empty, the portal reads the table's SCHEMA (metadata only —
free, no rows) and maps each role from a column's type and the words of its name
and description; the status words come from the table itself (the distinct
statuses of the last week, sorted by code into failed / done / still running).
Anything configured wins over anything discovered, role by role and list by
list, so a guess that is wrong is fixed with one line, never by switching
discovery off. /api/support/config-check shows what was decided.

    propose(schema)         -> {"roles", "unmapped", "missing"}   PURE (also used by
                               scripts/support_mapping.py to draft the chart block)
    mapping()               -> (columns, labels, how)   configured, else discovered
    classify_status(word)   -> "failed" | "done" | None (still running)

No regex: names are split into words by case and separators.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from ...config import settings
from .config import REQUIRED_ROLES, ConfigError, parse_columns, parse_table

# role → rules. A rule is (words that must ALL be among the column's NAME
# words, types allowed or () for any). First column to satisfy a rule takes the
# role; rules are tried in order, so the stricter ones come first.
_NAME_RULES: dict[str, list[tuple[tuple[str, ...], tuple[str, ...]]]] = {
    "run_id": [(("run", "id"), ()), (("execution", "id"), ()), (("workflow", "id"), ())],
    "status": [(("status",), ()), (("state",), ())],
    "updated_at": [(("insert",), ("TIMESTAMP", "DATETIME")), (("inserted",), ("TIMESTAMP", "DATETIME")),
                   (("loaded",), ("TIMESTAMP", "DATETIME")), (("updated",), ("TIMESTAMP", "DATETIME")),
                   (("update",), ("TIMESTAMP", "DATETIME")), (("modified",), ("TIMESTAMP", "DATETIME")),
                   (("written",), ("TIMESTAMP", "DATETIME")), (("created",), ("TIMESTAMP", "DATETIME"))],
    "event_at": [(("event",), ("TIMESTAMP", "DATETIME")), (("received",), ("TIMESTAMP", "DATETIME"))],
    "event_id": [(("event", "id"), ()), (("event", "key"), ()), (("trigger", "id"), ())],
    "job_id": [(("job", "id"), ()), (("job", "ref"), ())],
    "error": [(("error", "message"), ()), (("error", "msg"), ()), (("err", "msg"), ()), (("error",), ())],
    "details": [(("error", "details"), ()), (("err", "details"), ()), (("details",), ())],
    "members": [(("member", "count"), ()), (("row", "count"), ()), (("record", "count"), ()),
                (("rows",), ()), (("count",), ())],
}
# Grouping roles: words looked for in the NAME first, then in the DESCRIPTION.
# These are proposals — the person confirms each (see the module note).
_GROUP_RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "source": (("source", "fetcher", "producer"), ("source of data", "source of the data", "fetcher")),
    "process": (("report", "process", "pipeline", "workflow"), ("type of report", "type of process", "process being run")),
    "unit": (("book", "account", "acct", "portfolio", "unit", "customer", "tenant"), ()),
    "scope": (("entity", "region", "desk", "division"), ("legal entity",)),
    "system": (("system", "feed", "upstream"), ("system name", "capture system", "upstream system")),
}
_ORDER = ("date", "run_id", "status", "updated_at", "event_at", "event_id", "job_id", "error", "details", "members",
          "system", "source", "process", "unit", "scope")


def name_words(name: str) -> list[str]:
    """`pipelineJobId` / `pipeline_job_id` → ["pipeline", "job", "id"]."""
    words: list[str] = []
    cur = ""
    for ch in name:
        if not ch.isalnum():
            if cur:
                words.append(cur)
            cur = ""
        elif ch.isupper() and cur and not cur[-1].isupper():
            words.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        words.append(cur)
    return [w.lower() for w in words]


def _columns(schema: Any) -> list[dict[str, str]]:
    fields = schema.get("fields") if isinstance(schema, dict) else schema
    out = []
    for f in fields if isinstance(fields, list) else []:
        if isinstance(f, dict) and f.get("name"):
            out.append({"name": str(f["name"]), "type": str(f.get("type") or "").upper(),
                        "description": str(f.get("description") or "").strip()})
    return out


def propose(schema: Any) -> dict[str, Any]:
    """{"roles": {role: {"column", "why", "confirm": bool}}, "unmapped": [columns], "missing": [required roles]}."""
    cols = _columns(schema)
    taken: set[str] = set()
    roles: dict[str, dict[str, Any]] = {}

    def take(role: str, col: dict[str, str], why: str, confirm: bool = False) -> None:
        roles[role] = {"column": col["name"], "why": why, "confirm": confirm, "description": col["description"]}
        taken.add(col["name"])

    dates = [c for c in cols if c["type"] == "DATE"]
    if dates:
        partition = [c for c in dates if "partition" in c["description"].lower()]
        best = (partition or dates)[0]
        take("date", best, "the DATE column" + (" described as the partition key" if partition else ""),
             confirm=len(dates) > 1 and not partition)

    for role, rules in _NAME_RULES.items():
        for wanted, types in rules:
            hit = next((c for c in cols if c["name"] not in taken and all(w in name_words(c["name"]) for w in wanted)
                        and (not types or c["type"] in types)), None)
            if hit:
                take(role, hit, "its name" + (f" and type {hit['type']}" if types else ""))
                break

    for role, (in_name, in_description) in _GROUP_RULES.items():
        by_name = next((c for c in cols if c["name"] not in taken
                        and any(w in name_words(c["name"]) for w in in_name)
                        and "name" not in name_words(c["name"])[-1:]), None)
        by_text = next((c for c in cols if c["name"] not in taken
                        and any(p in c["description"].lower() for p in in_description)), None)
        hit = by_name or by_text
        if hit:
            take(role, hit, "its name" if hit is by_name else "its description", confirm=True)

    return {"roles": {r: roles[r] for r in _ORDER if r in roles},
            "unmapped": [c for c in cols if c["name"] not in taken],
            "missing": [r for r in ("date", "run_id", "status") if r not in roles]}


# ----- labels and the date's name ----------------------------------------------------

_DROP = frozenset({"id", "code", "name", "key", "cd"})


def label_of(column: str) -> str:
    """accountId → "Account", regionCode → "Region", jobKind → "Job kind"."""
    words = name_words(column)
    kept = [w for w in words if w not in _DROP] or words
    text = " ".join(kept)
    return text[:1].upper() + text[1:]


def date_label(description: str) -> str | None:
    """What the team calls the business date, when its description says so."""
    d = (description or "").lower()
    if "close of business" in d or "cob" in name_words(d):
        return "COB"
    return None


# ----- statuses ------------------------------------------------------------------------

_FAILED_PARTS = ("FAIL", "ERROR", "ABORT", "REJECT", "TIMEOUT", "TIMEDOUT", "KILLED", "CRASH", "CANCEL", "DEAD")
_DONE_PARTS = ("SUCCE", "COMPLET", "DONE", "FINISH", "PROCESSED")


def classify_status(word: str) -> str | None:
    """A status word → failed, done, or None for still running / unknown."""
    w = (word or "").strip().upper()
    if not w:
        return None
    if any(p in w for p in _FAILED_PARTS):
        return "failed"
    if any(p in w for p in _DONE_PARTS) or w == "OK":
        return "done"
    return None


# ----- the mapping, cached ----------------------------------------------------------------

_TTL = 3600.0
_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def _remember(key: str, compute):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
    value = compute()
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def clear() -> None:
    with _lock:
        _cache.clear()


def _schema(table_ref: str) -> list[dict[str, str]]:
    from .queries import _get_client

    t = _get_client().get_table(table_ref.strip("`"))
    return [{"name": f.name, "type": f.field_type, "description": f.description or ""} for f in t.schema]


def discovered(table_ref: str) -> dict[str, Any]:
    """The schema read once an hour per table: {columns, labels, date_label,
    unmapped, missing} — never raises; a failure is {"error"}."""
    def compute():
        try:
            schema = _schema(table_ref)
        except Exception as e:  # noqa: BLE001 — BigQuery's text is the useful part
            return {"error": f"{type(e).__name__}: {str(e)[:300]}"}
        prop = propose(schema)
        roles = prop["roles"]
        columns = {r: hit["column"] for r, hit in roles.items()}
        labels = {r: label_of(hit["column"]) for r, hit in roles.items()}
        date_desc = roles.get("date", {}).get("description", "")
        return {"columns": columns, "labels": labels, "date_label": date_label(date_desc),
                "unmapped": [c["name"] for c in prop["unmapped"]], "missing": prop["missing"],
                "confirm": [r for r, hit in roles.items() if hit["confirm"]]}
    return _remember(f"schema:{table_ref}", compute)


def mapping() -> tuple[dict[str, str], dict[str, str], str]:
    """(columns, labels, "configured" | "discovered"). ConfigError when neither
    gives the three required roles."""
    if settings.support_columns.strip():
        columns, labels = parse_columns(settings.support_columns)
        return columns, labels, "configured"
    table_ref = parse_table(settings.support_table)
    found = discovered(table_ref)
    if found.get("error"):
        raise ConfigError(f"SUPPORT_COLUMNS is empty and the table's schema could not be read: {found['error']}")
    missing = [r for r in REQUIRED_ROLES if r not in found["columns"]]
    if missing:
        raise ConfigError(f"SUPPORT_COLUMNS is empty and no column of the table looks like the "
                          f"{', '.join(missing)} — map {'it' if len(missing) == 1 else 'them'} in SUPPORT_COLUMNS.")
    return dict(found["columns"]), dict(found["labels"]), "discovered"


DEFAULT_FAILED = ("FAILED", "ERROR")
DEFAULT_DONE = ("SUCCEEDED", "SUCCESS", "COMPLETED", "COMPLETE", "DONE")
DEFAULT_DATE_LABEL = "Business date"


def configured(*names: str) -> bool:
    """Was any of these settings given a value (env / chart)? An empty one —
    the chart ships them empty — counts as not given: discover, else default."""
    return any(n in settings.model_fields_set and str(getattr(settings, n, "") or "").strip() for n in names)


def label_for_date() -> str:
    """SUPPORT_DATE_LABEL when set; else what the date column's description calls it."""
    label = (settings.support_date_label or "").strip()
    if configured("support_date_label"):
        return label
    if settings.support_columns.strip():
        return label or DEFAULT_DATE_LABEL
    try:
        found = discovered(parse_table(settings.support_table))
    except ConfigError:
        return label or DEFAULT_DATE_LABEL
    return found.get("date_label") or label or DEFAULT_DATE_LABEL
