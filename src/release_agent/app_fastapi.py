"""
FastAPI-based Chat Interface for Release Copilot (Preferred Production UI)

This is the recommended path for production / Kubernetes deployments.

Run locally:
    uvicorn src.release_agent.app_fastapi:app --reload --port 8000

Production example:
    uvicorn src.release_agent.app_fastapi:app --host 0.0.0.0 --port 8000 --workers 4

For more advanced setups, consider running behind a reverse proxy (nginx/traefik)
with proper auth, TLS, and observability.
"""

import json
import logging
import os
import time
import uuid
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .adk_service import get_adk_chat_service
from .config import settings as app_settings
from .session_creds import SessionCredentials, get_store

# Production-oriented logging
logging.basicConfig(level=logging.INFO)

# Cache-buster for static assets: browsers heuristically cache app.js (no
# Cache-Control header from StaticFiles); stamping the URL per server start
# guarantees a restart always ships fresh UI code.
APP_STARTED = str(int(time.time()))
logger = logging.getLogger("release_copilot")

# Use shared Pydantic settings (repos come from env / .env / Helm ConfigMap).
settings = app_settings

app = FastAPI(title=settings.app_title, version="0.2.0")

# Serve the UI's JavaScript from real files (not embedded in a Python string) so it
# is lint/syntax-checkable and free of Python-string escaping traps.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles + Cache-Control: no-cache. ES-module imports (./status.js etc.)
    carry no ?v= buster, so browsers would heuristically cache them across server
    restarts and run stale UI code. no-cache forces an ETag revalidation per load —
    cheap (304s) and always fresh."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _NoCacheStaticFiles(directory=_STATIC_DIR), name="static")

# CORS (useful if you later want a separate frontend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Single ADK-backed chat service. For multi-tenant or high scale, back this with
# persistent ADK session/artifact services instead of in-memory services.
adk_chat_service = get_adk_chat_service()


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None


class SessionConnectRequest(BaseModel):
    thread_id: str
    pat_token: str


class SessionThreadRequest(BaseModel):
    thread_id: str


_session_store = get_store()


def get_or_create_thread_id(thread_id: str | None) -> str:
    if not thread_id:
        return f"fastapi-{uuid.uuid4().hex[:8]}"
    return thread_id


