# LangGraph Release Copilot — Detailed Spec & PoV Plan

**Date**: 2026-06-25  
**Status**: Draft for Implementation  
**Goal**: Build a conversational chatbot (LangGraph agent + gh CLI tools) that Devs use to drive release promotion by providing `image:tag` pairs in chat. The agent safely updates JSON config(s), then dispatches GitHub Actions workflows for prod config changes.  
**Primary Deliverable**: A local-runnable PoV that talks to real https://github.com/phaniuk111 repos and can be containerized for Kubernetes.

---

## 1. Overview

A stateful LangGraph agent exposed via a friendly chat interface.  
Developers chat in natural language:

- "promote payments-api to 2.0.33 and orders-api to v1.2.3"
- "set image payments-api tag to latest-rc"
- "trigger the prod release workflow for the images we just updated"
- "what was the status of the last dispatch?"

The agent:
1. Parses intent and extracts image+tag list.
2. Validates against allowed images (from `image-workflows.json` or a curated allowlist).
3. Reads current state of target JSON config(s).
4. Proposes change, asks for explicit confirmation (HITL).
5. On confirm: updates a source-of-truth JSON file in the GitHub repo (using gh).
6. Dispatches a workflow (e.g. `release-promote.yml` or the existing `image-tag-step-report.yml` for PoV) passing the image:tag payload.
7. Streams status back to the chat.

Runs as a single container (Python + gh CLI).  
PoV uses local `gh auth` (or GH_TOKEN).  
Production deployment: Kubernetes + GitHub App or fine-scoped PAT + checkpointer (Postgres recommended).

---

## 2. Background & Current State

- Existing `devops/` tooling (local + .github/workflows) shows the target pattern:
  - `image-workflows.json` maps images → build workflow
  - `image-tag-step-report.yml` is already a `workflow_dispatch` that accepts `image_tags: "payments-api:vX.Y.Z,..."`
  - Shell script validates build + extracts `RLFT*` release-control steps.
- Current release process is mostly manual or tag-driven. No conversational layer.
- Users want: "just tell the bot the image+tag and it handles updating the right JSONs + kicking off the rest of the pipeline".

The bot becomes the controlled chatops surface for the release portion of the pipeline.

---

## 3. Goals & Non-Goals

### Goals (PoV + v1)
- Conversational multi-turn interface (CLI for PoV + web chat for K8s demo)
- LangGraph agent with custom tools that **only** call `gh` (or GH REST) for a tight allow-list of operations
- Update JSON config file(s) in a GitHub repo with image/tag data passed by user
- Trigger `workflow_dispatch` on a real repo under phaniuk111
- Human-in-the-loop confirmation before any mutation or dispatch
- Fully containerized + K8s manifests
- Local PoV works today with `gh auth login` (no extra infra)
- Clear audit of what was changed + run URL returned to user

### Non-Goals (for initial PoV)
- Full RBAC / per-user permissions beyond token scope
- Automatic rollback
- Updating arbitrary files or arbitrary gh commands (security boundary)
- Multi-repo fan-out in v1
- Slack/Teams integration (future)
- Long-term memory beyond thread checkpointer
- Replacing the existing build/tag validation flows

---

## 4. Proposed Architecture

```
User (dev/ops)
   |
   v
[Chat Interface]  (CLI rich loop / Gradio / Chainlit page)
   |
   v  (messages + thread_id)
[FastAPI or Chainlit/Gradio server]  <-- stream tokens + custom events
   |
   v
LangGraph Agent (StateGraph)
   ├── nodes: classify_intent, extract_params, read_config, propose_update, confirm (interrupt), apply_update, dispatch_workflow, report
   ├── tools: gh_*  (wrapped subprocess or API calls)
   └── checkpointer (Memory for PoV, Postgres in K8s)
   |
   +--> gh CLI (or PyGithub + httpx) --> https://github.com/phaniuk111/<target-repo>
         - contents API for JSON patch
         - workflow run dispatch
         - run status polling
```

**State** (simplified):

```python
class ReleaseAgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    thread_id: str
    release_request: Optional[dict]          # {"images": [{"name": "payments-api", "tag": "2.0.33"}, ...]}
    proposed_changes: Optional[dict]
    confirmation_token: Optional[str]
    last_dispatch: Optional[dict]            # {"run_id": "...", "url": "...", "status": "..."}
    repo: str                                # configurable target "phaniuk111/devops"
```

### Core Tools (gh-based, safe)

All tools live behind strict validation:

