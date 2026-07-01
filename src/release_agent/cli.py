"""
Simple but nice CLI chat interface for the Release Copilot (PoV).

Features:
- Streaming tokens when possible
- Handles ADK/deploy interrupts for confirmation
- Pretty printing with rich
- Thread support via --thread-id
"""

import argparse
import asyncio
import os
import uuid
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

load_dotenv()
console = Console()


def run_cli(thread_id: str | None = None, repo: str | None = None):
    from .config import settings

    if repo:
        # settings and gh_tools module globals were resolved at import time, so
        # updating only the env var here would be ignored — patch the live values.
        os.environ["BUILD_REPO"] = repo
        from .tools import gh_tools as _gh

        settings.build_repo = repo
        _gh.BUILD_REPO = repo

    from .adk_service import get_adk_chat_service

    service = get_adk_chat_service()
    thread_id = thread_id or f"cli-{uuid.uuid4().hex[:8]}"

    console.print(
        Panel.fit(
            f"[bold cyan]Release Copilot[/]  •  repo=[bold]{settings.build_repo}[/]\n"
            "Type your request (image:tag pairs). Use /quit to exit. Use /status for recent runs.",
            title="ADK + GitHub",
        )
    )

    while True:
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

        try:
            asyncio.run(_print_adk_turn(service, user, thread_id))

        except Exception as exc:
            console.print(f"[red]Error:[/red] {exc}")


async def _print_adk_turn(service, user: str, thread_id: str) -> None:
    async for event in service.stream_chat(user, thread_id):
        etype = event.get("type")
        if etype == "token" and event.get("content"):
            console.print(f"[bold blue]Copilot[/]:")
            console.print(Markdown(str(event["content"])))
        elif etype == "interrupt":
            data = event.get("data") or {}
            title = data.get("token") or data.get("function") or "confirmation"
            console.print(
                Panel(
                    Markdown(
                        f"**Action required**\n\n{data.get('message', '')}\n\n"
                        f"{data.get('action', 'Provide the requested confirmation.')}"
                    ),
                    title=f"[yellow]Confirmation needed — {title}[/]",
                    border_style="yellow",
                )
            )
            console.print("[dim]Reply with the requested token or confirmation text.[/dim]")
        elif etype == "error":
            console.print(f"[red]Error:[/red] {event.get('content', 'Unknown error')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", default=None, help="Resume or label a conversation thread")
    parser.add_argument("--repo", default=None, help="Override BUILD_REPO (e.g. org/repo)")
    args = parser.parse_args()

    run_cli(thread_id=args.thread_id, repo=args.repo)
