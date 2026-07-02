# Per-session Repository + PAT Token input

## Motivation

Today Release Copilot targets a single, server-wide repository pair
(`RELEASE_BUILD_REPO` / `RELEASE_DEPLOY_REPO`) and authenticates with one
server-wide GitHub token (`GH_TOKEN` / `GITHUB_TOKEN`, or the `gh` CLI login).
Every chat session shares that configuration.

We want the workflow shown in the reference UI: at the start of a session the
end user supplies **their own** connection details and the agent operates
against *that* repo with *that* token, e.g.

1. Repository (URL or `owner/repo`)
2. Branch name
3. PAT token
4. Project name (optional)

This turns the copilot into a multi-tenant tool: different users can point it at
different repositories in different sessions without changing server config or
sharing a single privileged token.

## Where the current code resolves repo + auth

- **Auth** funnels through a single choke point:
  `release_agent.tools._common._resolve_github_token()` →
  `_get_github_client()`. Order today: `GH_TOKEN` → `GITHUB_TOKEN` →
  `gh auth token`.
- **Repo targeting** is read directly as `settings.build_repo` /
  `settings.deploy_repo` in ~15 call sites across `manifest.py`,
  `pull_requests.py`, `promotion.py`, `release_window.py`, `controls.py`, and
  `app_fastapi.py`.
- Everything is keyed by `thread_id`. The FastAPI app + ADK service are
  in-memory singletons that may serve concurrent sessions, so per-session state
  must be **concurrency-safe** — we must not mutate the global `settings`
  object.

## Design

### 1. Session credential store (`session_creds.py`)

- A process-local `dict[thread_id -> SessionCredentials]` (in-memory only,
  mirroring the existing in-memory ADK session/artifact/memory services).
- `SessionCredentials`: `repo`, `branch`, `pat_token`, `project_name`, plus a
  `build_repo`/`deploy_repo` split (a single `repo` maps to both unless the
  caller distinguishes them).
- A `contextvars.ContextVar[SessionCredentials | None]` holds the **active**
  credentials for the duration of one request. `contextvars` propagate across
  `await` and `asyncio.to_thread`, so deeply-nested sync tool code can read the
  override without threading a parameter through every function.
- An `activate(thread_id)` context manager binds the stored creds to the
  contextvar for the request and resets it on exit.

**Security posture**

- Tokens live only in memory, never written to disk, never persisted to the ADK
  memory service.
- Tokens are never logged and never returned to the client — the status API
  returns a masked preview (`ghp_…abcd`) only.
- `disconnect` / starting a new thread clears the stored token.
- Because the server holds user PATs in memory, production deployments should
  terminate TLS and add auth in front of the app (already noted in
  `app_fastapi.py`).

### 2. Context-aware resolvers (`_common.py`)

- `_resolve_github_token()` gains a first step: return the active session PAT
  from the contextvar if present, else fall back to the existing env / `gh`
  chain. This is the whole auth story — one edit, one choke point.
- Add `active_build_repo()` / `active_deploy_repo()` that return the session
  override when set, else `settings.build_repo` / `settings.deploy_repo`.
- Replace direct `settings.build_repo` / `settings.deploy_repo` reads in the
  tool modules with these resolvers.

### 3. API (`app_fastapi.py`)

- `POST /api/session/connect` `{thread_id, repo, branch?, pat_token, project_name?}`
  → validates + stores; returns masked status.
- `GET /api/session/status?thread_id=…` → `{connected, repo, branch,
  project_name, token_preview}`.
- `POST /api/session/disconnect` `{thread_id}` → clears creds.
- The chat stream activates the thread's creds for the duration of the SSE
  generator so every tool call in that turn sees the override.

### 4. UI (`static/app.js` + `app_fastapi.py` HTML)

- A "Connect Repository" panel collecting repo / branch / PAT (password field) /
  project name, shown on load and via a header button.
- Displays connection status (repo + masked token) once connected.
- The PAT is sent once to `/api/session/connect` and never stored in
  `localStorage` (only the non-secret repo/branch/project are cached for
  convenience).

## Rollout / compatibility

- Fully backward compatible: when no session creds are set, the agent uses the
  server-wide env config exactly as before.
- `RELEASE_REQUIRE_SESSION_REPO` (future flag) can make per-session connection
  mandatory before chatting; default off so existing single-tenant deployments
  are unaffected.

## Testing

- Unit: store set/get/clear, masking, contextvar activation/reset, token
  resolver precedence (session PAT beats env), repo resolver precedence.
- API: connect → status (masked) → disconnect round-trip.
