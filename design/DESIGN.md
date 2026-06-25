# Technical Design Specification: LangGraph-Powered Release Chatbot Agent

**Date:** 2026-06-25  
**Author:** Grok (Systems Architect)  
**Status:** Design for immediate PoV implementation  
**Target GitHub Org:** https://github.com/phaniuk111 (PoV repo: phaniuk111/gh-image-tag-report-test)  
**Scope:** Chatbot application (conversational UX + LangGraph agent orchestration, tools, deployment). GitHub workflows are leveraged/extended only as needed for validation.

---

## 1. Executive Summary

This design specifies a production-minded, Kubernetes-deployable release promotion chatbot powered by LangGraph. Developers interact conversationally ("deploy payments-api with tag 2.0.33 to prod", "status of last release", "update orders-api to v1.2.3 and trigger promote") to:

- Provide image names + tags.
- Safely update JSON configuration files (e.g., `prod-image-versions.json` or equivalent release manifest) in a target GitHub repository using whitelisted operations.
- Trigger `workflow_dispatch` GitHub Actions (initially the existing `image-tag-step-report.yml` for PoV validation; extensible to future "promote-to-prod" workflows that consume the JSON to update additional prod configs/PRs).

**Core principles:**
- Stateful multi-turn conversations via LangGraph checkpointer.
- Human-in-the-loop (HITL) confirmation **interrupts** before any mutating actions (file updates, workflow dispatch).
- Strict whitelist of operations/images. No arbitrary `gh` commands.
- Audit logging of every intent, proposal, confirmation, and GitHub side-effect.
- Runnable locally (using host `gh auth`) **and** in Kubernetes.
- Configurable LLMs (default OpenAI; support for Gemini via Vertex, local Ollama).
- Web-based UI (Chainlit recommended for rapid agent UX with tool step visibility + native interrupts).

The devops/ assets (image-workflows.json + gh-image-tag-steps.sh + image-tag-step-report.yml workflow) are directly leveraged for validation and as the canonical source of allowed images.

**PoV success criteria:** A developer can run locally, say "update payments-api to v1.2.3 and trigger report", receive parsed proposal, explicitly confirm, see the JSON file updated in https://github.com/phaniuk111/gh-image-tag-report-test (or configured repo), and see a real `workflow_dispatch` run triggered + status reported.

---

## 2. Architecture

### 2.1 High-Level Diagram (Mermaid)

```mermaid
flowchart TB
    subgraph Dev["Developer (local or browser)"]
        U[Chat: "update payments-api v2.0.33<br/>and promote to prod"]
    end

    subgraph UI["Web Chat UI (Chainlit recommended)"]
        C[Chainlit Server<br/>+ streaming + steps + AskUserMessage for confirm]
    end

    subgraph Backend["FastAPI/Chainlit App (Python)"]
        direction TB
        S[Session/Thread Manager<br/>thread_id per conversation]
        A[LangGraph Agent<br/>StateGraph + checkpointer]
    end

    subgraph Agent["LangGraph Graph (compiled)"]
        direction LR
        P[parse_intent<br/>(structured Pydantic output)]
        T[Tools Layer<br/>whitelisted gh wrappers]
        U1[propose_updates]
        I1[interrupt: confirm file update]
        M1[mutate: update_json_config]
        I2[interrupt: confirm dispatch]
        M2[mutate: dispatch_workflow]
        R[report_status + poll]
    end

    subgraph GH["GitHub (phaniuk111/*)"]
        direction TB
        API[GitHub REST API via gh CLI<br/>(GH_TOKEN)]
        JSON[(JSON configs<br/>e.g. prod-image-versions.json<br/>image-workflows.json)]
        WF[workflow_dispatch<br/>image-tag-step-report.yml<br/>(+ future promote workflows)]
        RUNS[Actions Runs + Logs]
    end

    subgraph Persist["Persistence"]
        CP[(Checkpointer:<br/>PoV=SqliteSaver<br/>Prod=PostgresSaver)]
        AUDIT[(Audit Log<br/>structured + append-only)]
    end

    subgraph K8s["Kubernetes (Deployment/Service)"]
        POD[Pod: python + gh CLI installed<br/>Secrets: GH_TOKEN, LLM keys<br/>ConfigMap: TARGET_REPO, ALLOWED_IMAGES etc.]
    end

    U --> C
    C --> S
    S --> A
    A --> P
    P --> T
    T <--> GH
    A --> U1 --> I1 --> M1 --> I2 --> M2 --> R
    M1 --> JSON
    M2 --> WF
    WF --> RUNS
    A <--> CP
    A --> AUDIT
    POD -.deploy.-> K8s
```

