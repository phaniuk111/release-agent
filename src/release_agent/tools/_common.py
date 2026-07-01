"""GitHub tools for the release ADK agent using PyGithub.

All operations are performed via the GitHub REST API (PyGithub library).
Works great with a Personal Access Token (set via GH_TOKEN env var).
"""

import base64
import itertools
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from github import Github, Auth, GithubException
from pydantic import BaseModel, Field

# Config - using Pydantic settings for consistency
from ..config import settings

BUILD_REPO = settings.build_repo
DEPLOY_REPO = settings.deploy_repo
CONFIG_PATH = settings.config_path
MANIFEST_PATH = settings.manifest_path
# Dispatchable-workflow allow-list — driven by config (env / Helm ConfigMap), not
# hardcoded. The default workflow is always allowed so a promote never self-blocks.
ALLOWED_WORKFLOWS = set(settings.allowed_workflows) | {settings.default_workflow}
# Workflow used to (re)run the deployment simulation in DEPLOY_REPO.
ON_MERGE_WORKFLOW = settings.on_merge_workflow


@dataclass
class ToolFunction:
    """Small callable tool wrapper compatible with the repo's existing callers."""

    func: Callable
    args_schema: type[BaseModel] | None = None

    def __post_init__(self) -> None:
        self.name = self.func.__name__
        self.__name__ = self.func.__name__
        self.description = (self.func.__doc__ or "").strip()
        self.args = self._schema_properties()

    def _schema_properties(self) -> dict[str, Any]:
        if self.args_schema is None:
            return {}
        try:
            schema = self.args_schema.model_json_schema()
        except Exception:
            return {}
        return dict(schema.get("properties") or {})

    def invoke(self, payload: dict[str, Any] | None = None) -> Any:
        kwargs = dict(payload or {})
        if self.args_schema is not None:
            kwargs = self.args_schema(**kwargs).model_dump()
        return self.func(**kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)


def tool(func: Callable | None = None, *, args_schema: type[BaseModel] | None = None):
    def _decorate(target: Callable) -> ToolFunction:
        return ToolFunction(target, args_schema=args_schema)

    return _decorate if func is None else _decorate(func)



def _resolve_github_token() -> str | None:
    """Resolve a GitHub token from the environment, falling back to the `gh` CLI.

    Order: GH_TOKEN -> GITHUB_TOKEN -> `gh auth token` (keyring login).
    The CLI fallback means a developer who is logged in via `gh auth login`
    doesn't have to export a PAT manually (the previous behavior caused 404 /
    auth failures whenever GH_TOKEN was unset).
    """
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        return token.strip()
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


# HTTP-level retry for transient GitHub failures (5xx, secondary rate limits, network
# blips). Idempotent methods only (urllib3 excludes POST), so PR/branch *creation* is
# never retried — no risk of duplicate PRs. This layer matters because the tools catch
# exceptions and return strings, so a node-level retry alone wouldn't see the blip.
def _gh_retry():
    try:
        from urllib3.util.retry import Retry

        return Retry(
            total=4,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            respect_retry_after_header=True,
        )
    except Exception:
        return None


# Initialize PyGithub client (PAT via GH_TOKEN/GITHUB_TOKEN, or the gh CLI login)
def _get_github_client() -> Github:
    token = _resolve_github_token()
    retry = _gh_retry()
    if token:
        return Github(auth=Auth.Token(token), retry=retry)
    # Fallback - unauthenticated (will hit rate limits / 404s on private repos)
    return Github(retry=retry)


# Pydantic schemas for tool inputs (better validation + schema generation)
def _parse_pairs(image_tags: str) -> list[tuple[str, str]]:
    pairs = []
    for p in (x.strip() for x in image_tags.split(",")):
        if not p:
            continue
        if ":" not in p:
            raise ValueError(f"Bad image:tag {p}")
        img, tag = p.split(":", 1)
        pairs.append((img.strip(), tag.strip()))
    return pairs


def _upsert_json_file(repo, branch: str, path: str, new_doc: dict) -> None:
    """Create or update a JSON file on a branch."""
    try:
        c = repo.get_contents(path, ref=branch)
        sha = c.sha
    except Exception:
        sha = None
    content = json.dumps(new_doc, indent=2)
    msg = f"chore(release): update {path}"
    if sha:
        repo.update_file(path, msg, content, sha, branch=branch)
    else:
        repo.create_file(path, msg, content, branch=branch)


def _read_json_file(repo, branch: str, path: str) -> dict:
    try:
        c = repo.get_contents(path, ref=branch)
        return json.loads(c.decoded_content.decode())
    except Exception:
        return {}


# ---- Today's PRD release window (shared across sessions via GitHub) ----




__all__ = ['settings', 'tool', 'BaseModel', 'Field', 'json', 'base64', 'itertools', 'uuid', 'Github', 'Auth', 'GithubException', '_resolve_github_token', '_gh_retry', '_get_github_client', '_read_json_file', '_upsert_json_file', '_parse_pairs', 'CONFIG_PATH', 'MANIFEST_PATH', 'ALLOWED_WORKFLOWS', 'ON_MERGE_WORKFLOW', 'BUILD_REPO', 'DEPLOY_REPO']
