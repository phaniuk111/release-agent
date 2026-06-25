"""
Simple but nice CLI chat interface for the Release Copilot (PoV).

Features:
- Streaming tokens when possible
- Handles LangGraph interrupts for confirmation
- Pretty printing with rich
- Thread support via --thread-id
"""

import argparse
import json
import os
import sys
import uuid
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from .agent import get_compiled_graph, message_text
from .budget import ask_user_with_timeout, get_budget_status

load_dotenv()
console = Console()


def run_cli(thread_id: str | None = None, repo: str | None = None):
    from .config import settings
    if repo:
        # settings and gh_tools module globals were resolved at import time, so
        # updating only the env var here would be ignored — patch the live values.
        os.environ["RELEASE_AGENT_TARGET_REPO"] = repo
        from .tools import gh_tools as _gh
        settings.target_repo = repo
        _gh.TARGET_REPO = repo

    graph = get_compiled_graph()
    thread_id = thread_id or f"cli-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}

    console.print(Panel.fit(
        f"[bold cyan]Release Copilot[/]  •  repo=[bold]{settings.target_repo}[/]\n"
        "Type your request (image:tag pairs). Use /quit to exit. Use /status for recent runs.",
        title="LangGraph + gh CLI"
    ))

    pending = False       # True when the graph is paused at an interrupt (awaiting resume)
    auto_input = None     # if set, use as the next message without prompting the user

    while True:
        if auto_input is not None:
            user = auto_input
            auto_input = None
        else:
            try:
                user = console.input("\n[bold green]You[/]: ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\nBye!")
                break

            if not user:
                continue
            if user.lower() in {"/quit", "/exit", "quit", "exit"}:
                break
            if user.lower() in {"/status"}:
                user = "show me the status of recent workflow runs"

        # If the graph is paused at an interrupt, this input is a RESUME value
        # (the confirmation token / budget answer) — not a brand-new turn.
        input_data = Command(resume=user) if pending else {"messages": [HumanMessage(content=user)]}
        pending = False

        try:
            final_state = None
            for chunk in graph.stream(input_data, config=config, stream_mode="values"):
                final_state = chunk

            # After the turn, check for a pending interrupt (HITL pause).
            snapshot = graph.get_state(config)
            if snapshot.interrupts:
                pending = True
                interrupt_payload = snapshot.interrupts[0].value
                itype = interrupt_payload.get("type", "gate")

                if itype == "budget_confirmation":
                    msg = interrupt_payload.get("message", "Budget warning")
                    action = interrupt_payload.get("action", "Continue? (yes/no)")
                    console.print(Panel(
                        Markdown(f"**BUDGET WARNING**\n\n{msg}\n\n**{action}**\n\nCurrent: {get_budget_status()}"),
                        title="[red]Budget Protection — £10 limit[/]",
                        border_style="red"
                    ))
                    response = ask_user_with_timeout("Do you want to continue? (yes/no)", timeout=45)
                    if response is None:
                        console.print("[red]No response — stopping to protect budget.[/red]")
                        break
                    # Resume immediately with the answer on the next iteration.
                    auto_input = str(response)
                    continue

                # Normal release confirmation gate.
                token = interrupt_payload.get("token", "CONFIRM-???")
                proposed = json.dumps(interrupt_payload.get("proposed", {}), indent=2)
                console.print(Panel(
                    Markdown(
                        f"**Action required**\n\n{interrupt_payload.get('message', '')}\n\n"
                        f"Proposed:\n```json\n{proposed}\n```"
                    ),
                    title=f"[yellow]Confirmation needed — {token}[/]",
                    border_style="yellow"
                ))
                console.print(f"[dim]Type the token to confirm, or anything else to cancel.[/dim]")
                continue

            # Normal completion — print the last assistant (AIMessage) content.
            if final_state and final_state.get("messages"):
                for m in reversed(final_state["messages"]):
                    if isinstance(m, AIMessage):
                        text = message_text(m)
                        if text:
                            console.print(f"[bold blue]Copilot[/]: {text}")
                            break

            if final_state and final_state.get("last_action"):
                console.print(f"[dim]Last action:[/dim] {final_state['last_action']}")

        except Exception as exc:
            console.print(f"[red]Error:[/red] {exc}")
            try:
                snap = graph.get_state(config)
                console.print(f"[dim]next nodes: {snap.next}[/dim]")
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", default=None, help="Resume or label a conversation thread")
    parser.add_argument("--repo", default=None, help="Override RELEASE_AGENT_TARGET_REPO (e.g. phaniuk111/devops)")
    args = parser.parse_args()

    run_cli(thread_id=args.thread_id, repo=args.repo)
