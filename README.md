# release-agent — ADK GitHub Release Copilot (PoV)

Chat with an ADK agent to drive your release process:

- Tell it a Helm chart + version (or edit the deployment JSON directly) to deploy
- It writes the env-pathed `deployment.json` in your GitHub deploy repo
- Confirms before doing anything dangerous (HITL token gate)
- Opens the protected-branch PR chain that deploys to UAT / PRD
- Works locally with your `gh` auth; the build & deploy repos come from configuration (`.env` or env vars / Helm), never hardcoded in the source

Designed to run as a chatbot-style app in Kubernetes.

## Required inputs / credentials

To run the agent you need **GitHub auth**, **GCP / Vertex AI access**, and a few env vars.

| Input | Required? | How to provide | Purpose |
|-------|-----------|----------------|---------|
| **GitHub token** — `GH_TOKEN` (or `GITHUB_TOKEN`) | **Yes** | env var, or `gh auth login` locally (auto-detected) | commit the manifest, dispatch workflows, open & track PRs, read PR comments |
| **GCP project** — `GOOGLE_CLOUD_PROJECT` | **Yes** | env var (locally falls back to `gcloud config get-value project`) | Vertex AI Gemini calls |
| **Vertex ADC** (auth) | **Yes** | local: `gcloud auth application-default login` · GKE: **Workload Identity** on the KSA | authenticate to Vertex AI |
| `GOOGLE_CLOUD_LOCATION` | No — default `us-central1` | env var | Vertex region |
| `GEMINI_MODEL` | No — default `gemini-2.5-flash` | env var | model id (`gemini-2.0-flash` is retired) |
| `BUILD_REPO` | **Yes** — no hardcoded default | `.env`, env var, or Helm ConfigMap | code + image catalog (`image-workflows.json`), tags, build runs, RLFT/RFTL controls (legacy `RELEASE_AGENT_TARGET_REPO` still accepted) |
| `DEPLOY_REPO` | **Yes** — no hardcoded default | `.env`, env var, or Helm ConfigMap | deploy repo: SIT/UAT/PRD protected branches + `uat/deployment.json` & `prd/deployment.json` the deploy PR chain overrides |
| `DEFAULT_WORKFLOW` | No — default `image-tag-step-report.yml` | env var | workflow dispatched on promote |
| **`DEPLOY_PAT`** (repo **Secret**) | Only for the **cross-repo PR** | Actions secret on the **target** repo | lets the dispatched workflow open a PR in `DEPLOY_REPO` (GitHub's built-in `GITHUB_TOKEN` can't write across repos) |

**GitHub PAT scopes:** classic PAT with **`repo`** + **`workflow`**, or a fine-grained PAT with
**Contents**, **Pull requests**, and **Actions** (read/write) on the target & deploy repos.

- **Local:** `gh auth login` is enough (the app borrows the token via `gh auth token`); or `export GH_TOKEN=ghp_...`.
- **Docker / GKE:** there's no `gh` login in the image — provide `GH_TOKEN` (the Helm chart wires it from a Secret) and a GCP project + Workload Identity. See [helm/release-copilot/README.md](./helm/release-copilot/README.md).
- **CI (image build):** the [`build-image`](.github/workflows/build-image.yml) workflow needs **no extra secrets** — it pushes to GHCR using the built-in `GITHUB_TOKEN`.

## Quick Start (Local PoV) — Fully Isolated

**Strict isolation**: All Python packages and the virtual environment live **only** inside `release-agent/.venv`.
Nothing is installed globally. The `.gitignore` protects `.venv`, `.env`, etc.

```bash
# 1. Prerequisites (gh CLI can be system-wide, everything else is local)
gh auth login          # or export GH_TOKEN=ghp_...
# Needs repo + workflow scopes

# 2. One-command isolated setup (creates .venv + installs inside it)
./setup.sh

# 3. Activate the local venv
source .venv/bin/activate

# 4. Configuration — copy the template and fill in your values.
#    Nothing is hardcoded in the source; config is read from .env (gitignored),
#    real env vars, or (in-cluster) the Helm ConfigMap, in that precedence.
cp .env.example .env
#    Edit .env: set BUILD_REPO, DEPLOY_REPO, and GOOGLE_CLOUD_PROJECT.

# 5. Auth (locally): gcloud ADC for Vertex + gh login for the GitHub token
gcloud auth application-default login
gh auth login            # or set GH_TOKEN in .env

# 6. Run
#    PYTHONPATH must include both `src` (release_agent) and `.` (adk_release_agent).
#    Vertex is selected via GOOGLE_GENAI_USE_VERTEXAI=True (set in .env or exported).

# FastAPI Web UI (recommended)
PYTHONPATH=src:. uvicorn release_agent.app_fastapi:app --reload --port 8000
# Open http://localhost:8000

# or CLI
PYTHONPATH=src:. python -m release_agent.cli
```

