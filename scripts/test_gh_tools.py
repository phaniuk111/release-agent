"""Quick smoke test of the gh tools (requires gh auth + network to the repo)."""

import os
from src.release_agent.tools.gh_tools import (
    list_allowed_images,
    get_current_manifest,
    propose_update,
    get_recent_runs,
)

if __name__ == "__main__":
    repo = os.getenv("BUILD_REPO", "phaniuk111/devops")
    print(f"Testing against {repo}...")

    print("\n1. list_allowed_images")
    print(list_allowed_images())

    print("\n2. get_current_manifest")
    print(get_current_manifest()[:500])

    print("\n3. propose_update")
    prop = propose_update("payments-api:2.0.99-test")
    print(prop[:800])

    # WARNING: the next two will mutate if you have write permission.
    # They are commented by default.
    # print("\n4. (dry) apply would be next after confirm")
    # print(apply_json_update("payments-api:2.0.99-test", "test apply from agent smoke test"))

    print("\n5. get_recent_runs")
    print(get_recent_runs(3))

    print("\nDone. For full dispatch test run the CLI and follow the confirmation flow.")