**Data flow summary:**
1. User message → Chainlit/FastAPI → LangGraph `invoke`/`astream_events` with `{"configurable": {"thread_id": "..."}}`.
2. Router / ReAct or custom nodes parse intent → propose structured updates.
3. Before `update_json_config` or `dispatch_workflow`: `interrupt()` yields control to UI for explicit confirmation (e.g., user types "CONFIRM UPDATE payments-api:v1.2.3" or clicks).
4. On resume (via `Command(resume=...)` or next invoke), execute safe gh command.
5. Results + workflow status streamed back; state persisted.

### 2.2 Component Layers

- **Presentation:** Chainlit (preferred for PoV: zero-boilerplate agent chat, renders LangGraph steps/tools, human confirm). Fallback: FastAPI + SSE/WS + minimal static UI (or Gradio).
- **Orchestration:** LangGraph (StateGraph for explicit control + routing; `create_react_agent` for fallback tool-calling).
- **Tools:** Thin Python wrappers around `gh` CLI (or PyGithub) + validation. All mutating tools return rich result dicts including before/after and GitHub URLs.
- **GitHub Bridge:** Subprocess calls to `gh` (respects `GH_TOKEN` env; works identically locally and in container). Whitelisted commands only.
- **Config Source of Truth:** `image-workflows.json` (read-only for allowlist). New/updated: `prod-image-versions.json` (or `releases/current-manifest.json`).
- **Persistence:** LangGraph checkpointer + separate audit sink.

---

## 3. State Schema

Use a Pydantic `BaseModel` + `langgraph.graph` for strong typing (recommended over raw TypedDict for validation).

```python
from typing import Literal, Annotated, List, Optional, Dict, Any
from pydantic import BaseModel, Field
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage

class ImageUpdate(BaseModel):
    image: str = Field(..., description="Image name, e.g. payments-api")
    tag: str = Field(..., description="Tag e.g. v1.2.3 or 2.0.33")
    source: str = "chat"

class ReleaseIntent(BaseModel):
    action: Literal["update_images", "query_status", "list_images", "help"] = "update_images"
    updates: List[ImageUpdate] = Field(default_factory=list)
    workflow: Optional[str] = "image-tag-step-report.yml"
    notes: Optional[str] = None

class ReleaseAgentState(BaseModel):
    messages: Annotated[List[BaseMessage], add_messages] = Field(default_factory=list)
    current_intent: Optional[ReleaseIntent] = None
    proposed_updates: List[ImageUpdate] = Field(default_factory=list)
    confirmed_updates: List[ImageUpdate] = Field(default_factory=list)
    pending_confirmation: Optional[Literal["update_config", "dispatch", "none"]] = "none"
    last_dispatch: Optional[Dict[str, Any]] = None  # {workflow, run_id, url, status}
    audit_entries: List[Dict[str, Any]] = Field(default_factory=list)
    allowed_images: List[str] = Field(default_factory=list)
    target_repo: str = "phaniuk111/gh-image-tag-report-test"
    error: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True
```

**Thread scoping:** Every chat session uses a stable `thread_id` (user-provided or UUID generated per browser session / user). Passed in `config={"configurable": {"thread_id": thread_id}}`.

**Checkpointing:** Full graph state (including messages history + proposed/confirmed) is saved after each node.

---

## 4. Graph Flow (Nodes / Edges)

Use `StateGraph` (custom routing + explicit HITL) rather than pure ReAct for safety and control. `create_react_agent` can be used as a sub-graph for free-form Q&A.