Example chat (or use the **Deploy to UAT / PROD** form, which submits the same JSON):

```
You: deploy abc-client-api-svc:1.1.1230 to prod
Agent: Deploy abc-client-api-svc:1.1.1230 to PROD — will OVERRIDE
       `uat/deployment.json + prd/deployment.json`:
       { "uat/deployment.json": [ {helm_chart_name, helm_chart_version, helm_chart_dir,
         helm_values_file_name: "uat/values_uat.yaml", gke_namespace: "eod1"} ],
         "prd/deployment.json": [ {... helm_values_file_name: "prd/values_prd.yaml", ...} ] }
       Reply CONFIRM-7k9p2 to confirm.
You: CONFIRM-7k9p2
Agent: ✅ deployed to PROD (uat/deployment.json, prd/deployment.json)
       — UAT run #4872; PRD run #4873.  PRs: working→SIT (merged); SIT→UAT (merged); UAT→PRD (merged).
```

## Deploy flow — Helm charts to UAT / PRD

The unit of deploy is a **Helm-chart entry** written into an env-pathed deployment
JSON in the deploy repo: `uat/deployment.json` and `prd/deployment.json`, each shaped
`{"include": [entry, ...]}` where an entry is:

```json
{ "helm_chart_name": "abc-client-api-svc", "helm_chart_version": "1.1.1230",
  "helm_chart_dir": "hlm-all/com/db/eod-ds", "helm_values_file_name": "uat/values_uat.yaml",
  "gke_namespace": "eod1" }
```

The dev supplies only **chart name + version** (+ namespace); the agent fills the
constant `helm_chart_dir` and the env-specific `helm_values_file_name` + `gke_namespace`
from config. Entries are keyed by `helm_chart_name` (one per chart per env file).

**The env branches (SIT → UAT → PRD) are protected — the agent never commits to them
directly; every change is a PR chain.**

- **Deploy to UAT** OVERRIDES `uat/deployment.json` with the submitted `include[]` via
  the chain `working → SIT → UAT` — a complete file replace, **no upsert/merge**, so the
  file always reflects exactly the desired set.
- **Deploy to PROD** does NOT write PRD directly. It **adds the chart to today's PRD
  release PR** — a single day-long PR on a `release/prd/<date>` branch that **accumulates**
  (upsert by chart name) **both** `uat/deployment.json` and `prd/deployment.json`. Every
  prod deploy through the day adds to the same open PR (the staging view).
- **Release to PROD (promote at cutoff).** Production is **never written directly and never
  skips SIT/UAT.** After the daily cutoff (`PRD_CUTOFF_HOUR_UTC`, default 16:00 UTC), and
  only when asked — say *"release prod"* (→ `merge_prod_release`, also a UI quick-action) —
  the staged charts are **promoted through the full chain `… → SIT → UAT → PRD`** (a fresh
  branch cut from current SIT, upserted so existing prod charts are preserved — no
  stale-merge conflict against intraday UAT deploys). Before the cutoff it's refused and
  the PR stays open; if a protected hop needs review the staging PR stays open until PRD
  merges.
- **Editable JSON, multi-chart.** The UI "Deploy to UAT / PROD" buttons open the **whole
  `{"include":[...]}` file as an editable JSON box** — add entries to deploy several
  charts at once. Typing a deploy command in chat (e.g. `abc:1.2.3 promote to uat`) opens
  the **same editor pre-filled**. Submit is deterministic (no LLM, no NL ambiguity): the
  ADK deploy flow previews the exact file it will write, then a `CONFIRM-xxxxxx` token gates the
  write (HITL).
