"""Dataflow flex-template deploys — workflow-dispatch model.

The developer supplies the flex-template IMAGE NAME and TAG; deploying means
triggering the DF repo's deploy workflow (``workflow_dispatch`` of
``settings.df_deploy_workflow``, default ``df-deploy.yml``) with
``{image, tag, environment}`` inputs. There is no state file — the workflow run
IS the deploy. The run link is returned so the developer can watch it.
"""
from __future__ import annotations

import itertools
import time

from ._common import (
    settings,
    tool,
    BaseModel,
    Field,
    json,
    _get_github_client,
)


class DeployDataflowInput(BaseModel):
    environment: str = Field(..., description="Target environment: uat (prod not yet supported)")
    image: str = Field(..., description="Flex-template image name, e.g. 'order-enrichment'.")
    tag: str = Field(..., description="Image tag, e.g. '1.4.2'.")
    deployment_repo: str = Field(
        default="",
        description="Dataflow repo override (owner/repo) hosting the deploy workflow. "
        "Empty = configured default.",
    )


def _find_dispatched_run(workflow, before_ids: set, tries: int = 6, delay: float = 2.0):
    """Best-effort: the run our dispatch just created (newest run not seen before)."""
    for i in range(tries):
        try:
            for run in itertools.islice(workflow.get_runs(), 5):
                if run.id not in before_ids:
                    return {"id": run.id, "url": run.html_url, "status": run.status}
        except Exception:
            pass
        if i < tries - 1:
            time.sleep(delay)
    return None


@tool(args_schema=DeployDataflowInput)
def deploy_dataflow(environment: str, image: str, tag: str, deployment_repo: str = "") -> str:
    """Deploy a Dataflow flex template: dispatch the DF repo's deploy workflow with
    {image, tag, environment} inputs and return the triggered run."""
    from ..session_creds import _normalize_repo

    env = str(environment or "").strip().lower()
    if env != "uat":
        return (
            f"ERROR deploying dataflow: environment '{environment}' not supported yet "
            "(only uat for now)."
        )
    image, tag = (image or "").strip(), (tag or "").strip()
    if not image or not tag:
        return "ERROR deploying dataflow: both image name and tag are required."

    target_repo = _normalize_repo(deployment_repo) if deployment_repo else ""
    if deployment_repo and not target_repo:
        return (
            f"ERROR deploying dataflow: could not parse deployment_repo "
            f"'{deployment_repo}' (use owner/repo)."
        )
    repo_full = target_repo or settings.df_deploy_repo
    if not repo_full:
        return "ERROR deploying dataflow: no Dataflow repo configured (set DF_DEPLOY_REPO)."

    try:
        repo = _get_github_client().get_repo(repo_full)
        workflow = repo.get_workflow(settings.df_deploy_workflow)
        before_ids = {r.id for r in itertools.islice(workflow.get_runs(), 5)}
        inputs = {"image": image, "tag": tag, "environment": env}
        workflow.create_dispatch(ref=repo.default_branch, inputs=inputs)
    except Exception as e:
        return f"ERROR deploying dataflow: {e}"

    run = _find_dispatched_run(workflow, before_ids)
    note = (
        f"Dispatched DF deploy workflow {settings.df_deploy_workflow} in {repo_full} "
        f"for {image}:{tag} → {env}."
    )
    if run:
        note += f" Run #{run['id']}: {run['url']}"
    else:
        note += " The run should appear in Actions shortly."
    return json.dumps(
        {
            "ok": True,
            "action": "df_workflow_dispatched",
            "deployment_type": "dataflow",
            "environment": env,
            "repo": repo_full,
            "workflow": settings.df_deploy_workflow,
            "inputs": inputs,
            "run": run,
            "note": note,
        },
        indent=2,
    )