```mermaid
flowchart TD
    START([__start__]) --> ROUTE{route_intent}
    ROUTE -->|structured release intent| GATHER[gather_context<br/>read allowed images + current manifest]
    ROUTE -->|query| ANSWER[answer_query<br/>use tools + LLM]
    GATHER --> PROPOSE[propose_updates<br/>LLM + Pydantic output]
    PROPOSE --> VALIDATE{validate images in allowlist?}
    VALIDATE -->|yes| INTERRUPT_UPDATE[interrupt<br/>'confirm file update']
    VALIDATE -->|no| CLARIFY[clarify / error]
    INTERRUPT_UPDATE -->|user confirms| MUTATE_JSON[update_json_config<br/>gh api PUT contents]
    MUTATE_JSON --> INTERRUPT_DISPATCH[interrupt<br/>'confirm workflow_dispatch']
    INTERRUPT_DISPATCH -->|confirmed| DISPATCH[dispatch_workflow<br/>gh api POST /dispatches]
    DISPATCH --> POLL[poll_workflow_status<br/>or return run info]
    POLL --> ANSWER
    MUTATE_JSON --> ANSWER
    ANSWER --> END([__end__])
    CLARIFY --> END
```

**Key nodes (high-level):**
- `route_intent`: LLM with structured output (Pydantic `ReleaseIntent`) or tool-calling router.
- `gather_context`: Call read tools in parallel.
- `propose_updates`: Present diff-like proposal; populate `proposed_updates`.
- `update_json_config` (mutating): Only reachable after interrupt resume.
- `dispatch_workflow` (mutating).
- Tools are available at any ReAct-style step when using hybrid approach.

**HITL implementation (from current LangGraph docs):**
```python
from langgraph.types import interrupt, Command

# Inside node before mutation
if state.pending_confirmation == "update_config":
    request = {
        "action_request": {"action": "update_release_config", "args": {"updates": [...]}},
        "config": {"allow_respond": True, "allow_ignore": False},
        "description": f"About to commit {updates} to {repo} prod-image-versions.json"
    }
    response = interrupt([request])[0]
    if response["type"] != "response" or "CONFIRM" not in response.get("args", {}).get("text", "").upper():
        # reject path
        ...
    # else fallthrough to mutate
```

Resume: `graph.invoke(Command(resume=response), config)` or next user message triggers continuation for the same thread_id.

---

## 5. Detailed Tool Definitions

All tools are implemented as `@tool` decorated functions (or `StructuredTool`). They are **never** exposed for arbitrary execution.

Common safety in tool layer:
- Always load fresh allowlist from `image-workflows.json` in target repo.
- Validate `image` against allowlist before any change.
- Tag format: strict regex `^[vV]?[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$`.
- All gh calls use timeout (30-120s), capture stdout/stderr, never shell=True with user input.
- Every tool call appends structured entry to `state.audit_entries` (and side-effect log).
- Dry-run mode flag for testing.

### Read-only Tools

1. `list_allowed_images() -> List[str]`
   - Reads `.images` keys from `image-workflows.json` via `gh api .../contents/...`.
   - Used for validation + "what images are ready?"

2. `read_repo_json(path: str = "image-workflows.json") -> Dict`
   - Returns parsed JSON + sha (for subsequent updates) + GitHub URL.

3. `list_recent_release_runs(limit: int = 5) -> List[Dict]`
   - `gh run list --workflow image-tag-step-report.yml ... --json ...`

4. `get_workflow_run_status(run_id: str) -> Dict`
   - `gh run view $run_id --json ...` + summary of RLFT* steps where applicable.

### Mutating Tools (require prior interrupt/confirmation)

5. `update_image_tag_in_config(image: str, tag: str, config_path: str = "prod-image-versions.json") -> Dict`
   - **What it does:** Fetches current file (if exists), merges `{"images": {image: tag}}` (or top-level map), base64-encodes, calls `gh api --method PUT /repos/{owner}/{repo}/contents/{config_path}` with proper `message`, `sha`, `branch=main`.
   - Returns: `{ "success": bool, "commit_sha": , "html_url": , "before": , "after": , "path": }`
   - Safety: Image must be in allowlist; path restricted to approved config files list; commit message includes "release-chatbot: ..."; never overwrites unrelated content.
   - Audit: full before/after + actor context.

