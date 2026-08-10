"""Real-scenario E2E for the DF UAT deploy path (run from GitHub Actions).

Drives the SAME code path the portal runs when a developer submits a DF UAT
deploy — release_agent.tools.dataflow.deploy_dataflow — against this repo's own
df-deploy.yml, with a real token and a real workflow_dispatch:

1. Happy path: module/binary_version for uat -> dispatch accepted, ok:true,
   the mapped inputs are exactly what the workflow declares, and a run really
   appears in Actions.
2. Rejection path: the dispatch carries an input df-deploy.yml does not declare
   -> GitHub 422s, and the tool must report ERROR — not a successful deploy.
   This is the fc71092 regression (PyGithub throw=False swallowed the 422).

Required env (set by .github/workflows/tests.yml): GH_TOKEN, DF_DEPLOY_REPO,
DF_DEPLOY_WORKFLOW, DF_DEPLOY_REF, DF_DISPATCH_INPUTS.
"""
import json
import os
import sys


def main() -> int:
    from release_agent.config import settings
    from release_agent.tools.dataflow import deploy_dataflow

    image = os.environ.get("E2E_IMAGE", "order-enrichment")
    tag = os.environ.get("E2E_TAG", "1.4.2")

    print(f"[1/2] happy path: developer submits {image}:{tag} for UAT")
    raw = deploy_dataflow(environment="uat", image=image, tag=tag)
    print(raw)
    if raw.startswith("ERROR"):
        print("FAIL: the real deploy path errored", file=sys.stderr)
        return 1
    result = json.loads(raw)
    assert result["ok"] is True, "expected ok:true"
    assert result["action"] == "df_workflow_dispatched"
    assert result["inputs"] == {
        "module": image,
        "binary_version": tag,
        "environment": "uat",
    }, f"mapped inputs mismatch: {result['inputs']}"
    assert result["run"], "dispatch reported ok but no run appeared in Actions"
    print(f"OK: run {result['run']['url']}")

    print("[2/2] rejection path: mapping carries an input the workflow does not declare")
    settings.df_dispatch_inputs = json.dumps(
        {
            "module": "{image}",
            "binary_version": "{tag}",
            "environment": "{environment}",
            "not_a_declared_input": "boom",
        }
    )
    raw = deploy_dataflow(environment="uat", image=image, tag=tag)
    print(raw)
    assert raw.startswith("ERROR deploying dataflow"), (
        "a rejected dispatch MUST be reported as an error, not a deploy"
    )
    assert "rejected" in raw, f"error should carry the rejection reason: {raw[:200]}"
    print("OK: rejected dispatch surfaced as an error (fc71092 holds)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
