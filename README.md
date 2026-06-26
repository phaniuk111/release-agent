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
| `RELEASE_AGENT_TARGET_REPO` | No — default `phaniuk111/devops` | env var | manifest / source repo (also accepts `TARGET_REPO`) |
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
export RELEASE_AGENT_TARGET_REPO=phaniuk111/devops

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

## What It Does Today (PoV)

- Uses existing `devops/image-workflows.json` to know valid images.
- Updates (or creates) `release-manifest.json` in the target repo with your image:tag values.
- Dispatches the `image-tag-step-report.yml` (or any workflow you configure) with the tags.
- All gh calls are via a tightly controlled tool layer — no arbitrary shell.

## Architecture Highlights

- Pure LangGraph `StateGraph` with explicit nodes + `interrupt()` for confirmation.
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
- `RELEASE_AGENT_TARGET_REPO`
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
export RELEASE_AGENT_TARGET_REPO=phaniuk111/devops
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
