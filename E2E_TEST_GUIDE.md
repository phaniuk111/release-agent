# End-to-End Test Guide (Local UI → Real GitHub)

This guide walks you through testing the full flow locally:

1. Chat in the UI with image name + tag.
2. Agent updates the manifest JSON in your source repo.
3. Agent dispatches a workflow.
4. Workflow creates a PR in the separate deployment repo.
5. You can review/merge the PR.
6. On merge (or via retrigger), the deployment workflow posts comments with image details + simulated controls.
7. From chat, you can retrigger the deployment workflow.

---

## Prerequisites (do these first)

```bash
cd release-agent
source .venv/bin/activate
```

### 1. Google / Vertex AI (using your gcloud)
```bash
gcloud auth application-default login
gcloud config set project your-gcp-project
```

### 2. GitHub CLI
```bash
gh auth login
# Make sure it has at least: repo, workflow scopes
```

### 3. Environment variables (critical)
```bash
export GOOGLE_CLOUD_PROJECT=your-gcp-project
export GOOGLE_CLOUD_LOCATION=us-central1

# Separate repos (as you requested)
export RELEASE_AGENT_TARGET_REPO=phaniuk111/devops          # your manifest/source repo
export DEPLOY_REPO=phaniuk111/deployment-repo               # the separate deployment repo

# Optional
export RELEASE_MANIFEST_PATH=release-manifest.json
```

---

## Step 1: Prepare the two GitHub repos

### A. Deployment repo (separate)

If you haven't pushed it yet:

```bash
cd deployment-repo
git init -b main
git add .
git commit -m "Initial deployment simulation"
git remote add origin https://github.com/phaniuk111/deployment-repo.git
git push -u origin main
```

Then copy the PR-creator workflow into your **source** repo:

```bash
# From release-agent root
cp deployment-repo/examples/create-deployment-pr.yml \
   ../phaniuk111-devops/.github/workflows/create-deployment-pr.yml   # adjust path to your local clone of source repo
```

Commit and push that file to `phaniuk111/devops`.

### B. Make sure your source repo has the manifest file

The agent will update `release-manifest.json` (or whatever `RELEASE_MANIFEST_PATH` points to).

---

## Step 2: Start the UI

```bash
uvicorn src.release_agent.app_fastapi:app --reload --port 8000
```

Open: **http://localhost:8000**

---

## Step 3: Run the end-to-end test from chat

### Test message 1: Trigger update + dispatch

Type in the chat:

```
promote payments-api:2.0.99-test,orders-api:v9.9.9-test
```

**What should happen:**
- Agent parses the images.
- Shows a proposal (current vs new manifest).
- Asks for confirmation token (e.g. `CONFIRM-abc123`).
- You reply with the exact token.
- Agent calls `apply_json_update` → commits to your `RELEASE_AGENT_TARGET_REPO`.
- Agent calls `dispatch_workflow` (you may need to guide it to use `create-deployment-pr.yml`).

If the agent dispatches the wrong workflow name, just say in chat:
```
dispatch create-deployment-pr with image_tags payments-api:2.0.99-test,orders-api:v9.9.9-test
```

### After dispatch succeeds

Go to GitHub and check:
- In `phaniuk111/devops` → new commit on the manifest file + new workflow run.
- In `phaniuk111/deployment-repo` → a new PR should appear updating `configs/prod-images.json`.

---

## Step 4: Simulate the rest of the prod flow

1. Go to the PR in the deployment repo.
2. Review the changes (the config file should have your new image tags).
3. Merge the PR to `main`.

4. The `on-merge-deploy.yml` should automatically run and post comments like:
   - Images deployed
   - Simulated CHG ticket
   - RLFT controls (some closed, some opened)

---

## Step 5: Test re-triggering the deployment workflow from chat

This is the new feature you asked for.

After the PR exists (even before or after merging), from the chat say:

```
retrigger the deployment workflow for PR 42
```

or with external control simulation:

```
retrigger deployment workflow for PR 42 and mark RLFT approval gate as closed
```

The agent will call `retrigger_deployment_workflow`, which runs the deployment workflow again in `DEPLOY_REPO`.

Go back to the PR — you should see a new "**Current State**" comment that reflects:
- The images from the config
- Any controls you marked closed (either manually in comments or via the tool)

---

## Useful chat commands during testing

- `get current manifest`
- `find prs for payments-api`
- `summarize controls on PR 42`
- `retrigger deployment workflow for PR 42`
- `get recent runs`

---

## Troubleshooting

- **No GCP project error** → make sure `gcloud config set project your-gcp-project` and ADC login done.
- **Workflow not creating PR** → ensure `create-deployment-pr.yml` is in the source repo and you dispatched the correct workflow name.
- **No comments on merge** → check that the deployment repo has `on-merge-deploy.yml` and that you actually merged the PR.
- **Budget warning** → the agent has £10 protection. Confirm when it asks or it will stop.

---

## Quick one-command start (after env vars set)

```bash
source .venv/bin/activate
uvicorn src.release_agent.app_fastapi:app --reload --port 8000
```

Then open the browser and start chatting.

Let me know what happens when you try the first message — we can debug live. Good luck! 🚀