6. `dispatch_release_workflow(image_tags: str, workflow_file: str = "image-tag-step-report.yml", inputs: Optional[Dict]=None) -> Dict`
   - **What it does:** `gh api --method POST /repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches -f "ref=main" -f "inputs[image_tags]={image_tags}" ...`
   - Returns immediately with `{ "workflow_url": , "run_id": (polled or via list), "status": "queued" }`.
   - For PoV: uses the existing report workflow (validates tags via the marker logic).
   - Future: same mechanism for a `promote-to-prod.yml` that reads the just-updated JSON and opens PRs against prod manifests.

7. `confirm_and_dispatch(...)` (optional wrapper that also performs the interrupt check inside).

**Implementation note (gh wrapper):**
```python
import subprocess, os, json, base64

def _run_gh(args: list[str], input: Optional[str] = None) -> Dict:
    env = {**os.environ, "GH_TOKEN": os.environ.get("GH_TOKEN") or subprocess.getoutput("gh auth token").strip()}
    try:
        out = subprocess.check_output(["gh", "api", *args], env=env, input=input, stderr=subprocess.STDOUT, timeout=60)
        return {"success": True, "output": json.loads(out) if out else {}}
    except Exception as e:
        return {"success": False, "error": str(e)}
```

Never pass raw user strings into command construction except sanitized `image_tags`.

---

## 6. JSON Update + Workflow Dispatch End-to-End Sequence

1. User: "update payments-api with tag 2.0.33 and orders-api to v1.2.3, then trigger"
2. Agent parses → proposes. Shows current vs proposed.
3. HITL: "Do you want me to update prod-image-versions.json in phaniuk111/gh-image-tag-report-test? Reply CONFIRM UPDATE"
4. User confirms → tool `update_image_tag_in_config` (twice or batched) → commits (new tree or direct contents PUT).
5. Agent shows commit link.
6. HITL2: "Ready to dispatch image-tag-step-report.yml with 'payments-api:2.0.33,orders-api:v1.2.3'? Reply CONFIRM RELEASE"
7. Dispatch POST → GitHub queues run. Agent reports run URL immediately.
8. Optional background poll node or user follow-up "status?" uses `get_workflow_run_status`.
9. Workflow runs the shell script, emits step summary with RLFT* table + validation markers.
10. Agent surfaces summary back in chat.

The updated JSON becomes the hand-off to any future workflow that performs broader prod config mutations (e.g., Helm values, Argo Application, Terraform vars, etc.).

---

## 7. Persistence and Session Management (K8s)

- **LangGraph Checkpointer:**
  - PoV/local: `from langgraph.checkpoint.sqlite import SqliteSaver`; `SqliteSaver.from_conn_string("release_agent.db")` or `:memory:`.
  - Production: `langgraph-checkpoint-postgres` + `PostgresSaver.from_conn_string(...)`; call `.setup()` on startup. Use async variant `AsyncPostgresSaver` if using async graph execution.
  - Thread ID = stable conversation/session key (store in Chainlit user session or cookie; allow resume via "continue thread <id>").
  - Config example: `DB_URI = os.getenv("POSTGRES_CHECKPOINTER_URI", "postgresql://...")`

- **Chat sessions outside graph:** Chainlit or FastAPI in-memory dict (or Redis) for UI-level session metadata. Graph state is the source of truth.
- **K8s considerations:**
  - For sqlite: Use `StatefulSet` + PersistentVolumeClaim (RWX or RWO per replica; single replica recommended).
  - Strongly prefer external Postgres (Cloud SQL, RDS, or in-cluster) for HA and multi-replica agents.
  - Checkpointer connection pooled; handle reconnects.
- **Long-term history:** Optionally export to BigQuery or S3 via sidecar; graph state itself is durable in the checkpointer.

---

## 8. Authentication Model

- **GitHub:**
  - Recommended for prod: **GitHub App** (fine-grained: `Contents: Read & write`, `Actions: Read & write`, `Workflows: Read & write`, repo scoped). Install on the target org/repo. Use JWT + installation token (via `PyGithub` or `gh`).
  - PoV & simple: **PAT** (classic with `repo` scope or fine-grained). Store as `GH_TOKEN`.
  - Local: `gh auth login` (stores in OS keychain; `gh auth token` exports it).
  - In container/K8s: `GH_TOKEN` env var (gh CLI and direct API calls honor it). Never bake into image.

