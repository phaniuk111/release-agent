#!/usr/bin/env python3
"""Draft the support-triage column mapping from a table's schema.

Support triage reads a workflow control table through ROLES (date, run_id,
status, …; release_agent.tools.support_triage.ROLES). Writing that mapping by
hand for a 17-column table is where typos happen, so this drafts the
`supportTable:` block of the chart's values.yaml from the schema:

    support_mapping.py --schema path/to/schema.json      a BigQuery schema file
    support_mapping.py --table project.dataset.table     read it from BigQuery

It is a DRAFT for a person to review, once: the obvious roles are settled by a
column's type and the words of its name and description; the grouping roles
(system / source / process / unit / scope) decide how failures are categorised,
so each proposal there carries the description it was made from and a
"confirm" mark. Nothing here runs when the portal runs — the reviewed mapping
in values.yaml is what it uses.

No regex: names are split into words by case and separators.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# role → rules. A rule is (words that must ALL be among the column's NAME
# words, types allowed or () for any). First column to satisfy a rule takes the
# role; rules are tried in order, so the stricter ones come first.
_NAME_RULES: dict[str, list[tuple[tuple[str, ...], tuple[str, ...]]]] = {
    "run_id": [(("run", "id"), ()), (("execution", "id"), ()), (("workflow", "id"), ())],
    "status": [(("status",), ()), (("state",), ())],
    "updated_at": [(("insert",), ("TIMESTAMP", "DATETIME")), (("updated",), ("TIMESTAMP", "DATETIME")),
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


def render(proposal: dict[str, Any], table: str = "") -> str:
    """The `supportTable:` block for values.yaml, with the review notes as comments."""
    lines = ["supportTable:",
             f'  table: "{table or "<project>.<dataset>.<table>"}"',
             "  dateLabel: Business date          # what the team calls it, e.g. COB",
             "  columns:"]
    width = max((len(r) for r in proposal["roles"]), default=4) + 1
    for role, hit in proposal["roles"].items():
        note = "CONFIRM — " if hit["confirm"] else ""
        about = f' ("{hit["description"][:70]}")' if hit["confirm"] and hit["description"] else ""
        lines.append(f"    {role + ':':<{width}} {{ column: {hit['column']} }}   # {note}by {hit['why']}{about}")
    for role in proposal["missing"]:
        lines.append(f"    {role + ':':<{width}} {{ column: <REQUIRED — no column matched> }}")
    lines += ["  statuses:",
              "    failed: [FAILED, ERROR]          # the values your status column really takes",
              "    done:   [SUCCEEDED, COMPLETED]",
              "  owners:",
              "    default: L2 support"]
    if proposal["unmapped"]:
        lines += ["", "# Not mapped (no check uses them — fine to leave):"]
        lines += [f"#   {c['name']} ({c['type']})" + (f" — {c['description'][:80]}" if c["description"] else "")
                  for c in proposal["unmapped"]]
    groups = [r for r in ("system", "source", "process", "unit", "scope") if r in proposal["roles"]]
    lines += ["",
              "# The grouping roles decide the category of a failure — check each:",
              "#   system  = the upstream system the data comes from   (many units failing → \"upstream\")",
              "#   source  = the component/image that fetched the data (joined to the release log)",
              "#   process = the report or job type                    (many units failing → \"process\")",
              "#   unit    = what one run is for (a book, an account)  (one unit failing → \"unit\")",
              "#   scope   = a wider grouping of units (an entity)",
              f"# proposed here: {', '.join(groups) or 'none — assign them by hand'}"]
    return "\n".join(lines) + "\n"


def _schema_from_table(table: str) -> list[dict[str, str]]:
    from google.cloud import bigquery

    t = bigquery.Client().get_table(table.replace("`", ""))
    return [{"name": f.name, "type": f.field_type, "description": f.description or ""} for f in t.schema]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--schema", help="a BigQuery schema JSON file (a list of {name, type, description})")
    ap.add_argument("--table", help="project.dataset.table — read the schema from BigQuery")
    args = ap.parse_args(argv)
    if bool(args.schema) == bool(args.table):
        ap.error("give exactly one of --schema or --table")
    if args.schema:
        with open(args.schema, encoding="utf-8") as f:
            schema = json.load(f)
    else:
        schema = _schema_from_table(args.table)
    proposal = propose(schema)
    sys.stdout.write(render(proposal, args.table or ""))
    return 1 if proposal["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
