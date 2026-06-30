# release-agent — LangGraph GitHub Release Copilot (PoV)

Chat with a LangGraph agent to drive your release process:

- Tell it image names + tags in plain English
- It reads/updates JSON configs in your GitHub repo
- Confirms before doing anything dangerous
- Triggers GitHub Actions workflows (real `workflow_dispatch`)
- Works locally with your `gh` auth and can target any repo under https://github.com/phaniuk111 (for testing we use phaniuk111/devops)

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
| `BUILD_REPO` | No — default `phaniuk111/devops` | env var | code + config + build repo: image-workflows.json, tags, build runs, RLFT/RFTL controls (legacy `RELEASE_AGENT_TARGET_REPO` still accepted) |
| `DEPLOY_REPO` | No — default = target repo | env var | repo where the deployment PR is opened |
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

# 4. Vertex AI Gen AI SDK configuration
# The code auto-detects project from GOOGLE_CLOUD_PROJECT or your gcloud config.
# The project name is **never hardcoded** in the source code.
export GOOGLE_CLOUD_LOCATION=us-central1
# Make sure gcloud ADC is active:
# gcloud auth application-default login
# (Project is taken from `gcloud config get-value project` if env not set)

# 5. Use your phaniuk111 account for testing
export BUILD_REPO=phaniuk111/devops

# 6. Run

# FastAPI Web UI (recommended)
uvicorn src.release_agent.app_fastapi:app --reload --port 8000
# Open http://localhost:8000

# or CLI
python -m src.release_agent.cli
```

Example chat:

```
You: promote payments-api:2.0.33 to prod please
Agent: Parsed: payments-api → 2.0.33
       Current release-manifest.json: ...
       Proposed diff: ...
       Reply exactly with: CONFIRM-7k9p2 to apply the JSON update and dispatch the workflow.
You: CONFIRM-7k9p2
Agent: ✅ Updated release-manifest.json (sha...)
       🚀 Dispatched workflow run #4872 (this created PR #42 in deployment repo)
       Ask me: "summarize PR 42" or "get controls status" to track CHG ticket and RLFT gates in the PR comments.