- **LLM keys:** `OPENAI_API_KEY` (or `GOOGLE_APPLICATION_CREDENTIALS` for Vertex, `OLLAMA_HOST`).
- **Chat UI auth (K8s):** Start simple (none or static token for PoV). Later: OIDC (GitHub OAuth), or internal SSO. Pass user identity into graph metadata for audit.
- **No GitHub session in the bot itself beyond token** — the bot acts as a service account.

**Scopes minimal principle:** The token only needs access to the specific release-manifest repo(s). Separate tokens per environment if multiple.

---

## 9. Container & Kubernetes Design

### Dockerfile (multi-stage, slim, secure)

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS base
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git jq \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI (official)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev

COPY src/ ./src/
COPY chainlit.md app.py ./
ENV PYTHONPATH=/app/src

# Non-root
RUN useradd -m -u 10001 chatbot && chown -R chatbot:chatbot /app
USER chatbot

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"
CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8000"]
```

(Use `uv` or `pip` + requirements; pin everything.)

### Kubernetes Resources (plain manifests or Helm)

- **Deployment** (or StatefulSet if sqlite PV):
  - Replicas: 1–3 (sticky sessions via cookie or external LB affinity for threads).
  - ImagePullPolicy: Always in non-prod.
  - EnvFrom: ConfigMap + Secret.
  - SecurityContext: runAsNonRoot, readOnlyRootFilesystem (tmp volume for sqlite if needed), drop caps.
  - Resources:
    ```yaml
    requests: {cpu: "250m", memory: "512Mi"}
    limits:   {cpu: "1000m", memory: "1Gi"}
    ```
  - Probes: liveness `/health`, readiness `/ready`, startup (60s period).
  - Volume: optional emptyDir or PVC for local sqlite/checkpoint.

- **Service**: ClusterIP (or NodePort for PoV), port 8000.
- **Secret** (example):
  ```yaml
  apiVersion: v1
  kind: Secret
  metadata: {name: release-chatbot-secrets}
  type: Opaque
  stringData:
    GH_TOKEN: "..."
    OPENAI_API_KEY: "..."
    POSTGRES_CHECKPOINTER_URI: "postgresql://user:pass@svc:5432/langgraph?sslmode=require"
  ```
- **ConfigMap**:
  - `TARGET_GITHUB_REPO`, `DEFAULT_WORKFLOW`, `LOG_LEVEL`, `LLM_MODEL`, `CONFIRM_PHRASE=CONFIRM RELEASE`.
- **RBAC**: Minimal (none needed for the bot itself; the GH token carries permissions).
- **ExternalSecrets** pattern (recommended in prod, modeled after qTest.Charts): Use `external-secrets.io` + GCP/AWS Secret Manager or Vault for GH token and LLM keys.
- **Ingress**: Optional (TLS via cert-manager). Path `/` → service. Add auth annotations later.
- **HPA**: CPU-based for chat load.
- **NetworkPolicy**: Restrict egress to only GitHub API IPs + LLM provider + Postgres (if in-cluster).

**Helm values** skeleton modeled on promptfoo/Openfeature-example patterns:
```yaml
replicaCount: 1
image:
  repository: ghcr.io/phaniuk111/release-chatbot
  tag: latest
env:
  TARGET_REPO: phaniuk111/gh-image-tag-report-test
resources: { ... }
persistence:
  enabled: false   # prefer Postgres
secrets:
  existingSecret: release-chatbot-secrets
```

**Probes implementation:** Simple FastAPI/Chainlit endpoints returning 200 when graph compiles and gh token present (light check).

---

## 10. Local PoV Run Instructions + Example Conversation

### Prerequisites
- Python 3.12+
- `gh` CLI installed and `gh auth login --hostname github.com`
- OpenAI key (or set `OLLAMA_HOST` etc.)
- Clone or have write access to target repo for PoV.

### Run
```bash
# 1. Clone this agent code (once implemented)
cd release-chatbot
python -m venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"   # or pip install -r requirements.txt

