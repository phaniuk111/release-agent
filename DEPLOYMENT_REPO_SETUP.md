# Deployment Repo Setup (Separate from Manifest Repo)

This is critical for realistic simulation as requested.

## What was created

A complete `deployment-repo/` folder with:

- `configs/prod-images.json` — the config file that PRs will update
- `.github/workflows/on-merge-deploy.yml` — triggers on PR merge to main and posts comments with:
  - Image names + tags from the config
  - Simulated CHG ticket
  - Simulated RLFT control gates (closed / opened)
- `examples/create-deployment-pr.yml` — **example workflow to copy to your manifest/source repo**
- `PUSH_INSTRUCTIONS.md` — exact commands to push this as a new GitHub repo

## Step 1: Create the remote repo on GitHub

1. Go to https://github.com/new
2. Repository name: `deployment-repo` (recommended)
3. Description: "Prod deployment configs (simulated)"
4. **Important**: Leave "Add a README file" **unchecked**
5. Click "Create repository"

## Step 2: Push the local files

Follow the instructions in `deployment-repo/PUSH_INSTRUCTIONS.md` or run:

```bash
cd deployment-repo

git init -b main
git add .
git commit -m "Initial deployment simulation repo

- configs/prod-images.json (target for PR updates)
- Workflow that comments on merge with image details + simulated controls"
git remote add origin https://github.com/phaniuk111/deployment-repo.git
git push -u origin main
```

## Step 3: Configure the agent to use separate repos

```bash
export RELEASE_AGENT_TARGET_REPO=phaniuk111/devops          # manifest/source repo
export DEPLOY_REPO=phaniuk111/deployment-repo               # this new separate repo
```

## Step 4: Add the PR creation workflow to your source repo

Copy `deployment-repo/examples/create-deployment-pr.yml` into your source/manifest repo:

```bash
# Example (adjust paths)
cp deployment-repo/examples/create-deployment-pr.yml \
   ../your-manifest-repo/.github/workflows/create-deployment-pr.yml
```

Then commit and push that workflow to `phaniuk111/devops` (or your TARGET_REPO).

This workflow:
- Is triggered by the agent via `workflow_dispatch` with `image_tags`
- Creates a branch + PR in the **deployment repo**
- Updates `configs/prod-images.json` with the new image tags

## Full End-to-End Flow (simulated prod)

1. In the UI/CLI, send: `promote payments-api:2.0.99-test`
2. Confirm the token
3. Agent updates manifest in TARGET_REPO + dispatches workflow
4. The dispatched workflow creates a PR in DEPLOY_REPO (with the config update)
5. You go to the PR in `phaniuk111/deployment-repo`, review, and **merge it**
6. `on-merge-deploy.yml` runs and adds comments like:
   - Image names + tags
   - "CHG ticket simulated"
   - "RLFT approval gate closed"
   - "RLFT deploy control opened"

This closely matches real enterprise release processes where:
- Promotion logic lives in one repo
- Actual prod config lives in another
- Merge to prod triggers notifications / tickets / control updates

## Re-running the deployment workflow + external control changes

**Yes**, the setup now supports re-running the deployment repo workflow.

The `on-merge-deploy.yml` has been enhanced to:

- Support manual re-runs (via GitHub Actions UI "Re-run jobs")
- Support `workflow_dispatch` with inputs (`pr_number` and `simulate_closed_controls`)
- Scan **existing PR comments** to detect controls that were closed "outside" the workflow
- Combine detected state + any manually passed closed controls
- Post an updated "**Current State**" comment showing what is currently closed vs still open

### How to test re-run + external closure

1. Let the copilot create a PR (or merge it).
2. On the PR, manually add a comment like:
   ```
   RLFT approval gate - closed (closed by external team)
   ```
3. Re-run the workflow:
   - Go to the **Actions** tab → find the workflow → click **Re-run jobs**, **or**
   - From terminal:
     ```bash
     gh workflow run on-merge-deploy.yml \
       -f pr_number=123 \
       -f simulate_closed_controls="RLFT approval gate"
     ```

4. It will read the comments, see the closed control, and post a new status comment reflecting the updated state.

This lets you realistically simulate:
- Controls being closed outside the automation
- Re-running deployment notifications later
- The copilot (or you) re-querying the PR and seeing the current control status

See `deployment-repo/PUSH_INSTRUCTIONS.md` for more re-run examples.

## Next Steps

- After pushing, update your agent environment variables.
- Use the testing subagent or manual UI to drive the flow.
- The budget protection and Vertex AI (flash-keel-412418 via ADC) remain active.

Let me know when the repo is pushed and you want help wiring the dispatch workflow or testing the full flow.