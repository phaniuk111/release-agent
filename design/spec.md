# ADK Release Copilot - Focused Spec

## Objective

Provide a chatbot-style release copilot that uses ADK for free-form operator
interaction and deterministic Python tools for GitHub release operations.

## In Scope

- Answer release status, PR, workflow, and build-control questions through
  read-only tools.
- Prepare exact deployment JSON previews for UAT and PRD deploy requests.
- Require a same-thread `CONFIRM-*` token before applying a pending deploy preview.
- Route scoped operations through narrow tools:
  `remove_from_release`, `retrigger_deployment_workflow`, and `merge_prod_release`.
- Package the app for local, Docker, and Kubernetes/Helm execution.
- Keep repos, paths, branches, namespaces, model id, and workflow names config-driven.

## Out of Scope

- Letting the LLM directly edit deployment JSON or dispatch arbitrary workflows.
- Hardcoding organization-specific repo names in source code.
- Replacing GitHub as the durable release-state store.
- Production-grade shared ADK session storage; the PoV uses in-memory ADK services.

## Runtime Flow

1. User sends a message through CLI or FastAPI `/api/chat`.
2. `AdkChatService` handles pending ADK calls, confirmation tokens, and deploy
   preview detection.
3. Deploy-like requests call `prepare_deploy_preview` directly and return exact
   JSON plus a `CONFIRM-*` token.
4. Confirmation messages call `apply_confirmed_deploy`, which invokes
   `open_release_pr` through the existing GitHub tool facade.
5. Non-deploy free-form messages run through the ADK root agent and specialist
   sub-agents.

## ADK Agent Structure

- `release_copilot_adk`: root agent with global safety instruction.
- `release_status_agent`: read-only release state and workflow status.
- `release_pr_agent`: PR lookup, details, comments, and control summaries.
- `release_controls_agent`: image tag and RLFT/RFTL build checks.
- `release_ops_agent`: remove/unstage, retrigger, release staged PRD batch.
- `release_deploy_agent`: deploy preview and confirmed apply only.

## Required Safety Invariants

- `ADK_CHAT_TOOLS` must not include `apply_json_update`, `dispatch_workflow`, or
  `open_release_pr`.
- Deploy apply must reject missing, expired, or wrong confirmation tokens.
- Query-like messages that contain `image:tag` values must not be treated as
  deploys unless they include a deploy/promote intent.
- UAT deploy previews target only `uat/deployment.json`.
- PRD deploy previews target both `uat/deployment.json` and `prd/deployment.json`.

## Verification

- Run `PYTHONPATH=src:. .venv/bin/python -m pytest -q`.
- Run `PYTHONPATH=src:. .venv/bin/python -m compileall -q src adk_release_agent tests`.
- Run `helm lint helm/release-copilot`.
- Run `helm template rc helm/release-copilot -n release --set config.GOOGLE_CLOUD_PROJECT=p --set githubToken.existingSecret=s`.
- Search runtime, tests, dependencies, and current docs for retired framework terms.