```

## PRD build-control gate (RLFT/RFTL pass/fail)

Before a production release, the agent fetches the **release controls recorded in
the tag's build pipeline** and reports each one PASS/FAIL — e.g. *RFTL0001: PASSED,
RFTL0002: FAILED*. A PRD release is **fail-closed**: if any control failed, it's blocked.

- **Trigger:** ask for a PRD release with an image:tag ("promote payments-api:v1.1.0 to prod") — controls are shown up front before the change-request step.
- **Auto-discovery:** the tag is resolved to its commit and the build-workflow run at that commit is found automatically.
- **Run id fallback:** if the run can't be located from the tag, the agent **asks the developer for the GitHub Actions run id** that generated the tag, then fetches controls from `get_build_controls(run_id=…)`.
- **Server-side enforcement:** `open_release_pr` re-checks controls for prod and refuses to open the PR if any failed (so the UI JSON-paste path is gated too).
- **Config:** `BUILD_REPO` (where build pipelines run; defaults to the target repo), `CONTROL_PREFIXES` (default `RLFT,RFTL`), `PRD_REQUIRE_CONTROLS` (default `true`).

Controls are GitHub Actions **steps** in the build job whose name starts with a
control prefix; pass/fail comes from each step's conclusion. The standalone
`verify_image_tag_build` check (tag-gen step + log marker) still exists for
build-authenticity verification.

## Daily release flow — SIT → UAT → PRD (accumulate, then cut at the cutoff)

The deploy repo has three branches: **SIT → UAT → PRD**. **They are protected — the
agent never commits to them directly; every change is a PR.** A "promote to prod /
add image" request opens a **PR chain (working branch → SIT → UAT)** so the image
lands on UAT, where the day's release set accumulates. The single **UAT → PRD**
release PR is raised **only after the daily cutoff** (default 16:00 UTC) — raising
it **locks the day**, so it must not happen earlier or no more images could be added.

> Auto-merge of the SIT/UAT PRs assumes the agent's token may merge those lower-env
> branches (CI-gated). If your protection requires human review there, the agent
> raises the PRs and they merge once approved; the PRD release PR is always left
> open for approval. Removal works the same way (a PR that drops the image).

- **Before the cutoff:** promote-to-prod stages onto UAT via the PR chain (no change request needed yet). Multiple developers keep adding all day.
- **Concurrent-promote guard:** before opening a promote PR the agent checks whether a PR touching the images config is **already open** — if so it refuses and reports that PR number instead of stacking a second, conflicting PR (the same approach Dependabot/Renovate use). One promote per file at a time; the next dev retries once it merges.
- **Deploy run surfaced:** when the `SIT → UAT` PR merges, the agent looks up the **GitHub Actions run** that the merge triggered (by the merge commit) and shows it in the chat as a clickable link — e.g. `staged on UAT — UAT deploy run #28425044664`. This requires a workflow in the deploy repo that runs on a UAT push, e.g. `on: push: branches: [UAT]` (a sample `deploy-uat.yml` with a `concurrency: { group: deploy-uat }` serialization guard lives in the deploy repo). If no such workflow exists, the run is simply omitted.
- **After the cutoff:** the prod request (or the `raise_prod_release` tool) raises the one UAT → PRD PR with the full UAT set; this needs a change request (drives the auto-created **CHG/RMG**) and locks the day.
- **Lead time:** the change request's `start_date` must be **tomorrow or later** (`PRD_LEAD_TIME_DAYS`, default 1) — a same-day production start is refused, no PR raised.
- **Nothing to release:** if UAT has no changes vs PRD (a quiet day), no PR is raised — the tool reports there is nothing to release rather than opening an empty PR. The agent is request-driven, so nothing auto-raises either.
- **Remove / unstage:** "remove `<image>` from the release" (`remove_from_release`) goes through the same PR chain as add — a PR from a working branch into **SIT** dropping the image, then a **SIT → UAT** promotion PR, both merged so the removal reaches UAT. Each image is reverted to PRD's current tag (or dropped if it was new). Branches are never edited directly.
- **Locked:** once today's UAT → PRD PR exists (open or merged), further adds are refused with a link to that PR.
- **Build-control gate:** every staged image is checked first — a failed RLFT/RFTL control blocks it (see above).

