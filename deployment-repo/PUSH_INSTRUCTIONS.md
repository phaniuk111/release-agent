# How to Push This Deployment Repo to GitHub

This folder simulates your **separate deployment / prod config repository**.

## Step-by-step to create and push as a new repo

1. Go to GitHub and create a **new empty repository**:
   - Recommended name: `deployment-repo` or `prod-configs`
   - Example: https://github.com/phaniuk111/deployment-repo
   - **Do NOT** initialize with README (we'll push everything).

2. In your terminal, run these commands from inside this folder:

```bash
cd deployment-repo

# Initialize git (if not already)
git init -b main

# Add all files
git add .

# First commit
git commit -m "Initial deployment repo simulation for release copilot testing

- configs/prod-images.json (target config file for PRs)
- .github/workflows/on-merge-deploy.yml (posts comments on merge)
- Simulates CHG tickets + control gates"

# Add your remote (replace with your actual repo)
git remote add origin https://github.com/phaniuk111/deployment-repo.git

# Push
git push -u origin main
```

3. After pushing, go to the repo settings and make sure:
   - Actions are enabled
   - No branch protection that would block the workflow for testing (you can add it later)

## Usage with the Release Copilot

When running the agent, set:

```bash
export RELEASE_AGENT_TARGET_REPO=phaniuk111/devops          # manifest / source repo
export DEPLOY_REPO=phaniuk111/deployment-repo               # this new deployment repo
```

The workflow you trigger in `RELEASE_AGENT_TARGET_REPO` should be updated (if not already) to create a PR against `DEPLOY_REPO` that modifies `configs/prod-images.json`.

## What happens in prod simulation
1. Agent updates manifest + dispatches workflow in source repo.
2. That workflow creates a PR in this deployment repo (updating the config file).
3. You review & merge the PR.
4. `on-merge-deploy.yml` runs and posts comments with image names, simulated CHG, RLFT controls, etc.

This closely mimics a real separation between:
- Release manifest / promotion logic (one repo)
- Actual prod configuration + deployment (deployment repo)

## Re-running workflows and simulating external control changes

The `on-merge-deploy.yml` workflow now supports re-runs:

- Re-run it from the GitHub Actions UI (after the PR is created/merged).
- Or trigger via `workflow_dispatch` passing:
  - `pr_number`
  - `simulate_closed_controls` (e.g. `RLFT approval gate,RLFT deploy control`)

**Example flow for re-run + external closure:**

1. Let the copilot create + merge a PR (or merge it yourself).
2. Manually comment on the PR:
   ```
   RLFT approval gate - closed (done outside the workflow)
   ```
3. Go to Actions → re-run the "On PR Merge - Simulate Prod Deployment & Controls" workflow, or use:

   ```bash
   gh workflow run on-merge-deploy.yml \
     -f pr_number=42 \
     -f simulate_closed_controls="RLFT approval gate"
   ```

4. It will scan existing comments + your input and post an updated "Current State" comment showing which controls are now closed.

This lets you test scenarios where:
- Some controls are closed by external teams/systems
- You re-run the deployment notification
- The status correctly reflects the new state

You can also re-run the workflow multiple times to simulate progressive control closures.