1. `list_allowed_images()` → reads `image-workflows.json`
2. `get_current_release_manifest()` → reads a `release-manifest.json` (or creates if missing)
3. `propose_update(images: list[ImageTag])` → builds the diff (no side effect)
4. `apply_json_update(images: list[ImageTag], commit_message: str)` → uses `gh api` to PUT updated file contents (with sha). Only on allow-listed paths.
5. `dispatch_workflow(workflow: str, inputs: dict)` → `gh workflow run ... -f ...` only for approved workflow files.
6. `get_workflow_run(run_id: str)` → status + jobs summary
7. `get_recent_release_runs(limit: int)` 

**Safety wrappers**:
- Every mutating tool first checks an in-memory or env allow-list.
- Arguments are never shell-expanded blindly (use list form + validate).
- Confirmation gate lives in the graph, not the tool.

### Graph Flow (high level)

START → parse_user_message → extract_and_validate → read_current_state → propose → (if needs confirm) **interrupt** → on resume (user said "yes  CONFIRM-123") → apply_update → dispatch → poll_status → respond → END (or more chat)

Use `interrupt()` for clean HITL (see langgraph human-in-the-loop patterns).

---

## 5. JSON Configs & Workflow Integration

### Primary files the agent manages (in target repo)
- `image-workflows.json` (read-only mostly, source of truth for allowed images)
- `release-manifest.json` (new or existing) — example shape:

```json
{
  "last_updated": "2026-06-25T...",
  "requested_by": "chat:phani",
  "images": {
    "payments-api": "2.0.33",
    "orders-api": "v1.2.3"
  },
  "promote_to": "prod",
  "status": "pending-dispatch"
}
```

### Triggered workflow
For PoV we dispatch the existing:
`image-tag-step-report.yml` with `image_tags` input.

For real release, user/repo will have (or we supply) a `release-promote.yml` or `update-prod-configs.yml` that:
- Checks out
- Reads the manifest JSON (or receives inputs)
- Mutates Helm values, Argo ApplicationSets, Terraform vars, or other JSON/YAML prod configs
- Opens PR or pushes + tags
- Runs the RLFT* gates etc.

The agent itself does **not** contain the prod mutation logic — it is the chat front-end + controlled dispatcher.

---

## 6. Chat Interface Options (PoV → K8s)

**PoV (fastest)**: Rich terminal CLI that:
- Uses `graph.stream(..., stream_mode="values" | "messages")`
- Shows tool calls nicely
- Handles `interrupt` by prompting "Type: CONFIRM-xxx or 'yes' to proceed"

**Demo / K8s**:
- Option A (recommended for speed): Gradio `ChatInterface` + custom `.submit` that invokes the graph.
- Option B: Chainlit (beautiful, threads, LangGraph native integration via `cl.LangchainCallbackHandler` or direct).
- Option C: Plain FastAPI + static HTML + EventSource (more work).

Start with CLI + a Gradio single-file app (`app.py` with `demo.launch(server_name="0.0.0.0")`).

---

## 7. Deployment

### Dockerfile (multi-stage-ish)
- python:3.11-slim
- Install gh (official script or apt source)
- COPY src + requirements
- Non-root user
- ENTRYPOINT for the chat app or chainlit

### K8s
- Deployment (1 replica for stateful chat threads; later horizontal with shared Postgres)
- Service
- Secret: GH_TOKEN or mounted GitHub App key
- LLM provider secret
- ConfigMap for target repo, allowed workflows, log level
- (Optional) Postgres for checkpointer + init
- Liveness/readiness on /health if web
- Resource requests: 512Mi / 500m reasonable for agent

Persistence note: In-memory checkpointer for PoV is fine. For multi-replica or restart-safe use `langgraph-checkpoint-postgres` or Redis.

---

## 8. Security & Guardrails (Critical)

- Only allow-listed gh subcommands + flags (`workflow run`, `api /repos/.../contents/...` with specific paths, `run view`, `run list`).
- No `gh auth login` from inside agent.
- Token injected at runtime with minimal scopes: `repo`, `workflow` (or better: fine-grained GitHub App with only Contents:write + Actions:write on specific repos).
- Confirmation string required before dispatch (e.g. must contain a random token shown by agent).
- All actions logged with thread_id + user intent + final GH URLs.
- Rate limiting / circuit breaker around GitHub calls.
- Never commit secrets; .env only locally.

---

## 9. Local PoV Instructions (what we will deliver)

