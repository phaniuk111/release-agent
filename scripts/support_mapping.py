#!/usr/bin/env python3
"""Draft the support-triage column mapping from a table's schema.

Support triage reads a workflow control table through ROLES (date, run_id,
status, …; release_agent.tools.support.config.ROLES). Writing that mapping by
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
import pathlib
import sys
from typing import Any

# The matching rules live in the package: the portal maps a table by itself
# when SUPPORT_COLUMNS is empty, and this script drafts the same proposal for
# the chart. One implementation.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from release_agent.tools.support.discover import name_words, propose  # noqa: E402,F401

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