# 2. Local
export GH_TOKEN=$(gh auth token)
export OPENAI_API_KEY=sk-...
export TARGET_GITHUB_REPO=phaniuk111/gh-image-tag-report-test
export LLM_MODEL=gpt-4o-mini

# Chainlit
chainlit run src/release_chatbot/main.py -w --port 8000

# Or pure python entry
python -m release_chatbot.server
```

### Docker PoV
```bash
docker build -t release-chatbot:local .
docker run --rm -p 8000:8000 \
  -e GH_TOKEN=$(gh auth token) \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  -e TARGET_GITHUB_REPO=phaniuk111/gh-image-tag-report-test \
  release-chatbot:local
```

### Example Conversation (multi-turn, with progress)

```
User: what images can I release?
Agent: Allowed images from image-workflows.json: payments-api, orders-api.
        Current prod-image-versions.json: {"payments-api": "v1.2.3", "orders-api": "v2.4.0"}

User: update payments-api with tag 2.0.33 and trigger the report workflow
Agent: [tool: list_allowed_images] [tool: read_repo_json]
        Proposed:
        - payments-api: v1.2.3 → 2.0.33
        This will update prod-image-versions.json and then dispatch image-tag-step-report.yml.
        Please confirm with exact phrase: CONFIRM UPDATE payments-api:2.0.33

User: CONFIRM UPDATE payments-api:2.0.33
Agent: [interrupt resume] [tool: update_image_tag_in_config]
        ✅ Committed sha abc123 to phaniuk111/gh-image-tag-report-test:prod-image-versions.json
        https://github.com/.../commit/abc123

        Now dispatch? Reply: CONFIRM RELEASE payments-api:2.0.33

User: CONFIRM RELEASE payments-api:2.0.33
Agent: [tool: dispatch...] 
        🚀 Dispatched workflow image-tag-step-report.yml
        Run: https://github.com/phaniuk111/gh-image-tag-report-test/actions/runs/123456
        Status: queued

User: status?
Agent: [tool: get_workflow_run_status]
        Run #123456: completed / success
        | payments-api | 2.0.33 | success |
        RLFT approval gate: success
        See full report in step summary.