This is **shared across sessions** because the state lives in **GitHub itself**
(the UAT images config + the UAT→PRD PR), not in any in-process memory. The side
panel reads `GET /api/release-status` live and shows one of: 🧺 *collecting on UAT
(N images)* / ⏰ *cutoff passed — ready to raise* / 🔒 *raised & locked (PR #N)*,
refreshed on load, after every turn, and every 60s. Ask in chat: *"is there a PRD
release scheduled today?"* (`check_release_window`).

- **Config:** `SIT_BRANCH`/`UAT_BRANCH`/`PRD_BRANCH`, `PRD_CUTOFF_HOUR_UTC` (default `16`).

The per-session LangGraph checkpointer is only for *conversation* memory; the
release state is durable in GitHub, so it survives restarts and is consistent for
every developer without a separate database.

## What It Does Today (PoV)

- Uses existing `devops/image-workflows.json` to know valid images.
- Updates (or creates) `release-manifest.json` in the target repo with your image:tag values.
- Dispatches the `image-tag-step-report.yml` (or any workflow you configure) with the tags.
- All gh calls are via a tightly controlled tool layer — no arbitrary shell.

## Architecture Highlights

- Pure LangGraph `StateGraph` with explicit nodes + `interrupt()` for confirmation.
- **Transient-failure resilience:** a LangGraph `RetryPolicy` on the tool/LLM nodes (retries only network blips, 5xx, and rate limits — never 404/422/auth), plus HTTP-level retry on the PyGithub client (idempotent methods only, so PR creation is never double-fired).
- **ReAct loop guard:** the free-form chat lane is capped at `REACT_MAX_TOOL_TURNS` tool turns (default 8); on hitting it the agent stops gracefully with a "narrow the request" message instead of running to the recursion limit and crashing.
- Tools are thin, validated wrappers around `gh` (subprocess, list args only).
- **FastAPI** as the main interface (chosen for production readiness).
- Streaming support (SSE) so the chat feels responsive.
- Threaded conversations (MemorySaver for PoV → Postgres checkpointer recommended for prod).

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

The container expects:
- `GH_TOKEN` (or mounted GitHub App credentials) — least-privilege scopes
- Vertex AI: `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` (uses Application Default Credentials / Workload Identity)
- `BUILD_REPO`
- Optionally a shared Postgres for the checkpointer (for conversation persistence across restarts).

## Safety

- Mutating actions only after explicit confirmation token shown in the same thread.
- Whitelisted operations only.
- Full URLs and commit SHAs echoed back.

## Budget Protection (Vertex AI)

Hard limit: **£10**.

- Before every LLM call, estimated cost is checked.
- If approaching or exceeding £10, the agent will interrupt and ask you to confirm.
- If you do not respond within ~45 seconds (CLI), the process will **self-terminate** to protect your budget.
- Current spend is always shown in interrupts.

Project is resolved dynamically from GOOGLE_CLOUD_PROJECT env or your local gcloud config (no hardcoding in code).

## How to Test

Full testing guide is in [TESTING.md](./TESTING.md).

### Quick Start Testing

```bash
source .venv/bin/activate
export BUILD_REPO=phaniuk111/devops
export GOOGLE_CLOUD_PROJECT=<your-gcp-project>   # for Vertex AI
gh auth login                                    # repo + workflow scopes

# CLI test
python -m src.release_agent.cli

# Web UI test (recommended)
uvicorn src.release_agent.app_fastapi:app --reload --port 8000
# Open http://localhost:8000
```

Example message to test the full flow:
- `promote payments-api:2.0.99-test`
- Wait for proposal + token
- Paste the `CONFIRM-...` token

See `scripts/test_gh_tools.py` for a safe read-only smoke test.

### No-LLM tool testing

Exercise every tool directly — no Vertex/Gemini config needed. The runner imports
only the PyGithub tool layer (never the LLM or the graph), so it works with just a
GitHub token + the repo env vars:

```bash
export DEPLOY_REPO=phaniuk111/deployment-repo BUILD_REPO=phaniuk111/gh-image-tag-report-test
# (GH_TOKEN or `gh auth login` — no GOOGLE_CLOUD_PROJECT required)

PYTHONPATH=src python -m release_agent.tools_cli                       # list all tools + args
PYTHONPATH=src python -m release_agent.tools_cli get_build_controls    # show one tool's schema
PYTHONPATH=src python -m release_agent.tools_cli get_build_controls image=payments-api tag=v1.5.0
PYTHONPATH=src python -m release_agent.tools_cli find_prs '{"search_term":"payments-api"}'
```

Read/query tools are safe to run. The mutating tools (`open_release_pr`,
`apply_json_update`, `dispatch_workflow`, `remove_from_release`, `raise_prod_release`,
`retrigger_deployment_workflow`) **execute for real** — this runner bypasses the
human-confirmation gate by design. Add `--dry-run` to simulate them without executing
(read-only tools still run), so you can sweep the whole toolset safely:

```bash
PYTHONPATH=src python -m release_agent.tools_cli --dry-run open_release_pr environment=uat image_tags=x:1
```

## Extending for Real Releases

Replace or augment the dispatch with a workflow that:
1. Reads the `release-manifest.json` you just wrote
2. Renders/updates Helm values, ArgoCD apps, Terraform, or other prod config files
3. Creates the PR or applies the change + runs your RLFT gates

The chatbot stays the conversational "front door".

## Next

- Add more workflows as inputs
- Support multiple target repos/environments
- Chainlit UI variant
- Real GitHub App auth + fine-grained permissions
- Postgres checkpointer + history UI

## End-to-End Testing Subagent

A dedicated testing subagent lives at `src/release_agent/testing_agent.py`.

It can drive the main copilot, handle confirmations, verify manifest changes, locate the PR created by the workflow, and inspect PR comments for CHG tickets + control states.

Run it with:

```bash
python -m src.release_agent.testing_agent --image-tags "payments-api:2.0.99-test"
```

See `TESTING.md` for full documentation and how to use it programmatically as a sub-agent.

**For realistic testing (separate deployment repo):** Follow `DEPLOYMENT_REPO_SETUP.md`. It creates a distinct repo where PRs land, and includes a workflow that posts comments with image names + simulated controls when merged to main.