- **Concurrent-deploy guard:** before opening a PR the agent checks whether a PR
  touching the deployment JSON is **already open** — if so it refuses and reports that
  PR number instead of stacking a conflicting one (Dependabot/Renovate-style).
- **Deploy run surfaced:** when a `SIT → UAT` (and `UAT → PRD`) PR merges, the agent
  looks up the GitHub Actions run the merge triggered (by merge commit) and shows it as
  a clickable link. Needs a workflow on a UAT/PRD push (e.g. `deploy-uat.yml`,
  `on-merge-deploy.yml`); otherwise the run is simply omitted.
- **Remove / unstage:** `remove_from_release(image_names, environment)` drops the chart
  by `helm_chart_name` via the same PR chain — `uat` removes from `uat/deployment.json`;
  `prod` removes from **both** files.

This is **shared across sessions** because the state lives in **GitHub itself** (the
deployment JSONs + the open PRD release PR), not in any in-process memory. The side panel
reads `GET /api/release-status` live and shows what's live on UAT/PRD and **today's PRD
release PR** (the charts staged for prod + whether it can be merged yet), refreshed on
load, after each turn, and every 60s. Ask in chat: *"what's deployed to prod?"* or *"what's
in today's PRD release PR?"* (`check_release_window`).

- **Config:** `SIT_BRANCH`/`UAT_BRANCH`/`PRD_BRANCH`, `DEPLOYMENT_PATH_PATTERN`
  (`{env}/deployment.json`), `HELM_CHART_DIR`, `HELM_VALUES_PATTERN`, `PRD_CUTOFF_HOUR_UTC`
  (`{env}/values_{env}.yaml`), `UAT_NAMESPACE`/`PRD_NAMESPACE`.

The per-session ADK state is only for *conversation* memory; the deploy state is
durable in GitHub, so it survives restarts and is consistent for every developer
without a separate database.

## What It Does Today (PoV)

- Takes Helm chart `name:version` entries (one or more) and OVERRIDES the full deployment
  file — `uat/deployment.json` (UAT) or **both** `uat/` + `prd/deployment.json` (PROD).
- Drives the SIT → UAT → PRD promotion as protected-branch PR chains in `DEPLOY_REPO`.
- Surfaces the GitHub Actions deploy run each merge triggers.
- All GitHub calls go through a tightly controlled **PyGithub** tool layer — no arbitrary shell.

## Architecture

Two clearly separated lanes run on **Google ADK 2.x**:

1. **Deterministic deploy Workflow — the LLM is never the mutation gate.**
   Deploy / add / promote intent is detected deterministically and routed into an ADK
   `Workflow` graph (`adk_release_agent/deploy_workflow.py`):
   `START → preview → (HITL confirm) → apply | cancel`. The confirm step is an ADK
   `RequestInput` human-in-the-loop node — the graph **pauses** until the user replies
   with the exact `CONFIRM-xxxx` token shown in the thread, then applies (or cancels).
   The terminal output is validated by a typed `output_schema` (`DeployOutcome`). The
   nodes are plain Python (no LLM), so the whole flow is deterministic and unit-testable.

2. **Skills-routed chat agent for everything else.** A single ADK `Agent` uses filesystem
   **Skills** as the router: each `SKILL.md` declares its `adk_additional_tools`, so a
   domain's tools are surfaced to the model **only when that skill activates**:
   - `release-status` / `release-pr` / `release-controls` — **read-only**
   - `release-ops` — scoped mutations only: `remove_from_release`,
     `retrigger_deployment_workflow`, `merge_prod_release`
   - `release-deploy` — **tool-less guard**; defers to the deterministic Workflow

   The agent is wrapped in an ADK `App` with a **`MutationGuardPlugin`** that blocks the
   release-defining mutations (`open_release_pr`, `apply_json_update`, `dispatch_workflow`,
   `apply_confirmed_deploy`) from the free-form chat path **in code** — not just by prompt.
   High-impact prod ops (`merge_prod_release`, a **prod** `remove_from_release`) require an
   ADK tool-confirmation before they run.

- **PyGithub** for all GitHub operations — no `gh` subprocess in the tool layer; the
  token is resolved from `GH_TOKEN`/`GITHUB_TOKEN`, falling back to `gh auth token`.
- **Transient-failure resilience:** HTTP-level retry on the PyGithub client (idempotent
  methods only, so PR creation is never double-fired).
