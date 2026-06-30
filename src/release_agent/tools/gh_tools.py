"""Backward-compat facade — re-exports the modular GitHub tools.

The implementation now lives in focused modules (_common, manifest, pull_requests,
controls, release_window, promotion). This module re-exports them so existing
`from .tools.gh_tools import ...` imports keep working, and assembles GH_TOOLS.
"""


# Underscore helpers other modules import from this facade:
from ._common import (  # noqa: F401
    _get_github_client, _read_json_file, _upsert_json_file, _parse_pairs,
    _resolve_github_token, BUILD_REPO, DEPLOY_REPO,
)
from .pull_requests import _find_prs_for_images, _find_pr_for_images  # noqa: F401
from .controls import (  # noqa: F401
    _build_repo_full, _find_build_run, _controls_report, _image_build_workflow,
)
from .promotion import _lead_time_ok, _apply_via_pr_chain, _merge_pr  # noqa: F401
from .release_window import get_release_status  # noqa: F401

# Tools assembled for the agent:
from .manifest import (  # noqa: F401
    list_allowed_images, get_current_manifest, propose_update, apply_json_update,
    dispatch_workflow, get_recent_runs, get_workflow_status,
)
from .pull_requests import (  # noqa: F401
    find_prs, get_pr_details, get_pr_comments, summarize_pr_controls,
    retrigger_deployment_workflow,
)
from .controls import verify_image_tag_build, get_build_controls  # noqa: F401
from .release_window import check_release_window  # noqa: F401
from .promotion import open_release_pr, raise_prod_release, remove_from_release  # noqa: F401

GH_TOOLS = [
    list_allowed_images,
    get_current_manifest,
    propose_update,
    apply_json_update,
    dispatch_workflow,
    get_recent_runs,
    get_workflow_status,
    find_prs,
    get_pr_details,
    get_pr_comments,
    summarize_pr_controls,
    retrigger_deployment_workflow,
    verify_image_tag_build,
    get_build_controls,
    open_release_pr,
    raise_prod_release,
    remove_from_release,
    check_release_window,
]
