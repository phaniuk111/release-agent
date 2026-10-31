# ADK Release Copilot - Focused Spec

## Objective

Provide a chatbot-style release copilot that uses ADK for free-form operator
interaction and deterministic Python tools for GitHub release operations.

## In Scope

- Answer release status, PR, workflow, and build-control questions through
  read-only tools.
- Prepare exact deployment JSON previews for UAT deploy requests.
- Require a same-thread `CONFIRM-*` token before applying a pending deploy preview.
- Route scoped operations through narrow tools: `promote_release` and
  `promote_df_release` (PRD and PRL1 pause on a yes/no approval).
- There are no removals: a chart leaves the next release only by being withdrawn
  from the release queue (`withdraw_release_intent`).
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
5. Non-deploy free-form messages run through the ADK chat agent, whose skills
   unlock each domain's tools.

## ADK Agent Structure

One chat agent with a global safety instruction, routed by filesystem skills:

- `release-status`: read-only release state and workflow status.
- `release-pr`: PR lookup, details, comments, and control summaries.
- `release-controls`: image tag and RLFT/RFTL build checks.
- `release-ops`: promote a CARE or DF release to the next environment.
- `release-deploy`: tool-less guard; deploys run in the deterministic Workflow.

## Required Safety Invariants

- `ADK_CHAT_TOOLS` must not include `open_release_pr`, `apply_confirmed_deploy`, or
  `deploy_dataflow`.
- Deploy apply must reject missing, expired, or wrong confirmation tokens.
- Query-like messages that contain `image:tag` values must not be treated as
  deploys unless they include a deploy/promote intent.
- UAT deploy previews target only `uat/deployment.json`.
- A PRD deploy is refused: PROD is reached only through a release.

## Verification

- Run `PYTHONPATH=src:. .venv/bin/python -m pytest -q`.
- Run `PYTHONPATH=src:. .venv/bin/python -m compileall -q src adk_release_agent tests`.
- Run `helm lint helm/release-copilot`.
- Run `helm template rc helm/release-copilot -n release --set config.GOOGLE_CLOUD_PROJECT=p --set githubToken.existingSecret=s`.
- Search runtime, tests, dependencies, and current docs for retired framework terms.
