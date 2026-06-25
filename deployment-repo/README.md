# Deployment Repo (Simulated Prod Configs)

This repo simulates the **deployment / prod config repository**.

## Purpose
- The release copilot (in the manifest repo) will create PRs here to update prod configuration.
- The PR will contain updates to config files (e.g. `configs/prod-images.json`).
- When the PR is merged to `main`, a workflow runs to simulate production deployment actions:
  - Parse the config for image names and tags.
  - Post comments on the PR (simulating CHG ticket creation, control gates, deployment logs, etc.).

This allows end-to-end testing of the full release flow:
1. Update manifest in source repo
2. Trigger workflow → creates PR in this deployment repo
3. Review & merge PR
4. On merge → workflow posts comments with image details, etc.

## Config Files
- `configs/prod-images.json`: Contains the current desired image:tag for production.
  This is the file that PRs will typically modify.

## Workflow
- `.github/workflows/on-merge-deploy.yml`: Triggers on PR merge to main.
  - Reads the config from the merged commit.
  - Posts comments simulating prod steps (image deployment, CHG references, RLFT-style controls).

## Usage for Testing
Set `DEPLOY_REPO=phaniuk111/deployment-repo` (or your actual repo name) when running the agent.

After the agent dispatches and creates a PR here:
- Go to the PR
- Merge it
- Watch the workflow run and the comments it adds.

**Note:** This is a simulation of real prod config management + deployment notification flows.