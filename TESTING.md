# How to Test the Release Copilot

**We are using https://github.com/phaniuk111 for testing** (your GitHub account).

- Manifest/config repo (default): `phaniuk111/devops`
- Deployment / PR repo: also defaults to `phaniuk111/devops` (set `DEPLOY_REPO` if different)

The agent can now **track PRs** created by the workflow, read comments, and summarize:
- CHG ticket references
- Control states (RLFT gates closed/opened, etc.)

This guide covers testing from basic smoke tests to full end-to-end (real file updates + workflow dispatches on your phaniuk111 repos).

## 1. Prerequisites (Isolated Installation Only)

**All Python packages live inside `.venv` in this directory only.**
Never run `pip install` outside an activated venv.

```bash
cd release-agent

# One-time isolated setup (recommended)
./setup.sh

# Then activate
source .venv/bin/activate

# Required - Vertex AI Gen AI
# Do NOT hardcode the project. It is auto-detected from your gcloud + ADC
# export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_CLOUD_LOCATION=us-central1
# gcloud auth application-default login

# GitHub target
export RELEASE_AGENT_TARGET_REPO=phaniuk111/devops

# Authenticate as phaniuk111
gh auth login

# Make sure the token has these scopes for full testing:
# - repo (contents: write)
# - workflow
```

## 2. Quick Smoke Test (Read-only + Safe)

```bash
source .venv/bin/activate
python scripts/test_gh_tools.py
```

This exercises:
- Reading allowed images from `image-workflows.json`
- Reading live release/deployment state
- Read-only GitHub status and PR lookups

## 3. Test via CLI (Fast for iteration)

```bash
source .venv/bin/activate
python -m src.release_agent.cli
```

**Test against your https://github.com/phaniuk111 account:**

```
You: list allowed images

You: deploy payments-api:2.0.99-test to uat

# Agent shows exact deployment JSON + token (e.g. CONFIRM-7K9P2)
You: CONFIRM-7K9P2
```

After confirmation the agent will:
- Open the protected-branch release PR path in the deployment repo
- Surface the PR/deploy-run links when available
- Return direct links

You can then continue chatting:
- "find the PR for payments-api:2.0.99-test"
- "summarize controls on the latest PR"
- "get PR comments for 42"

Restart the CLI for a fresh thread.

## 4. Test via FastAPI Web UI (Recommended for chatbot feel)

```bash
source .venv/bin/activate
uvicorn src.release_agent.app_fastapi:app --reload --port 8000
```

Open **http://localhost:8000**

Test the full flow using messages like:
- `deploy payments-api:2.0.99-test to uat`

The UI will stream and show a confirmation box with the token.

All activity will target repos under **https://github.com/phaniuk111**.

## 5. Full End-to-End Test (Real actions on phaniuk111)

This performs **real writes** and workflow dispatches under https://github.com/phaniuk111.

Use safe tags like `2.0.99-test` the first time.

### Run it

```bash
uvicorn src.release_agent.app_fastapi:app --reload --port 8000
```

In the browser chat send:
```
deploy payments-api:2.0.99-test to uat
```

Paste the `CONFIRM-...` token when prompted.

### Verify on your GitHub account

- Repo: https://github.com/phaniuk111/devops
- Look for the release PR that updates `uat/deployment.json`
- Check the **Actions** tab for the deployment workflow run, if configured

CLI verification:

```bash
gh pr list --repo phaniuk111/devops --limit 10
gh run list --repo phaniuk111/devops --limit 5
```

## 6. Test Individual Components

### Test the tool layer without LLM/ADK (dry)

The easiest low-level check is to test the tool functions directly in Python:

```python
from adk_release_agent.deploy import prepare_deploy_preview

print(prepare_deploy_preview(image_tags="payments-api:2.0.99-test", environment="uat"))
```

### Test interrupt / confirmation logic

The confirmation flow is exercised automatically when you send a deploy message through the ADK-backed CLI or FastAPI app.

To test resume behavior, just send the confirmation token as the next message in the same thread.

### Automated test suite (no LLM / no GitHub needed)

The ADK runtime is covered by `pytest` — including the deploy `Workflow` graph
(preview → HITL confirm → apply | cancel) driven end-to-end through `InMemoryRunner`,
the `MutationGuardPlugin`, the skills→tools wiring, and the ADK 2.x feature toggles:

```bash
PYTHONPATH=src:. python -m pytest -q
# focused:
PYTHONPATH=src:. python -m pytest tests/test_deploy_workflow.py tests/test_adk_features.py \
                                  tests/test_safety_and_skills.py -q
```

These need neither Vertex/Gemini nor a GitHub token — the deploy Workflow nodes are pure
Python and the confirmation plumbing is tested against synthetic ADK events.

## 7. Docker / Kubernetes Testing

Build and run locally:

```bash
docker build -t release-copilot .
docker run -p 8000:8000 \
  -e GOOGLE_CLOUD_PROJECT=$GOOGLE_CLOUD_PROJECT \
  -e GOOGLE_CLOUD_LOCATION=$GOOGLE_CLOUD_LOCATION \
  -e RELEASE_AGENT_TARGET_REPO=phaniuk111/devops \
  -e GH_TOKEN=$GH_TOKEN \
  release-copilot
```

For Kubernetes:

```bash
# Apply manifests (update secrets first)
kubectl apply -f k8s/
kubectl port-forward svc/release-copilot 8000:80
```

Then test the same way via `http://localhost:8000`.

Check logs:

```bash
kubectl logs -l app=release-copilot -f
```

## 8. What to Test (Checklist)

- [ ] Reads allowed images correctly from your phaniuk111 repo
- [ ] Parses image:tag from chat
- [ ] Previews exact deployment JSON without mutating
- [ ] Shows clear `CONFIRM-xxx` token
- [ ] Only mutates after you confirm
- [ ] Opens/updates the expected release PR path in https://github.com/phaniuk111/...
- [ ] Surfaces workflow/deploy-run links when configured
- [ ] Returns commit + run links under phaniuk111
- [ ] New threads work
- [ ] Health check works

## Tips for Safe Testing (using phaniuk111)

- Use fake tags like `2.0.99-test` first time
- You can always close the generated release PR or revert the deployment JSON change after testing
- Monitor https://github.com/phaniuk111 directly while running tests
- Start with the read-only `python scripts/test_gh_tools.py`

## Debugging

Enable more logs:

```bash
export LOG_LEVEL=DEBUG
```

Or run with verbose Python:

```bash
python -m src.release_agent.cli
```

For the web UI, check the terminal output from uvicorn.

---

If you run into issues (e.g. authentication errors with Vertex AI), make sure `gcloud auth application-default login` has been run and GOOGLE_CLOUD_PROJECT is set.
