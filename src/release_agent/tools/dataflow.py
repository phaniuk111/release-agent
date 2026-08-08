"""Dataflow flex-template deploys — workflow-dispatch model.

The developer supplies the flex-template IMAGE NAME and TAG; deploying means
triggering the DF repo's deploy workflow (``workflow_dispatch`` of
``settings.df_deploy_workflow``) on ``settings.df_deploy_ref``. There is no state
file — the workflow run IS the deploy. The run link is returned so the developer
can watch it.

The dispatch input NAMES are configured, not hardcoded: GitHub 422s a dispatch
carrying an input the workflow does not declare, and DF workflows name theirs
differently (``module``/``binary_version`` rather than ``image``/``tag``). See
``settings.df_dispatch_inputs``.
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


def _dispatch_inputs(image: str, tag: str, env: str) -> dict:
    """Map our values onto the target workflow's declared input names.

    Teams name workflow_dispatch inputs differently (module/binary_version vs
    image/tag), and GitHub rejects a dispatch that carries an input the
    workflow does not declare — so this cannot be hardcoded. DF_DISPATCH_INPUTS
    is a JSON object of {workflow_input_name: template}; {image}, {tag} and
    {environment} are substituted. Keys absent from the template are simply not
    sent, which is how a workflow with no environment input is supported.
    """
    template = (settings.df_dispatch_inputs or "").strip()
    if not template:
        return {"image": image, "tag": tag, "environment": env}
    try:
        mapping = json.loads(template)
    except json.JSONDecodeError as e:
        raise ValueError(f"DF_DISPATCH_INPUTS is not valid JSON ({e}): {template[:80]}")
    if not isinstance(mapping, dict):
        raise ValueError("DF_DISPATCH_INPUTS must be a JSON object")
    values = {"image": image, "tag": tag, "environment": env}
    out = {}
    for key, raw in mapping.items():
        text = str(raw)
        for name, value in values.items():
            text = text.replace("{" + name + "}", value)
        out[str(key)] = text
    return out


def _dispatch_mapping() -> dict:
    """DF_DISPATCH_INPUTS as a dict, or {} when it is unset/unusable.

    UI-facing callers want the mapping without inheriting the dispatch path's
    hard failure — a bad template must break the deploy, not blank the form.
    """
    template = (settings.df_dispatch_inputs or "").strip()
    if not template:
        return {}
    try:
        mapping = json.loads(template)
    except json.JSONDecodeError:
        return {}
    return mapping if isinstance(mapping, dict) else {}


def _field_input_names() -> dict:
    """Invert the mapping: which workflow input carries our image, and which our
    tag — e.g. {"image": "module", "tag": "binary_version"}.

    Only templates that are exactly one placeholder map back to a form field; a
    composite like "{image}:{tag}" is one input fed by two fields, so it has no
    single field to label or populate and is left out.
    """
    out = {}
    for key, raw in _dispatch_mapping().items():
        placeholder = str(raw).strip()
        for field in ("image", "tag"):
            if placeholder == "{" + field + "}":
                out.setdefault(field, str(key))
    return out


def workflow_dispatch_inputs(repo, workflow, ref: str) -> dict:
    """The target workflow's declared ``workflow_dispatch`` inputs, read from its
    YAML at ``ref``. Empty dict if it cannot be read — the form falls back to
    plain text fields rather than failing to open."""
    from ruamel.yaml import YAML

    try:
        raw = repo.get_contents(workflow.path, ref=ref).decoded_content
        spec = YAML(typ="safe").load(raw) or {}
    except Exception:
        return {}
    # YAML 1.1 parses a bare `on:` key as the boolean True; 1.2 keeps it a
    # string. Loaders disagree, so accept either.
    triggers = spec.get("on", spec.get(True)) or {}
    if not isinstance(triggers, dict):
        return {}
    dispatch = triggers.get("workflow_dispatch") or {}
    inputs = dispatch.get("inputs") if isinstance(dispatch, dict) else None
    return inputs if isinstance(inputs, dict) else {}


@tool(args_schema=DeployDataflowInput)
def deploy_dataflow(environment: str, image: str, tag: str, deployment_repo: str = "") -> str:
    """Deploy a Dataflow flex template: dispatch the DF repo's deploy workflow with
    the image/tag inputs it declares and return the triggered run."""
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
        inputs = _dispatch_inputs(image, tag, env)
    except ValueError as e:
        return f"ERROR deploying dataflow: {e}"

    try:
        repo = _get_github_client().get_repo(repo_full)
        workflow = repo.get_workflow(settings.df_deploy_workflow)
        before_ids = {r.id for r in itertools.islice(workflow.get_runs(), 5)}
        # The ref must be a branch that CONTAINS the workflow file, which is not
        # necessarily the repo default branch.
        ref = (settings.df_deploy_ref or "").strip() or repo.default_branch
    except Exception as e:
        return f"ERROR deploying dataflow: {e}"

    try:
        # throw=True is REQUIRED: PyGithub defaults to throw=False, which swallows
        # GitHub's rejection and returns False — we would report a deploy that
        # never ran. The raised error carries the actionable reason (an input the
        # workflow does not declare, a value outside a choice input's options, or
        # a ref that does not hold the workflow file).
        workflow.create_dispatch(ref=ref, inputs=inputs, throw=True)
    except Exception as e:
        return (
            f"ERROR deploying dataflow: dispatch of {settings.df_deploy_workflow} "
            f"in {repo_full} on ref '{ref}' with inputs {inputs} was rejected: {e}"
        )

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
            "ref": ref,
            "inputs": inputs,
            "run": run,
            "note": note,
        },
        indent=2,
    )