- **FastAPI** single interface with SSE streaming; threaded conversations via ADK
  in-memory session/artifact services for PoV (shared storage recommended for prod).
- **Config-driven:** repos, GCP project, branches, paths, namespaces, etc. come from `.env` /
  env vars / the Helm ConfigMap — nothing org-specific is hardcoded in the source.

### Project structure

```
src/release_agent/
  agent/
    parsing.py      #   pure NL intent parsing (no graph/LLM state)
  tools/            # GitHub tool layer (PyGithub), split by domain behind a facade
    _common.py  manifest.py  pull_requests.py  controls.py  release_window.py  promotion.py
    gh_tools.py     #   re-export facade that assembles GH_TOOLS
  config.py  adk_service.py  app_fastapi.py  cli.py  tools_cli.py
```

### ADK runtime

Requires **Python 3.11+** and **google-adk 2.3+**. The runtime ADK app lives at
`adk_release_agent/`:

```
adk_release_agent/
  agent.py                  # skills-routed root Agent + App (safety plugin, caching, compaction, memory)
  deploy_workflow.py        # deterministic deploy Workflow graph (preview → HITL confirm → apply | cancel)
  deploy.py                 # deploy preview/token/apply helpers used by the Workflow nodes
  safety.py                 # MutationGuardPlugin — blocks release-defining mutations in free-form chat
  tools.py                  # ADK Function Tool wrappers over the existing GitHub tool layer
  skills/*/SKILL.md         # ADK Skills (status, pr, controls, ops, deploy) with adk_additional_tools
```

Install the ADK dependency and run it with:

```bash
PYTHONPATH=src:. adk run adk_release_agent
```

The FastAPI `/api/chat` route is backed by an ADK service adapter
(`src/release_agent/adk_service.py`) that drives two ADK runtimes sharing in-memory
session/artifact/memory services: the **skills-routed chat `App`** for questions and
scoped ops, and the **deploy `Workflow`** for the deterministic preview → confirm →
apply sequence. Deploy intent never reaches the LLM.

#### ADK 2.x runtime features (all env-toggleable — see the `ADK_*` vars)

| Feature | What it does |
|---------|--------------|
| **Skills as router** | `SKILL.md` `adk_additional_tools` surface a domain's tools only on skill activation (one agent, no sub-agent sprawl). |
| **Deploy `Workflow` graph** | `RequestInput` HITL confirm node + typed `output_schema` (`DeployOutcome`); pure-function nodes, no LLM. |
| **`App` + `MutationGuardPlugin`** | Release-defining mutations blocked from free-form chat in code (`before_tool_callback`). |
| **Context caching** (`ContextCacheConfig`) | Caches the static prefix (root instruction + skill catalog) to cut latency/cost. `ADK_CONTEXT_CACHE`. |
| **Event compaction** (`EventsCompactionConfig`) | Summarizes older events on long chats to avoid context overflow. `ADK_EVENT_COMPACTION`. |
| **Memory** (`preload_memory` + `add_session_to_memory`) | Recalls relevant context each turn; persists finished sessions to the **in-memory** memory service. `ADK_MEMORY_ENABLED`. |
| **Conditional confirmation** (`require_confirmation`) | `merge_prod_release` always confirms; `remove_from_release` confirms **only for prod**. `ADK_CONFIRM_PROD_OPS`. |

Memory and sessions are **in-memory (per-pod, ephemeral)** by design — see the scaling
note below. A PVC does not persist the in-memory memory service (it is pure RAM); durable
storage would use ADK's `DatabaseSessionService` (SQLite/Postgres), not a PVC-mounted file.

**Production note**: FastAPI is the single interface — async, scalable, easy to instrument, and the standard choice for Python services running in Kubernetes.

See the specs in the [design/](./design/) folder:
- [design/spec.md](./design/spec.md) — Focused implementation spec
- [design/DESIGN.md](./design/DESIGN.md) — Full detailed technical design document + PR plan (generated)
- [design/DESIGN_SUMMARY.md](./design/DESIGN_SUMMARY.md) — Concise summary of architecture and decisions

## Kubernetes & Production

See `k8s/` and `Dockerfile`.

