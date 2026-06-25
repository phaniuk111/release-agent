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

## 2. Quick Smoke Test (Read-only + Propose - Safe)

```bash
source .venv/bin/activate
python scripts/test_gh_tools.py
```

This exercises:
- Reading allowed images from `image-workflows.json`
- Reading the current manifest
- `propose_update(...)` (no writes to GitHub)

## 3. Test via CLI (Fast for iteration)

```bash
source .venv/bin/activate
python -m src.release_agent.cli
```

**Test against your https://github.com/phaniuk111 account:**

```
You: list allowed images

You: promote payments-api:2.0.99-test and orders-api:v9.9.9

# Agent shows proposal + token (e.g. CONFIRM-7k9p2)
You: CONFIRM-7k9p2
```

After confirmation the agent will:
- Commit changes to the manifest repo
- Dispatch a workflow (which creates a PR in the deployment repo)
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
- `promote payments-api:2.0.99-test`

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
promote payments-api:2.0.99-test
```

Paste the `CONFIRM-...` token when prompted.

### Verify on your GitHub account

- Repo: https://github.com/phaniuk111/devops
- Look for the new commit on `release-manifest.json`
- Check the **Actions** tab for the triggered run (image-tag-step-report.yml or similar)

CLI verification:

```bash
gh run list --repo phaniuk111/devops --limit 5
gh run view <RUN_ID> --repo phaniuk111/devops
```

## 6. Test Individual Components

### Test the Graph without LLM (dry)

You can temporarily comment out the LLM call or use mocks, but the easiest is to test the tool functions directly in Python:

```python
from src.release_agent.tools.gh_tools import propose_update, get_current_manifest

print(get_current_manifest())
print(propose_update("payments-api:2.0.99-test"))
```

### Test interrupt / confirmation logic

The confirmation flow is exercised automatically when you send a real message that reaches the `gate` node.

To test resume behavior, just send the confirmation token as the next message in the same thread.

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
- [ ] Proposes without mutating
- [ ] Shows clear `CONFIRM-xxx` token
- [ ] Only mutates after you confirm
- [ ] Updates files in https://github.com/phaniuk111/...
- [ ] Dispatches workflow on phaniuk111
- [ ] Returns commit + run links under phaniuk111
- [ ] New threads work
- [ ] Health check works

## Tips for Safe Testing (using phaniuk111)

- Use fake tags like `2.0.99-test` first time
- You can always revert `release-manifest.json` on GitHub after testing
- Monitor https://github.com/phaniuk111 directly while running tests
- Start with the read-only `python scripts/test_gh_tools.py`

## End-to-End Testing Subagent (Recommended for full validation)

We now have a dedicated **testing subagent** (`src/release_agent/testing_agent.py`) that can drive the main Release Copilot end-to-end and automatically verify the side effects.

### How to run it

```bash
source .venv/bin/activate
export GOOGLE_CLOUD_PROJECT=your-project
export GOOGLE_CLOUD_LOCATION=us-central1
export RELEASE_AGENT_TARGET_REPO=phaniuk111/devops
export DEPLOY_REPO=phaniuk111/devops   # or the repo where PRs land

# Run a full scenario
python -m src.release_agent.testing_agent --image-tags "payments-api:2.0.99-test" --scenario full-release-e2e
```

Or from Python:

```python
from release_agent.testing_agent import run_end_to_end_test
report = run_end_to_end_test(
    image_tags="payments-api:2.0.99-test,orders-api:v9.9.9-test",
    scenario_name="full-release-e2e"
)
print(report.model_dump_json(indent=2))
```

The tester will:
1. Send the release request to the copilot
2. Simulate the user confirmation (HITL)
3. Verify the manifest was updated
4. Find the PR created by the workflow in the deployment repo
5. Inspect PR comments for CHG tickets and RLFT control states (closed/opened)
6. Return a structured `TestReport` with pass/fail + evidence

You can also use the tester graph directly as a sub-agent if you want the main copilot to call it for self-testing.

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