@app.get("/", response_class=HTMLResponse)
async def chat_page():
    """Serve a clean, self-contained chat UI."""
    html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dev Portal</title>
    <script>
    // Served under a path prefix (e.g. /dev-portal) the TRAILING SLASH decides
    // how relative asset URLs resolve: at "/dev-portal" the browser resolves
    // "static/main.js" against the parent -> "/static/main.js" at the domain
    // root -> 404 -> no JS -> a dead page that still renders its static shell.
    // Pin the base to the directory form so assets resolve inside the prefix
    // whether or not the ingress issues the usual "/prefix" -> "/prefix/"
    // redirect. Must run before any relative URL is parsed.
    (function () {
        var path = window.location.pathname;
        if (path.charAt(path.length - 1) !== '/') path += '/';
        var base = document.createElement('base');
        base.href = path;
        document.head.appendChild(base);
    })();
    </script>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { font-family: 'Inter', ui-sans-serif, system-ui, -apple-system, sans-serif; }
        body {
            color: #e5e7eb;
            background:
                radial-gradient(1100px 560px at 12% -8%, rgba(16,185,129,.12), transparent 60%),
                radial-gradient(900px 520px at 100% 0%, rgba(45,212,191,.08), transparent 55%),
                #080d1a;
            min-height: 100vh;
        }
        .glass {
            background: rgba(15, 23, 42, .55);
            backdrop-filter: blur(16px) saturate(140%);
            -webkit-backdrop-filter: blur(16px) saturate(140%);
            border: 1px solid rgba(148,163,184,.12);
        }
        .brand-grad { background: linear-gradient(135deg, #10b981, #2dd4bf); }
        .chat-container { max-height: calc(100vh - 250px); scroll-behavior: smooth; }
        .chat-container::-webkit-scrollbar { width: 8px; }
        .chat-container::-webkit-scrollbar-thumb { background: rgba(148,163,184,.18); border-radius: 99px; }
        .chat-container::-webkit-scrollbar-thumb:hover { background: rgba(148,163,184,.30); }
        .message { max-width: 84%; line-height: 1.6; animation: rise .28s cubic-bezier(.2,.8,.2,1); }
        @keyframes rise { from { opacity: 0; transform: translateY(7px); } to { opacity: 1; transform: none; } }
        .bot { background: rgba(30,41,59,.6); border: 1px solid rgba(148,163,184,.10); }
        .user { background: linear-gradient(135deg, #10b981, #2dd4bf); color: #04241c; font-weight: 500; }
        .bot code { background: rgba(2,6,23,.55) !important; color: #6ee7b7 !important; }
        .interrupt-box { background: rgba(60,24,4,.6); border: 1px solid rgba(245,158,11,.6); backdrop-filter: blur(10px); }
        .streaming { opacity: .92; }
        .dots span { display: inline-block; width: 6px; height: 6px; margin: 0 2px; border-radius: 99px; background: #64748b; animation: blink 1.2s infinite; }
        .dots span:nth-child(2) { animation-delay: .2s; }
        .dots span:nth-child(3) { animation-delay: .4s; }
        @keyframes blink { 0%,80%,100% { opacity: .25; transform: translateY(0); } 40% { opacity: 1; transform: translateY(-3px); } }
        #input { transition: box-shadow .15s, border-color .15s; }
        #input:focus { box-shadow: 0 0 0 3px rgba(16,185,129,.18); }
        .send-btn { background: linear-gradient(135deg, #10b981, #2dd4bf); color: #04241c; transition: filter .15s, transform .1s; }
        .send-btn:hover { filter: brightness(1.07); }
        .send-btn:active { transform: scale(.97); }
        .navbtn { transition: background .15s, border-color .15s; }
    </style>
</head>
<body class="text-white">
    <div class="max-w-6xl mx-auto px-4 py-6 flex gap-4 items-start">
      <div class="flex-1 min-w-0">
        <!-- Header -->
        <div class="flex items-center justify-between mb-6">
            <div class="flex items-center gap-3">
                <div class="w-10 h-10 brand-grad rounded-xl flex items-center justify-center shadow-lg shadow-emerald-500/20">
                    <i class="fa-solid fa-rocket text-[#04241c] text-xl"></i>
                </div>
                <div>
                    <h1 class="text-2xl font-semibold tracking-tight">Dev Portal</h1>
                    <p class="text-xs text-slate-400 tracking-wide">Deploys, releases &amp; insights</p>
                </div>
            </div>
            <div class="flex items-center gap-2 text-sm">
                <div class="px-3 py-1 glass rounded-lg flex items-center gap-2">
                    <i class="fa-solid fa-server text-emerald-400"></i>
                    <span id="thread-label" class="text-slate-300 font-mono text-xs"></span>
                </div>
                <button id="repo-chip" onclick="showConnectForm()"
                        class="px-3 py-1 glass navbtn hover:border-emerald-400/30 rounded-lg text-xs flex items-center gap-2">
                    <i id="repo-chip-icon" class="fa-brands fa-github text-slate-400"></i>
                    <span id="repo-chip-label" class="text-slate-300">Connect with GitHub</span>
                </button>
                <button onclick="openPalette()"
                        class="px-3 py-1 glass navbtn hover:border-emerald-400/30 rounded-lg text-xs flex items-center gap-2">
                    <i class="fa-solid fa-wand-magic-sparkles text-emerald-400"></i>
                    <span>What can I do?</span>
                    <span class="text-slate-500 font-mono text-[10px] border border-slate-700 rounded px-1">⌘K</span>
                </button>
                <button onclick="newThread()"
                        class="px-3 py-1 glass navbtn hover:border-emerald-400/30 rounded-lg text-xs flex items-center gap-2">
                    <i class="fa-solid fa-plus"></i>
                    <span>New Thread</span>
                </button>
                <button id="panel-toggle" onclick="toggleInsights()" title="Insights panel"
                        class="px-3 py-1 glass navbtn hover:border-emerald-400/30 rounded-lg text-xs flex items-center gap-2">
                    <i class="fa-solid fa-chart-column text-emerald-400"></i>
                </button>
            </div>
        </div>

        <!-- One-line status strip (click 'details' for the full picture) -->
        <div id="release-banner"
             class="mb-3 rounded-xl border px-3 py-1.5 text-xs hidden border-slate-800 bg-slate-900/60">
            <div class="flex items-center gap-2">
                <span id="rb-dot" class="w-2 h-2 rounded-full bg-slate-500 inline-block"></span>
                <span id="rb-title" onclick="toggleBannerDetail()"
                      class="text-slate-300 truncate cursor-pointer hover:text-white"
                      title="Click to see the charts in each environment">Checking release window…</span>
                <span id="rb-age" class="text-slate-600 text-[11px]"></span>
                <button id="rb-toggle" onclick="toggleBannerDetail()" class="text-slate-500 hover:text-slate-300">details</button>
                <span class="flex-1"></span>
                <button onclick="loadReleaseStatus(true)" title="Refresh now (live read)" class="text-slate-600 hover:text-slate-400">
                    <i class="fa-solid fa-rotate-right"></i>
                </button>
            </div>
            <div id="rb-detail" class="hidden text-slate-400 mt-1 pl-4"></div>
        </div>

        <!-- Chat Area -->
        <div id="chat"
             class="chat-container overflow-y-auto glass rounded-2xl p-5 mb-4 space-y-4">
            <!-- Messages injected here -->
        </div>

        <!-- Input -->
        <div class="flex gap-2">
            <input id="input" 
                   type="text" 
                   placeholder="Message, or / for commands…"
                   class="flex-1 glass rounded-2xl px-5 py-3.5 text-white placeholder-slate-500 focus:outline-none">
            <button onclick="sendMessage()" 
                    class="send-btn px-8 rounded-2xl font-semibold flex items-center gap-2">
                <span>Send</span>
                <i class="fa-solid fa-paper-plane"></i>
            </button>
        </div>
        <p class="text-[10px] text-slate-500 mt-2 text-center">
            Messages are sent to ADK. Confirmations are required before any release actions.
        </p>
      </div>

      <!-- Insights drawer: collapsible report sections (rendered by app.js) -->
      <aside id="insights-panel" class="hidden w-72 shrink-0 glass rounded-2xl p-3 sticky top-6">
          <div class="flex items-center justify-between mb-2 px-1">
              <span class="text-xs font-semibold text-slate-300">
                  <i class="fa-solid fa-chart-column text-emerald-400 mr-1"></i>Insights</span>
              <button onclick="toggleInsights()" class="text-slate-500 hover:text-slate-300 text-xs">
                  <i class="fa-solid fa-xmark"></i></button>
          </div>
          <div id="insights-sections" class="space-y-2"></div>
      </aside>
    </div>

    <!-- Vendored Chart.js (no CDN — must work behind the corporate proxy).
         Loaded before the module bundle so `Chart` is global when charts render. -->
    <script src="static/vendor/chart.umd.min.js?v={APP_STARTED}"></script>
    <script type="module" src="static/main.js?v={APP_STARTED}"></script>
</body>
</html>
    """
    return HTMLResponse(content=html.replace("{APP_STARTED}", APP_STARTED))


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    """Streaming chat endpoint using Server-Sent Events (SSE).

    Production notes:
    - This endpoint streams tokens + special events (interrupt for confirmation).
    - For high load, consider using persistent ADK session/artifact services
      instead of in-memory services.
    """
    thread_id = get_or_create_thread_id(req.thread_id)

    logger.info(f"Chat request | thread={thread_id} | msg_len={len(req.message)}")

    async def event_generator() -> AsyncGenerator[str, None]:
        # Bind this thread's connected repo + PAT (if any) for the whole turn so
        # every GitHub tool call resolves them; falls back to server config when
        # the session isn't connected. contextvars propagate across await/threads.
        try:
            with _session_store.activate(thread_id):
                async for event in adk_chat_service.stream_chat(req.message, thread_id):
                    if event.get("type") == "interrupt":
                        logger.info(f"Interrupt emitted | thread={thread_id}")
                    yield f"data: {json.dumps(event)}\n\n"

        except Exception:
            logger.exception(f"Error in chat stream | thread={thread_id}")
            error_payload = json.dumps(
                {"type": "error", "content": "Internal error processing request"}
            )
            yield f"data: {error_payload}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # critical when behind nginx
        },
    )


@app.post("/api/session/connect")
async def session_connect_endpoint(req: SessionConnectRequest):
    """Connect this chat thread to GitHub with the user's PAT token.

    The PAT is held in memory only and never logged or returned (the response
    carries a masked preview). All GitHub operations in this thread then run as
    this user against the server-configured repositories.
    """
    thread_id = get_or_create_thread_id(req.thread_id)
    creds = SessionCredentials(pat_token=req.pat_token or "")
    if not creds.pat_token:
        return {"ok": False, "error": "A PAT token is required to connect."}

    _session_store.set(thread_id, creds)
    logger.info("Session connected | thread=%s", thread_id)  # never log the token
    return {"ok": True, "thread_id": thread_id, **creds.public_status()}


@app.get("/api/session/status")
async def session_status_endpoint(thread_id: str = ""):
    """Return the (token-masked) connection status for a thread."""
    creds = _session_store.get(thread_id) if thread_id else None
    if creds is None:
        return {"connected": False, "token_preview": ""}
    return creds.public_status()


@app.post("/api/session/disconnect")
async def session_disconnect_endpoint(req: SessionThreadRequest):
    """Clear a thread's stored repo + PAT (called on New Thread / Disconnect)."""
    _session_store.clear(req.thread_id)
    return {"ok": True, "connected": False}


# NOTE: the read endpoints below are deliberately `def`, not `async def`.
# They perform SYNCHRONOUS network I/O (GitHub REST via PyGithub, BigQuery
# query jobs). On the event loop that blocks every other request — with a
# handful of concurrent users the portal serializes (measured: 5 concurrent
# banner loads took 22.7s instead of ~6.5s). As plain `def`, FastAPI runs them
# in its threadpool and they overlap. /api/chat stays async: it streams and its
# work is already awaited or dispatched to threads.
# The banner is SHARED state (same answer for everyone) but costs 5 GitHub API
# calls per load. With a team on one PAT (5000/hr) that is the first thing to
# exhaust the rate limit, so serve it from a short cache. Anyone who just acted
# passes fresh=1 (after a chat turn, or the manual refresh) and bypasses it.
_STATUS_TTL_SECONDS = 15.0
_status_cache: dict = {"at": 0.0, "value": None}


@app.get("/api/release-status")
def release_status_endpoint(fresh: int = 0):
    """Today's PRD release window — read live from GitHub so every session/developer
    sees the same answer (the PRD PR is the shared source of truth)."""
    from .tools.gh_tools import get_release_status

    if not fresh and _status_cache["value"] is not None:
        if time.time() - _status_cache["at"] < _STATUS_TTL_SECONDS:
            return _status_cache["value"]
    try:
        status = get_release_status()
    except Exception as e:
        logger.exception("Error computing release status")
        return {"error": str(e)}
    # Banner extra: how many charts are queued for the NEXT release (cached ≤1/min;
    # None/absent when the BQ queue is disabled or unreachable — never an error).
    try:
        from .tools import release_queue

        count = release_queue.cached_queue_count()
        if count is not None:
            status["queued_next"] = count
    except Exception:
        pass
    _status_cache["at"] = time.time()
    _status_cache["value"] = status
    return status


# --- Next-release intake queue (BigQuery-backed) -----------------------------
class QueueAddRequest(BaseModel):
    artifact: str  # chart:version (or full artifactory URL)
    requested_by: str
    prl1_only: bool = False
    df_only: bool = False
    note: str = ""
    jira_ticket: str = ""
    change_details: str = ""  # dev's what-changed-and-why → CHG draft on release day
    build_run_url: str = ""  # Actions run that built the tag → eligibility check at queue time


class QueueWithdrawRequest(BaseModel):
    artifact_name: str
    requested_by: str = ""


_known_charts_cache: dict = {"at": 0.0, "charts": []}


def _known_charts() -> list[str]:
    """Chart-name datalist for the queue form — from the build repo's image
    catalog, cached 5 minutes, empty on any failure."""
    import time as _time

    now = _time.time()
    if now - _known_charts_cache["at"] < 300:
        return _known_charts_cache["charts"]
    charts: list[str] = []
    try:
        from .tools.manifest import list_allowed_images

        data = json.loads(list_allowed_images())
        charts = sorted(data.get("allowed_images") or [])
    except Exception:
        pass
    _known_charts_cache["at"] = now
    _known_charts_cache["charts"] = charts
    return charts


@app.get("/api/release-queue")
def release_queue_get():
    """The accumulated next-release queue + form context (default repo, known
    chart names). Powers the Insights panel and the Create-release pre-fill."""
    from .tools import release_queue
    from .tools._common import active_deploy_repo

    result = release_queue.current_queue()
    result["default_repo"] = app_settings.deploy_repo
    try:
        result["default_repo"] = active_deploy_repo() or app_settings.deploy_repo
    except Exception:
        pass
    result["known_charts"] = _known_charts()
    return result


@app.post("/api/release-queue")
def release_queue_add(req: QueueAddRequest):
    """Queue a chart:version for the next release (the 'Monday dev' path).
    Runs the courtesy build check and reports last-time routing, same as the
    conversational intake."""
    from adk_release_agent.tools import queue_release_intent

    return queue_release_intent(
        artifact=req.artifact,
        requested_by=req.requested_by,
        prl1_only=req.prl1_only,
        df_only=req.df_only,
        note=req.note,
        jira_ticket=req.jira_ticket,
        change_details=req.change_details,
        build_run_url=req.build_run_url,
    )


@app.post("/api/release-queue/withdraw")
def release_queue_withdraw(req: QueueWithdrawRequest):
    """Withdraw a chart from the next-release queue (append-only: writes a
    'withdrawn' event; nothing is deleted)."""
    from .tools import release_queue

    return release_queue.withdraw_intent(req.artifact_name, req.requested_by)


@app.get("/api/release-insights")
def release_insights(pattern: str = "", days: int = 90, event_type: str = "released"):
    """Stats over the release/deploy history event log — powers the Insights
    panel's Release stats section (which images released, per-chart counts,
    pattern filter like acme-capability*)."""
    from .tools import release_queue

    return release_queue.history_stats(pattern=pattern, days=days, event_type=event_type)


@app.get("/api/deployment-types")
async def deployment_types_endpoint():
    """The IDP capability registry — the UI renders deploy cards/forms from this."""
    from .deployment_types import deployment_types

    return deployment_types()


@app.get("/api/df-template")
def df_template_endpoint(env: str = "uat"):
    """Recent DF deploy workflow runs plus the default repo — pre-fills the DF form's
    'recent deploys' context strip. (Deploy = workflow_dispatch; there is no state file.)"""
    import itertools

    from .tools._common import _get_github_client

    runs: list = []
    try:
        if app_settings.df_deploy_repo:
            repo = _get_github_client().get_repo(app_settings.df_deploy_repo)
            workflow = repo.get_workflow(app_settings.df_deploy_workflow)
            for r in itertools.islice(workflow.get_runs(), 3):
                runs.append(
                    {
                        "id": r.id,
                        "url": r.html_url,
                        "status": r.status,
                        "conclusion": r.conclusion,
                        "created_at": r.created_at.isoformat() if r.created_at else "",
                    }
                )
    except Exception:
        logger.exception("df-template: could not list DF workflow runs")
    return {
        "environment": "uat",
        "recent_runs": runs,
        "deploy_repo": app_settings.df_deploy_repo,
        "workflow": app_settings.df_deploy_workflow,
    }


@app.get("/api/deploy-template")
def deploy_template_endpoint(env: str = "uat", name: str = "", version: str = ""):
    """Pre-fill the UI's editable JSON box with the ACTUAL current deployment.json for the
    env — uat/deployment.json from the UAT branch, prd/deployment.json from PRD — so the dev
    edits the real deployed set, not a blank template. If a chart name+version is supplied
    (from a chat/CLI deploy command) it's upserted into that current set. Constants
    (helm_chart_dir, env values-file, namespace) come from config."""
    from .tools.gh_tools import assemble_entry
    from .tools._common import _get_github_client, settings, _read_json_file

    e = "prod" if str(env).lower() in ("prod", "prd", "production") else "uat"
    env_key = "prd" if e == "prod" else "uat"
    path = settings.deployment_path_pattern.format(env=env_key)
    branch = settings.prd_branch if e == "prod" else settings.uat_branch

    include: list = []
    from_repo = False
    try:
        repo = _get_github_client().get_repo(settings.deploy_repo)
        doc = _read_json_file(repo, branch, path)
        inc = doc.get("include") if isinstance(doc, dict) else None
        if isinstance(inc, list):
            include = [x for x in inc if isinstance(x, dict)]
            from_repo = True
    except Exception:
        logger.exception("deploy-template: could not read current %s on %s", path, branch)

    # Upsert the requested chart (from a chat command) into the current set, by chart name.
    if name and version:
        entry = assemble_entry(name, version, e)
        for i, x in enumerate(include):
            if x.get("helm_chart_name") == name:
                include[i] = entry
                break
        else:
            include.append(entry)

    # Empty repo / very first deploy: fall back to a single (blank or requested) entry.
    if not include:
        include = [assemble_entry(name or "", version or "", e)]

    return {
        "environment": e,
        "deployment": {"include": include},
        "from_repo": from_repo,
        # Default target for the form's "Deployment repo" field (user-overridable;
        # travels in the deploy JSON payload as deployment_repo).
        "deploy_repo": settings.deploy_repo,
    }


@app.get("/api/diagnostics")
def diagnostics():
    """One-shot dependency report for a fresh deployment: what is configured and
    whether each backend actually answers. Deliberately NOT part of /health —
    it makes live calls, so probes must not run it. Never returns secrets: the
    GitHub token is reported as a boolean only.

    Typical reads:
      vertex.ok false + "API key"        -> GOOGLE_GENAI_USE_VERTEXAI not "true"
      vertex.ok false + 403/PermissionDenied -> Workload Identity or missing
                                            roles/aiplatform.user in the Vertex project
      vertex.ok false + 404 on the model -> model not served in GOOGLE_CLOUD_LOCATION
      vertex.ok false + 429              -> quota; request an increase
      github.ok false + 404              -> wrong repo, or the token can't see it
      bq.ok false + 404 Not found: Table -> table not provisioned yet
    """
    import os as _os

    from .tools._common import _resolve_github_token, active_deploy_repo

    report: dict = {
        "config": {
            "GOOGLE_CLOUD_PROJECT": settings.gcp_project or "(unset)",
            "GOOGLE_CLOUD_LOCATION": settings.gcp_location,
            "GEMINI_MODEL": settings.gemini_model,
            "GOOGLE_GENAI_USE_VERTEXAI": _os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "(unset)"),
            "BUILD_REPO": settings.build_repo or "(unset)",
            "DEPLOY_REPO": settings.deploy_repo or "(unset)",
            "DF_BUILD_REPO": settings.df_build_repo or "(unset — falls back to BUILD_REPO)",
            "GITHUB_BASE_URL": settings.github_base_url or "(github.com)",
            "github_token_present": bool(_resolve_github_token()),
            "HTTPS_PROXY": _os.getenv("HTTPS_PROXY") or "(none)",
            "NO_PROXY": _os.getenv("NO_PROXY") or "(default)",
            "BQ": (
                f"{settings.bq_project or settings.gcp_project}."
                f"{settings.bq_dataset}.{settings.bq_table}"
                if settings.bq_dataset else "(disabled)"
            ),
        }
    }

    # Vertex: the smallest possible real generation — proves auth, region and model.
    try:
        from google import genai

        client = genai.Client()
        resp = client.models.generate_content(
            model=settings.gemini_model,
            contents="ping",
            config={"max_output_tokens": 1},
        )
        report["vertex"] = {"ok": True, "model": settings.gemini_model,
                           "responded": bool(resp)}
    except Exception as e:
        report["vertex"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}

    # GitHub: can we actually see the deploy repo with the resolved token?
    try:
        from .tools._common import _get_github_client

        repo_full = active_deploy_repo()
        if not repo_full:
            report["github"] = {"ok": False, "error": "DEPLOY_REPO is not configured"}
        else:
            repo = _get_github_client().get_repo(repo_full)
            report["github"] = {"ok": True, "repo": repo.full_name,
                                "default_branch": repo.default_branch}
    except Exception as e:
        report["github"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}

    # BigQuery: only meaningful when the queue feature is switched on.
    try:
        from .tools import release_queue

        if not release_queue.queue_enabled():
            report["bq"] = {"ok": None, "disabled": True,
                            "note": "BQ_DATASET or project unset — queue/analytics off"}
        else:
            q = release_queue.current_queue(use_cache=False)
            report["bq"] = ({"ok": True, "queued": q.get("count")} if q.get("ok")
                            else {"ok": False, "error": str(q.get("error"))[:300]})
    except Exception as e:
        report["bq"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}

    report["ok"] = bool(report["vertex"].get("ok") and report["github"].get("ok"))
    return report


@app.get("/health")
async def health():
    """Health check endpoint for Kubernetes liveness/readiness probes."""
    return {
        "status": "ok",
        "service": "release-copilot-fastapi-adk",
        "build_repo": settings.build_repo,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    print(f"Starting Dev Portal FastAPI on http://localhost:{port}")
    uvicorn.run("src.release_agent.app_fastapi:app", host="0.0.0.0", port=port, reload=True)
