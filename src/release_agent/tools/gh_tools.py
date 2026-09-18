"""Backward-compat facade — re-exports the modular GitHub tools.

The implementation now lives in focused modules (_common, manifest, pull_requests,
controls, release_window, promotion). This module re-exports them so existing
`from .tools.gh_tools import ...` imports keep working, and assembles GH_TOOLS.
"""


# Underscore helpers other modules import from this facade:
from ._common import (  # noqa: F401
    _get_github_client, _read_json_file, _upsert_json_file, _parse_pairs,
    _resolve_github_token,
)
from .promotion import (  # noqa: F401
    _merge_pr, assemble_entry, _upsert_entry, _remove_entry,
    plan_deploy, _normalize_entry, _entries_for_deploy,
)
from .release_window import get_release_status  # noqa: F401

# Tools assembled for the agent:
from .manifest import list_allowed_images, get_recent_runs, get_workflow_status  # noqa: F401
from .pull_requests import find_prs, get_pr_details, get_pr_comments  # noqa: F401
from .controls import get_build_report  # noqa: F401
from .release_window import check_release_window  # noqa: F401
from .promotion import open_release_pr, remove_from_release  # noqa: F401
from .dataflow import deploy_dataflow  # noqa: F401
from .release_fileset import promote_release  # noqa: F401

GH_TOOLS = [
    list_allowed_images,
    get_recent_runs,
    get_workflow_status,
    find_prs,
    get_pr_details,
    get_pr_comments,
    get_build_report,
    open_release_pr,
    remove_from_release,
    check_release_window,
]
