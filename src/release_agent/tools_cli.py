"""
No-LLM tool runner — exercise every Release Copilot tool directly.

This imports ONLY the tool layer (PyGithub), never the LangGraph agent or the
Vertex/Gemini LLM, so it runs with zero model config. Use it to test or demo all
tools without a chat model.

Usage:
    # list every tool with its arguments
    python -m release_agent.tools_cli

    # show one tool's full schema
    python -m release_agent.tools_cli get_build_controls

    # run a tool — JSON args
    python -m release_agent.tools_cli list_allowed_images
    python -m release_agent.tools_cli get_build_controls '{"image":"payments-api","tag":"v1.5.0"}'

    # run a tool — key=value args (convenience; ints/bools coerced)
    python -m release_agent.tools_cli get_recent_runs limit=5
    python -m release_agent.tools_cli open_release_pr environment=uat image_tags=payments-api:v1.5.0

    # --dry-run: simulate mutating tools (open_release_pr, dispatch_workflow, ...)
    # without executing — read-only tools still run.
    python -m release_agent.tools_cli --dry-run open_release_pr environment=uat image_tags=x:1

Needs GH_TOKEN (or `gh auth login`) for real GitHub calls. Repos come from the
usual env (BUILD_REPO / DEPLOY_REPO).
"""

import json
import sys

from .tools.gh_tools import GH_TOOLS

_BY_NAME = {t.name: t for t in GH_TOOLS}

# Tools that change real state on GitHub. With --dry-run these are NOT executed —
# the runner prints the call it would make. Read-only tools always run.
_MUTATING = {
    "open_release_pr",
    "apply_json_update",
    "dispatch_workflow",
    "remove_from_release",
    "raise_prod_release",
    "retrigger_deployment_workflow",
}


def _coerce(v: str):
    """Best-effort scalar coercion for key=value args."""
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if v.lstrip("-").isdigit():
        return int(v)
    return v


def _parse_args(argv: list[str]) -> dict:
    """Accept a single JSON object, or repeated key=value pairs."""
    if not argv:
        return {}
    if len(argv) == 1 and argv[0].lstrip().startswith("{"):
        return json.loads(argv[0])
    out: dict = {}
    for item in argv:
        if "=" not in item:
            raise SystemExit(f"bad arg {item!r} — use key=value or a single JSON object")
        k, v = item.split("=", 1)
        out[k.strip()] = _coerce(v.strip())
    return out


def _schema(tool) -> dict:
    try:
        return tool.args or {}
    except Exception:
        return {}


def _list_tools() -> None:
    print(f"\n{len(GH_TOOLS)} tools available (no LLM needed):\n")
    for t in GH_TOOLS:
        args = ", ".join(_schema(t).keys()) or "—"
        desc = (t.description or "").strip().splitlines()[0]
        print(f"  • {t.name}({args})")
        print(f"      {desc[:96]}")
    print("\nRun:  python -m release_agent.tools_cli <tool> '<json>'   (or key=value pairs)\n")


def _show_tool(tool) -> None:
    print(f"\n{tool.name}\n{'-' * len(tool.name)}")
    print((tool.description or "").strip(), "\n")
    print("args:")
    print(json.dumps(_schema(tool), indent=2, default=str))
    print()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # --dry-run can appear anywhere; mutating tools are simulated, not executed.
    dry_run = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    if not argv or argv[0] in ("-h", "--help", "list"):
        _list_tools()
        return 0

    name = argv[0]
    tool = _BY_NAME.get(name)
    if tool is None:
        print(f"Unknown tool {name!r}. Available: {', '.join(sorted(_BY_NAME))}")
        return 2

    rest = argv[1:]
    if not rest and _schema(tool):
        # No args given but the tool takes some → show the schema instead of guessing.
        _show_tool(tool)
        return 0

    try:
        kwargs = _parse_args(rest)
    except Exception as e:
        print(f"ERROR parsing args: {e}")
        return 2

    if dry_run and name in _MUTATING:
        print(f"\n[dry-run] would execute (skipped): {name}({json.dumps(kwargs)})\n")
        return 0

    print(f"\n▶ {name}({json.dumps(kwargs)})\n")
    try:
        result = tool.invoke(kwargs)
    except Exception as e:
        print(f"ERROR running {name}: {e}")
        return 1
    # Tools return strings (often JSON). Pretty-print JSON when possible.
    try:
        print(json.dumps(json.loads(result), indent=2))
    except Exception:
        print(result)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
