# Release Copilot ADK Design Summary

## Goal

Release Copilot is an ADK-backed conversational front door for GitHub-based
release operations. Developers can ask free-form questions about releases, PRs,
build controls, and deployment state, while release-changing actions are routed
through deterministic Python tools and explicit human confirmation.

## Current Architecture

- **Runtime:** Google ADK root agent in `adk_release_agent/agent.py`.
- **Skills:** Filesystem ADK skills under `adk_release_agent/skills/` for status,
  PR tracking, controls, scoped ops, and deploy.
- **Interfaces:** FastAPI SSE web UI and CLI both call `release_agent.adk_service`.
- **Tool layer:** PyGithub tools in `src/release_agent/tools/`, exported through
  `gh_tools.py` and wrapped for ADK in `adk_release_agent/tools.py`.
- **Deploy lane:** `prepare_deploy_preview -> CONFIRM token -> apply_confirmed_deploy`.
- **Conversation state:** ADK in-memory session/artifact/memory services for PoV;
  shared storage can be added for multi-replica production deployments.
- **Durable release state:** GitHub remains the source of truth through deployment
  JSON files, release PRs, branches, and workflow runs.

## Safety Boundaries

- The LLM may select read tools, summarize tool results, and guide operators.
- Low-level release-defining mutations are not exposed as free-form chat tools:
  `apply_json_update`, `dispatch_workflow`, and `open_release_pr`.
- Deploy/add requests must use the deterministic deploy facade.
- A real deploy mutation requires an exact `CONFIRM-*` token for a pending preview.
- Scoped ops are limited to remove/unstage, retrigger workflow, and release the
  staged PRD batch after cutoff.

## Deployment

- **Local:** `PYTHONPATH=src:. uvicorn release_agent.app_fastapi:app --app-dir src`.
- **ADK runner:** `PYTHONPATH=src:. adk run adk_release_agent`.
- **Container/Kubernetes:** Dockerfile copies both `src/` and `adk_release_agent/`;
  Helm wires GitHub and Vertex AI configuration through ConfigMap/Secret values.

## Validation Strategy

- Unit tests cover parser behavior, deploy previews, confirmation apply behavior,
  ADK tool exposure, FastAPI SSE events, config, UI JavaScript, and build controls.
- `compileall` verifies Python syntax/importability.
- Helm lint/template verifies deployment manifests.
- Repo search verifies the old framework stack is not present in runtime,
  dependency, or current docs.
