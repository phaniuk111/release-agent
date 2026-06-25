# Release Chatbot Agent — Concise Design Summary (2026-06-25)

**Goal:** LangGraph-powered conversational "front door" for release promotion. Devs chat image:tag pairs → agent safely updates JSON config(s) in GitHub repo → triggers `workflow_dispatch` (leveraging existing `image-tag-step-report.yml` + image-workflows.json from `devops/`).

**PoV Target:** Real dispatches + file writes against https://github.com/phaniuk111 (gh-image-tag-report-test clone). Runs locally via `gh auth` and in K8s.

## Key Architecture
- **UI:** Chainlit (preferred: streaming, tool steps, native human interrupts). FastAPI fallback.
- **Agent:** LangGraph `StateGraph` (not pure ReAct) with Pydantic `ReleaseAgentState` (messages + proposed/confirmed updates + pending_confirmation + audit).
- **HITL:** `interrupt()` before `update_image_tag_in_config` and `dispatch_release_workflow`. Explicit phrase confirm ("CONFIRM UPDATE", "CONFIRM RELEASE").
- **Tools (whitelisted only):**
  - Read: `list_allowed_images` (from image-workflows.json), `read_repo_json`, `list_recent...`, `get_workflow_run_status`.
  - Mutate: `update_image_tag_in_config` (gh contents PUT with sha), `dispatch_release_workflow` (POST /dispatches with image_tags).
- **GitHub:** gh CLI + `GH_TOKEN` (local = `gh auth token`; K8s Secret). No arbitrary commands.
- **Persistence:** SqliteSaver (PoV) / PostgresSaver (prod) via thread_id. Stateful multi-turn.
- **Sequence:** Parse → gather (allowed + current manifest) → propose → interrupt → mutate JSON → interrupt → dispatch → poll/report.
- **Config handoff:** Chatbot owns/updates `prod-image-versions.json` (or similar); passes `image_tags` CSV to workflows that can further mutate prod configs.

## Deployment
- **Local:** `export GH_TOKEN=$(gh auth token) OPENAI_API_KEY=...`; `chainlit run ...` or `python -m`.
- **Docker:** Full image with gh CLI baked in + healthcheck. `docker run -e GH_TOKEN=...`.
- **K8s:** Deployment (or StatefulSet), Service, ConfigMap, Secret (GH_TOKEN + LLM). ExternalSecrets pattern (from qTest.Charts). Resources 250m/512Mi → 1/1Gi. Probes, non-root, modeled on promptfoo helm + eks-production.
- **Checkpointer DB:** Postgres recommended for K8s.

## Safety & Ops
- Strict allowlist + tag regex + path whitelist.
- Full structured audit in state + logs.
- Low-temp structured LLM outputs + validation.
- LangSmith traces + Prometheus + JSON logs.
- GitHub App (prod) / PAT (PoV). Minimal scopes.

## Example Flow
"update payments-api to 2.0.33" → propose (shows diff) → CONFIRM UPDATE → commit link → CONFIRM RELEASE → dispatch run URL + later status (RLFT steps via existing workflow).

## PR Plan (incremental, mergeable)
1. Bootstrap + LangGraph skeleton + state.
2. Read-only gh tools + allowlist.
3. Mutating tools + interrupts + audit.
4. Chainlit UI + sample convos.
5. Dockerfile + docker PoV.
6. K8s manifests + Helm skeleton.
7. Observability, tests, hardening, docs.
8. (Later) GitHub App, multi-repo, Slack, etc.

**Status:** Design complete and ready for immediate PoV coding. All patterns cross-checked against workspace (devops/ workflows + sh + json, helm charts, agent examples, EKS TF, FastAPI chat demo).

**Files written:**
- /tmp/grok-design-doc-d602e8dc.md (full spec)
- /tmp/grok-design-summary-d602e8dc.md (this)

## Key Decisions
- Chainlit for initial UI (fast rich agent experience with built-in steps + human input) over pure custom React or Gradio. Can swap backend later.
- StateGraph + explicit interrupts rather than pure `create_react_agent` for predictable HITL and safety routing.
- gh CLI (via GH_TOKEN) for PoV fidelity to existing shell tooling and easy local dev. PyGithub as optional future abstraction.
- Postgres checkpointer in prod; sqlite for PoV. Never rely on in-memory only.
- Direct file update + dispatch (not PRs) for speed in release path; branch protection + audit compensate. Alternative "create PR" tool can be added.
- image-workflows.json as allowlist source (read-only); new `prod-image-versions.json` (or equivalent) as the mutable release manifest the chatbot owns.
- GitHub App preferred long-term over PAT.
- Audit as first-class (structured log entries in state + external sink) because this controls prod changes.
- Focus strictly on chatbot + orchestration; treat downstream "update other prod configs" as responsibility of the dispatched workflow (design provides clean handoff via updated JSON + image_tags input).

## PR Plan
1. PR-01: Repository bootstrap + core LangGraph skeleton
2. PR-02: GitHub read-only tools + allowlist integration
3. PR-03: Mutating tools + HITL interrupts (core safety)
4. PR-04: Chat UI (Chainlit) + conversational flows
5. PR-05: Dockerfile, local Docker validation, and PoV packaging
6. PR-06: Kubernetes manifests + deployment assets
7. PR-07: Hardening, observability, tests & docs
8. PR-08 (post-PoV): GitHub App auth support, Slack integration hook, multi-repo config, rate limits, PR-creation variant of update tool, production Helm values + Argo CD Application.
