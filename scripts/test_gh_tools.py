"""Quick smoke test of the gh tools (requires gh auth + network to the repo)."""

import os
from src.release_agent.tools.gh_tools import (
    list_allowed_images,
    get_build_report,
    get_recent_runs,
)

if __name__ == "__main__":
    repo = os.getenv("BUILD_REPO", "phaniuk111/devops")
    print(f"Testing against {repo}...")

    print("\n1. list_allowed_images")
    print(list_allowed_images())

    print("\n2. get_build_report")
    print(get_build_report(image="payments-api", tag="2.0.99-test")[:500])

    # WARNING: open_release_pr will mutate if you have write permission.
    # Run it through the CLI and follow the confirmation flow instead.

    print("\n3. get_recent_runs")
    print(get_recent_runs(3))

    print("\nDone. For full dispatch test run the CLI and follow the confirmation flow.")
