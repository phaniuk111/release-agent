# Backstage PoC — Release Copilot front door

Run the `adk-release-agent` Release Copilot behind a local Backstage portal,
both in Docker. See `docs/BACKSTAGE_PORT_PLAN.md` for the analysis/design.

## Prereqs

- Docker Desktop running
- `gh` CLI logged in (`repo` + `workflow` scopes)
- `gcloud auth application-default login` (ADC for Vertex AI)
- `.env` at the repo root with `GOOGLE_CLOUD_PROJECT`, `BUILD_REPO`, `DEPLOY_REPO`
  (copy `.env.example` as a template)

## One-time setup

```bash
# read-only worktree of the agent branch (docker builds from it)
git worktree add .worktrees/adk adk-release-agent
```

## Run

```bash
export GH_TOKEN=$(gh auth token)
docker compose -f docker-compose.backstage.yml up --build
```

Then open **<http://localhost:7007/release-copilot>**, sign in as **Guest**, and chat.

## What runs

| Container | Image | Purpose |
| --- | --- | --- |
| `backstage` | `backstage-release-copilot:poc` | Backstage 1.54 (guest auth), proxies `/api/proxy/release-copilot/*` → the agent; frontend plugin `plugins/release-copilot` renders chat + release status/queue |
| `release-copilot` | `release-copilot:poc` | Unmodified `adk-release-agent` Dockerfile (FastAPI + ADK on :8000) |

The browser never talks to the agent directly; all traffic (including SSE chat
streams) goes through the Backstage proxy backend.

## Local dev (no docker)

```bash
cd backstage && yarn install && yarn start        # portal on :3000
PYTHONPATH=src:. .venv/bin/uvicorn release_agent.app_fastapi:app --port 8000
```