Recommended runtime:
- Use `uvicorn ... --workers 2-4` (or gunicorn with uvicorn workers) depending on CPU.
- The FastAPI app exposes `/health` for liveness/readiness probes.
- Port 8000 by default.

The container expects (all via the Helm `config:` block → ConfigMap, plus a Secret for the token):
- `GH_TOKEN` (or mounted GitHub App credentials) — least-privilege scopes
- Vertex AI: `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` (uses Application Default Credentials / Workload Identity)
- `BUILD_REPO` and `DEPLOY_REPO`
- Optionally shared ADK session/artifact storage for conversation persistence across restarts.

## Safety

- Mutating actions only after explicit confirmation token shown in the same thread.
- Whitelisted operations only.
- Full URLs and commit SHAs echoed back.

## Budget Protection (Vertex AI)

The current ADK runtime does not yet enforce a spend gate around every ADK model call.

- Keep using cheap Gemini Flash-class models for the ADK runtime.
- The ADK `App` plugin hook is already in place (`MutationGuardPlugin` in
  `adk_release_agent/safety.py`). A spend cap is a drop-in second plugin implementing
  `before_model_callback` — add it before production scale-out if a strict cap is required.
- Context caching (`ADK_CONTEXT_CACHE`) already trims repeated-prefix token cost.

Project is resolved dynamically from GOOGLE_CLOUD_PROJECT env or your local gcloud config (no hardcoding in code).

## How to Test

Full testing guide is in [TESTING.md](./TESTING.md).

### Quick Start Testing

```bash
source .venv/bin/activate
cp .env.example .env     # set BUILD_REPO, DEPLOY_REPO, GOOGLE_CLOUD_PROJECT
gh auth login            # repo + workflow scopes (or put GH_TOKEN in .env)

# CLI test
PYTHONPATH=src:. python -m release_agent.cli

# Web UI test (recommended)
PYTHONPATH=src:. uvicorn release_agent.app_fastapi:app --reload --port 8000
# Open http://localhost:8000
```

Example message to test the full flow:
- `deploy abc-client-api-svc:1.1.1230 to uat` (or use the **Deploy to UAT** form)
- Wait for the assembled-entry preview + token
- Paste the `CONFIRM-...` token

See `scripts/test_gh_tools.py` for a safe read-only smoke test.

### No-LLM tool testing

Exercise every tool directly — no Vertex/Gemini config needed. The runner imports
only the PyGithub tool layer (never the LLM or ADK runner), so it works with just a
GitHub token + the repo env vars:

```bash
# BUILD_REPO + DEPLOY_REPO from your .env (or export them); GH_TOKEN or `gh auth login`.
# No GOOGLE_CLOUD_PROJECT required — this runner never touches the LLM.

PYTHONPATH=src python -m release_agent.tools_cli                       # list all tools + args
PYTHONPATH=src python -m release_agent.tools_cli get_build_controls    # show one tool's schema
PYTHONPATH=src python -m release_agent.tools_cli get_build_controls image=payments-api tag=v1.5.0
PYTHONPATH=src python -m release_agent.tools_cli find_prs '{"search_term":"payments-api"}'
```

Read/query tools are safe to run. The mutating tools (`open_release_pr`,
`apply_json_update`, `dispatch_workflow`, `remove_from_release`,
`retrigger_deployment_workflow`) **execute for real** — this runner bypasses the
human-confirmation gate by design. Add `--dry-run` to simulate them without executing
(read-only tools still run), so you can sweep the whole toolset safely:

```bash
PYTHONPATH=src python -m release_agent.tools_cli --dry-run open_release_pr environment=uat image_tags=x:1
```

## Extending for Real Releases

Replace or augment the dispatch with a workflow that:
1. Reads the updated `uat/deployment.json` / `prd/deployment.json`
2. Renders/updates Helm values, ArgoCD apps, Terraform, or other prod config files
3. Applies the change through your protected release workflow + runs your RLFT gates

The chatbot stays the conversational "front door".

## Next

- Add more workflows as inputs
- Support multiple target repos/environments
- Chainlit UI variant
- Real GitHub App auth + fine-grained permissions
- Shared ADK session storage + history UI

**For realistic testing (separate deployment repo):** Follow `DEPLOYMENT_REPO_SETUP.md`. It creates a distinct repo where PRs land, and includes a workflow that posts comments with image names + simulated controls when merged to main.
