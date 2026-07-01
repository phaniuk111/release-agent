# Technical Design Specification: ADK Release Copilot

## 1. Purpose

Release Copilot is a Kubernetes-deployable ADK application that lets developers
operate GitHub-based release workflows through chat. The language model handles
operator phrasing, tool selection, and summarization. Deterministic Python code
handles release-changing actions.

## 2. Components

```text
Browser / CLI
  -> release_agent.app_fastapi or release_agent.cli
  -> release_agent.adk_service.AdkChatService
  -> adk_release_agent.agent.root_agent
  -> ADK specialist agents + ADK skills
  -> adk_release_agent.tools wrappers
  -> src/release_agent/tools PyGithub facade
  -> GitHub repos, PRs, workflow runs, deployment JSON
```

## 3. ADK Runtime

`adk_release_agent/agent.py` builds the ADK root agent and five specialist
sub-agents:

- status: read-only release state, allowed images, recent runs, workflow status.
- PR: find/read PRs and summarize comments/control metadata.
- controls: verify image tags and build controls.
- ops: remove/unstage, retrigger workflow, release staged PRD batch.
- deploy: prepare exact deploy previews and apply only confirmed previews.

Filesystem skills in `adk_release_agent/skills/` document each specialist's
responsibilities and safety limits. The root agent loads those skills through ADK
`SkillToolset`.

## 4. Deterministic Deploy Lane

The deploy lane is regular Python, not prompt logic:

1. Parse message, image tags, or deployment JSON payload.
2. Build an exact deployment plan with `plan_deploy`.
3. Mint a `CONFIRM-*` token and store the pending preview with a TTL.
4. Stream the preview and confirmation interrupt to the UI.
5. Apply only when the user sends the exact token.
6. Invoke `open_release_pr` through the existing GitHub tool facade.

The ADK deploy agent also wraps the confirmed apply function with ADK native tool
confirmation, but the FastAPI adapter handles the primary token-gated path so the
UI remains deterministic.

## 5. Tool Exposure

The PyGithub tool layer remains the source of truth for GitHub behavior. ADK
receives plain Python wrappers with typed signatures and dictionary results.

Free-form ADK chat may use read tools and scoped ops tools. It must not directly
receive release-defining mutation tools:

- `apply_json_update`
- `dispatch_workflow`
- `open_release_pr`

Those remain reachable only through the deterministic deploy facade or direct
operator tool testing.

## 6. State Model

- Conversation state: ADK in-memory services for PoV.
- Durable release state: GitHub deployment JSON, branches, PRs, and workflow runs.
- Pending deploy previews: process-local token map with TTL.

For production multi-replica deployment, add shared ADK session/artifact storage
and a shared pending-preview store before scaling horizontally.

## 7. Configuration

Configuration comes from `.env`, environment variables, or Helm ConfigMap/Secret
values:

- GitHub auth: `GH_TOKEN` or `GITHUB_TOKEN`, with local fallback to `gh auth token`.
- Vertex AI: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GEMINI_MODEL`.
- Repos: `BUILD_REPO`, `DEPLOY_REPO`.
- Branch/path/workflow settings: SIT/UAT/PRD branch names, deployment path pattern,
  allowed workflows, cutoff hour, namespaces, chart directory, values file pattern.

## 8. Interfaces

- FastAPI serves static UI and `/api/chat` SSE.
- CLI streams ADK chat turns for local operation.
- `tools_cli.py` exercises the GitHub tool layer directly, with `--dry-run` for
  mutating tools.
- `adk run adk_release_agent` supports ADK-native local execution.

## 9. Safety

- Whitelisted tools only.
- No arbitrary shell execution in the GitHub tool layer.
- Exact preview before mutation.
- Exact same-thread confirmation token before deploy apply.
- PRD deploy staging uses the configured protected-branch PR flow.
- PRD release is a separate scoped operation governed by cutoff/config controls.

## 10. Validation Gates

Before considering the refactor complete:

- Full pytest suite passes.
- Python compile check passes for `src`, `adk_release_agent`, and tests.
- Helm chart lints and renders.
- Dependency lock/export does not include the retired agent framework stack.
- Runtime source/tests/current docs do not reference the retired framework stack.
- ADK root agent imports and exposes the expected specialists.
- Deploy preview/apply tests prove the mutation path is token-gated.