```bash
# 1. In this repo
cd release-agent

# 2. Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# or uv pip install ...

# 3. Auth (one time)
gh auth login   # or export GH_TOKEN=...

# 4. LLM key (OpenAI example; Gemini also fine)
export OPENAI_API_KEY=...

# 5. Run CLI chat
python -m release_agent.cli --repo phaniuk111/devops

# Example session
> promote payments-api:2.0.33 for prod
Agent: I parsed payments-api → 2.0.33. Current manifest has ... Proposed update ...
Agent: Reply with "CONFIRM-8f3a" to proceed with file update + dispatch.
> CONFIRM-8f3a
Agent: Updated release-manifest.json (commit abc123). Dispatched workflow run #12345
Agent: https://github.com/phaniuk111/devops/actions/runs/...
```

Also a Gradio web UI on http://localhost:7860.

---

## 10. Key Decisions

1. **LangGraph over plain ReAct or ADK**: Needed for reliable HITL interrupts, custom routing between "parse → propose → confirm → mutate", and future branching (multi-image, conditional promote vs report-only).
2. **gh CLI inside tools (not only SDKs)**: Matches user request "with gh cli tools". Easy to mirror what humans type. Still strictly validated.
3. **Update a dedicated manifest JSON rather than directly editing prod manifests**: Separation of concerns. The dispatched workflow owns the "other config files" mutation. Chatbot owns the intent capture.
4. **Confirmation via interrupt() + token**: Clean, first-class in LangGraph. Avoids accidental prod triggers.
5. **Start with CLI + Gradio**: Fastest path to working PoV and nice demo. Chainlit can be added in a follow-up PR.
6. **Target repo = phaniuk111/devops for PoV**: Already has a dispatchable workflow + the image mapping. Real prod repo can be swapped via config/env later.
7. **Postgres checkpointer for K8s prod path**: Standard, supports thread history, time travel for debugging releases.

---

## 11. PR / Implementation Plan

**PR 1: Project skeleton + spec**  
- release-agent/ layout, README, spec.md, .env.example, pyproject/requirements, basic Dockerfile + k8s yaml stubs.

**PR 2: Safe gh tools layer**  
- `src/release_agent/tools/gh_tools.py` with 5-7 @tool functions + unit tests (mocked).
- Strict command builder + validator.
- Helper to call gh and parse JSON output.

**PR 3: LangGraph agent core**  
- State definition, nodes (intent, propose, apply, dispatch, status), graph wiring with conditional + interrupt for confirm.
- Use `create_react_agent` as fallback or pure StateGraph.
- Configurable target repo + allowed images.

**PR 4: CLI chat interface (PoV)**  
- `cli.py` rich console loop, streaming, interrupt handling, pretty tool output.

**PR 5: Gradio web chat (optional but recommended for "chatbot")**  
- `app_gradio.py` with `gr.ChatInterface`, background graph execution, session threads via simple dict or sqlite.

**PR 6: Sample data + workflow enhancement**  
- Add `release-manifest.json` example to repo (or the agent can create on first use).
- Optional: add a simple `release-promote.yml` stub workflow in `.github/workflows` that accepts the tags and prints a plan (so dispatch is meaningful).

**PR 7: Container + K8s**  
- Polish Dockerfile (gh install, nonroot, health).
- k8s/deployment.yaml, service, config, secret.example.
- docker-compose for local + postgres checkpointer demo.

**PR 8: Docs + end-to-end test script**  
- README with screenshots / transcript.
- `scripts/e2e_pov.sh` that runs a canned conversation and asserts a workflow was dispatched (using gh run list).
- Observability notes (LangSmith project, GitHub run URLs in responses).

**Order note**: PR1-4 give a working local CLI PoV. Later PRs improve UX and deployability.

---

## 12. Open Questions (for user)

1. Preferred LLM for the PoV? (OpenAI gpt-4o-mini cheap/fast vs Gemini 1.5 Flash via Vertex vs local Ollama llama3)
2. Exact name of the JSON file(s) to update in chat ("release-manifest.json", "images-to-promote.json", or reuse/extend something existing)?
3. Preferred real target repo for dispatch in your account besides devops (if any)?
4. Should the agent create a PR for the JSON change or commit directly to a `release/` branch or main?
5. Do you want Chainlit instead of (or in addition to) Gradio for the web chat?

---

## 13. References

- Existing: `devops/gh-image-tag-steps.sh`, `devops/image-workflows.json`, `devops/.github/workflows/image-tag-step-report.yml`
- LangGraph docs (HITL, streaming, checkpointers)
- `gh workflow run --help` and `gh api repos/:owner/:repo/contents/:path`
- User's other agents (ADK + MCP patterns) for style inspiration

---

**Next**: After review/consensus, execute the plan starting with PR 1-4 to get a live local PoV that can update JSON + dispatch real actions against phaniuk111.

End of spec.