```

All tool executions visible as "steps" in Chainlit UI.

---

## 11. Safety, Governance & Observability

### Safety & Governance
- **Whitelist only:** Images from live `image-workflows.json`; operations = fixed set of 6-7 tools. No `gh workflow run`, `gh repo edit`, shell exec, etc.
- **Confirmation phrase:** Hard-coded exact match (case-insensitive prefix ok) + UI button in Chainlit.
- **Audit log:** Every transition + tool input/output + confirmation text + GH response + actor (when available) written via structlog to stdout (JSON) + optional persistent sink (file, Postgres table, or dedicated GH repo audit log).
- **Rate limiting / concurrency:** Per-thread simple in-memory; global via Redis later. Max 1 active dispatch per image in flight.
- **Idempotency:** Dispatch uses explicit inputs; workflow itself should be safe to re-run.
- **Rollback:** Agent can read previous version (via git history) and propose revert.
- **Never in prod without:** Signed commits? Branch protection on target JSON paths; required PR reviews on the manifest repo itself (chatbot pushes directly to main for speed; consider PR creation as alternative tool).
- **Least privilege token** at all times.

### Observability
- **LangSmith** (or Langfuse): Set `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`. Full graph traces, token usage, node timings.
- **Structured logging:** structlog + JSON. Include `thread_id`, `run_id`, `tool`, `image:tag`, `outcome`.
- **Metrics:** Expose `/metrics` (Prometheus) with counters for `release_intents_total`, `updates_applied_total`, `dispatches_total`, `confirmations_rejected_total`, latency histograms.
- **Graph visualization:** On `/graph` or debug endpoint render `graph.get_graph().draw_mermaid()`.
- **K8s:** Standard container logs + pod metrics. Add Datadog / CloudWatch agent if in EKS (modeled on aws-eks-prod).
- **Workflow linkage:** Every dispatch response includes run URL; poll results contain conclusion + RLFT step table when relevant.

---

## 12. Alternatives Considered

| Alternative | Pros | Cons | Why Not Primary |
|-------------|------|------|-----------------|
| Pure `gh` shell + fzf / simple script | Zero LLM cost, fast, auditable | No conversational memory, poor UX for multi-turn + status, no natural language | Loses the "chatbot front door" value |
| Slack bot (Bolt + LangGraph) | Familiar for devs, rich blocks for confirm | Requires Slack app, auth mapping, harder K8s embed than pure web | Good future extension; web first for PoV |
| GitHub App + /release slash command in issues/comments | Native GitHub, no extra UI | Limited multi-turn state, poor tool visibility | Complementary (can trigger same agent) |
| Argo Workflows + chatops or Argo CD notifications | Already GitOps native | Steeper, more infra, not conversational | Can be the *backend* workflow triggered by this agent |
| Full custom Slack + Argo + human approval gates in GitHub | Mature | High complexity for simple image:tag updates | Overkill for this front door |
| Direct Terraform / Helm PR bot | Declarative | Slower feedback than chat + dispatch | Good for the "updates other config" part of the downstream workflow |

**Recommendation:** This LangGraph chatbot as thin, auditable, conversational orchestration layer on top of existing GH workflow assets. Downstream workflows own the prod mutation complexity.

---

## 13. Implementation Considerations & Risks

- **Token leakage:** Never log GH_TOKEN or full responses containing secrets. Use redaction in logs.
- **Race conditions on JSON:** Use sha on update; GH will 409 on conflict → agent should re-read + retry or surface to user.
- **Workflow runtime:** The report workflow is intentionally read-only validation. A real promote workflow must be authored (small follow-up PR) that reads the JSON and produces the prod change PRs.
- **LLM non-determinism:** Structured output + validation + re-prompt on parse failure. Temperature low (0 or 0.1).
- **Cost:** Use cheap models (4o-mini) for routing/proposals; reserve larger for complex status narration.
- **Repo sprawl:** One target repo for PoV; make `TARGET_GITHUB_REPO` + per-env manifests configurable.
- **Testing:** Unit (tools with mocked gh), integration (real repo with test branch or separate test repo), graph replay via checkpoints.
- **EKS patterns reuse:** Resource limits, node affinity, securityContext, external-secrets modeled directly on existing aws-eks-prod and qTest.Charts.

---

## 14. Key Decisions

- **Chainlit for initial UI** (fast rich agent experience with built-in steps + human input) over pure custom React or Gradio. Can swap backend later.
- **StateGraph + explicit interrupts** rather than pure `create_react_agent` for predictable HITL and safety routing.
- **gh CLI (via GH_TOKEN)** for PoV fidelity to existing shell tooling and easy local dev. PyGithub as optional future abstraction.
- **Postgres checkpointer in prod**; sqlite for PoV. Never rely on in-memory only.
- **Direct file update + dispatch** (not PRs) for speed in release path; branch protection + audit compensate. Alternative "create PR" tool can be added.
- **image-workflows.json as allowlist source** (read-only); new `prod-image-versions.json` (or equivalent) as the mutable release manifest the chatbot owns.
- **GitHub App preferred long-term** over PAT.
- **Audit as first-class** (structured log entries in state + external sink) because this controls prod changes.
- **Focus strictly on chatbot + orchestration**; treat downstream "update other prod configs" as responsibility of the dispatched workflow (design provides clean handoff via updated JSON + image_tags input).

---

## 15. PR Plan

Ordered list of small, independently reviewable + mergeable PRs. Each builds on previous; can ship PoV after PR-03/04.

1. **PR-01: Repository bootstrap + core LangGraph skeleton**
   - pyproject.toml / src layout, basic deps (langgraph, langchain-openai, pydantic, python-dotenv, structlog).
   - `ReleaseAgentState` Pydantic model + simple StateGraph with route + echo node.
   - Config loading (TARGET_REPO, etc.). Local runnable `python -m ...` stub.
   - Unit tests for state and router.

2. **PR-02: GitHub read-only tools + allowlist integration**
   - Implement `list_allowed_images`, `read_repo_json`, `list_recent_release_runs`, `get_workflow_run_status`.
   - gh CLI wrapper with timeout + error handling.
   - Integration with devops/image-workflows.json parsing.
   - Tool tests (mocked subprocess or live against a public fixture).
   - Graph nodes that call tools and surface results.

3. **PR-03: Mutating tools + HITL interrupts (core safety)**
   - `update_image_tag_in_config` (full contents API roundtrip with sha).
   - `dispatch_release_workflow` (workflow_dispatch).
   - Interrupt nodes + confirmation phrase logic + resume path using `Command`.
   - Audit entry population on every transition.
   - End-to-end graph test using a test branch or dry-run flag (or dedicated test repo).

4. **PR-04: Chat UI (Chainlit) + conversational flows**
   - Chainlit app wiring graph with `cl.Message`, `@cl.on_chat_start`, streaming, step rendering for tool calls.
   - Session thread_id management.
   - Example multi-turn flows matching the spec.
   - Basic FastAPI health + /graph debug endpoints.
   - Local README with exact run commands + sample transcript.

5. **PR-05: Dockerfile, local Docker validation, and PoV packaging**
   - Full Dockerfile (gh install, non-root, healthcheck).
   - docker-compose.yml (optional postgres for checkpointer).
   - Update README with `docker run ... GH_TOKEN=$(gh auth token)` instructions.
   - Verify real dispatch against phaniuk111 repo from container.

6. **PR-06: Kubernetes manifests + deployment assets**
   - Deployment, Service, ConfigMap, Secret (example), ServiceAccount.
   - Probes, resources, securityContext, affinity modeled on promptfoo helm + eks-production patterns.
   - Helm chart skeleton (values.yaml, templates) or Kustomize.
   - docs/deploy.md + example ExternalSecret usage.
   - (Optional) basic NetworkPolicy.

7. **PR-07: Hardening, observability, tests & docs**
   - Full audit logging sink (stdout + optional file/DB).
   - LangSmith tracing + Prometheus `/metrics`.
   - More edge-case tests (invalid tags, conflict on sha, confirmation rejection).
   - Error recovery / retry nodes.
   - Security review checklist in docs.
   - Sample "future promote workflow" YAML stub that consumes the JSON.
   - Final architecture diagram updates + full conversation examples.

8. **PR-08 (post-PoV):** GitHub App auth support, Slack integration hook, multi-repo config, rate limits, PR-creation variant of update tool, production Helm values + Argo CD Application.

Each PR must include:
- Updated README / run instructions.
- Passing tests (or explicit PoV verification steps).
- Security note in PR description for any mutating changes.

---

**End of Design Specification.** Ready for immediate implementation of PoV starting with PR-01. All paths validated against existing devops/ assets and workspace patterns (helm deployments, agent code structure, workflow_dispatch examples, EKS manifests).

## Key Decisions
- Chainlit for initial UI (fast rich agent experience with built-in steps + human input) over pure custom React or Gradio. Can swap backend later.
- StateGraph + explicit interrupts rather than pure `create_react_agent` for predictable HITL and safety routing.
- gh CLI (via GH_TOKEN) for PoV fidelity to existing shell tooling and easy local dev. PyGithub as optional future abstraction.
- Postgres checkpointer in prod; sqlite for PoV. Never rely on in-memory only.
- Direct file update + dispatch (not PRs) for speed in release path; branch protection + audit compensate. Alternative "create PR" tool can be added.
- image-workflows.json as allowlist source (read-only); new `prod-image-versions.json` (or equivalent) as the mutable release manifest the chatbot owns.
- GitHub App preferred long-term over PAT.
- Audit as first-class (structured log entries in state + external sink) because this controls prod changes.
- Focus strictly on chatbot + orchestration; treat downstream "update other prod configs" as responsibility of the dispatched workflow (design provides clean handoff via updated JSON + image_tags input).

## PR Plan
1. PR-01: Repository bootstrap + core LangGraph skeleton
2. PR-02: GitHub read-only tools + allowlist integration
3. PR-03: Mutating tools + HITL interrupts (core safety)
4. PR-04: Chat UI (Chainlit) + conversational flows
5. PR-05: Dockerfile, local Docker validation, and PoV packaging
6. PR-06: Kubernetes manifests + deployment assets
7. PR-07: Hardening, observability, tests & docs
8. PR-08 (post-PoV): GitHub App auth support, Slack integration hook, multi-repo config, rate limits, PR-creation variant of update tool, production Helm values + Argo CD Application.
