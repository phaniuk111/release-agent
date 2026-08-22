# Porting `adk-release-agent` → Backstage (Local PoC)

Source of truth analysed: branch `adk-release-agent` (read-only; **no code written there**).
All PoC work lands on branch `backstage_poc`.

## 1. What the adk-release-agent codebase is

**Release Copilot** — a Google ADK-backed conversational front door for GitHub-based
release operations.

### Layers

| Layer | Location | Role |
| --- | --- | --- |
| Agent runtime | `adk_release_agent/agent.py` | ADK root `Agent` in an `App` with a mutation-guard safety plugin; filesystem skills under `adk_release_agent/skills/` |
| Deploy lane | `adk_release_agent/deploy_workflow.py`, `deploy.py`, `intent.py`, `safety.py` | Deterministic ADK Workflow graph: `prepare_deploy_preview → CONFIRM-* token → apply_confirmed_deploy` (HITL gate) |
| Tool layer | `src/release_agent/tools/` (PyGithub; `gh_tools.py` export, wrapped in `adk_release_agent/tools.py`) | GitHub reads/mutations: manifests, PRs, workflow dispatch, release queue, controls, Jira, BigQuery insights |
| Service adapter | `src/release_agent/adk_service.py`, `session_creds.py` | Session/thread management over ADK session services (in-memory or Vertex AI Agent Engine) |
| Web UI | `src/release_agent/app_fastapi.py` + `src/release_agent/static/*.js` | FastAPI app: HTML chat page + SSE `/api/chat` + REST APIs (below) |
| CLI | `src/release_agent/cli.py` | Same service, terminal front end |
| Infra | `Dockerfile` (uv, 2-stage, non-root), `helm/release-copilot/`, `k8s/` | Container + GKE deploy |

### HTTP surface (what Backstage will call)

- `POST /api/chat` (SSE stream) — conversational endpoint
- `POST /api/session/connect|disconnect`, `GET /api/session/status`
- `GET /api/release-status`, `GET/POST /api/release-queue(+ /batch, /withdraw)`
- `POST /api/release-draft`, `GET /api/release-insights`, `GET /api/console-links`
- `GET /api/deployment-types`, `/api/df-template`, `/api/deploy-template`

### External dependencies / env

- `GH_TOKEN` (repo + workflow scopes) — the app shells out to `git`/`gh` logic via PyGithub and git CLI
- `GOOGLE_CLOUD_PROJECT` + Vertex ADC (or Workload Identity), `GOOGLE_CLOUD_LOCATION`, `GEMINI_MODEL`
- `BUILD_REPO`, `DEPLOY_REPO` (no defaults), optional `DEFAULT_WORKFLOW`
- BigQuery dataset for the release intake queue
- Runtime needs `git` in the image (CARE/DF release flow clones deploy repo)

## 2. Porting strategy for the PoC

**Do NOT rewrite the agent.** Backstage becomes the new front door; Release Copilot
stays a containerized service that Backstage talks to.

```
┌────────────────────────── Backstage (local, Docker) ─────────────────────────┐
│  Frontend plugin (release-copilot)  →  Backend plugin (proxy + auth)         │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       │ HTTP (container network)
┌──────────────────────────────────────▼───────────────────────────────────────┐
│  release-copilot container (existing Dockerfile, FastAPI :8000)             │
└──────────────────────────────────────────────────────────────────────────────┘
```

1. **release-copilot service** — build the branch's own `Dockerfile` unmodified;
   run with env vars from `.env`.
2. **Backend proxy** — uses the built-in `@backstage/plugin-proxy-backend`
   (config-only, no custom plugin for the PoC): `/api/proxy/release-copilot/*`
   → `RELEASE_COPILOT_URL`. Handles CORS and streams SSE chat.
3. **Backstage frontend plugin** `release-copilot` — a page with:
   - a chat panel streaming from the SSE proxy, and
   - simple panels for release-status / release-queue (REST GET/POST).
4. **Docker Compose** — one network: `backstage` + `release-copilot` (+ optional
   mock env for a fully offline demo).

### PoC scope cuts (explicit)

- No Backstage auth/identity integration initially (guest access, local only).
- No catalog ingestion of releases (post-PoC candidate).
- No Kubernetes/Helm — Compose only.

## 3. Build checklist (branch `backstage_poc`)

- [x] `docs/BACKSTAGE_PORT_PLAN.md` (this file)
- [x] Scaffold Backstage app under `backstage/` (`npx @backstage/create-app`, guest auth)
- [x] Proxy + SSE forwarding via built-in proxy backend (`proxy.endpoints` in `backstage/app-config.yaml`)
- [x] `plugins/release-copilot`: chat + status/queue page, route `/release-copilot`, sidebar icon
- [x] `docker-compose.backstage.yml`: backstage + release-copilot services, shared `.env`
- [ ] `README` PoC run instructions (`docker compose up`, open :7007/release-copilot)
- [x] Smoke test: chat round-trip + release-status through containers ✅ (SSE tokens streamed via proxy in docker compose)
