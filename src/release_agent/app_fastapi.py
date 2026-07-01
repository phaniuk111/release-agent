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
import uuid
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .adk_service import get_adk_chat_service
from .config import settings as app_settings

# Production-oriented logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("release_copilot")

# Use shared Pydantic settings (repos come from env / .env / Helm ConfigMap).
settings = app_settings

app = FastAPI(title=settings.app_title, version="0.2.0")

# Serve the UI's JavaScript from a real file (not embedded in a Python string) so it
# is lint/syntax-checkable and free of Python-string escaping traps.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

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
    <title>Release Copilot</title>
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
    <div class="max-w-4xl mx-auto px-4 py-6">
        <!-- Header -->
        <div class="flex items-center justify-between mb-6">
            <div class="flex items-center gap-3">
                <div class="w-10 h-10 brand-grad rounded-xl flex items-center justify-center shadow-lg shadow-emerald-500/20">
                    <i class="fa-solid fa-rocket text-[#04241c] text-xl"></i>
                </div>
                <div>
                    <h1 class="text-2xl font-semibold tracking-tight">Release Copilot</h1>
                    <p class="text-xs text-slate-400 tracking-wide">Deploy &amp; release management</p>
                </div>
            </div>
            <div class="flex items-center gap-2 text-sm">
                <div class="px-3 py-1 glass rounded-lg flex items-center gap-2">
                    <i class="fa-solid fa-server text-emerald-400"></i>
                    <span id="thread-label" class="text-slate-300 font-mono text-xs"></span>
                </div>
                <button onclick="showCapabilities()"
                        class="px-3 py-1 glass navbtn hover:border-emerald-400/30 rounded-lg text-xs flex items-center gap-2">
                    <i class="fa-solid fa-wand-magic-sparkles text-emerald-400"></i>
                    <span>What can I do?</span>
                </button>
                <button onclick="newThread()"
                        class="px-3 py-1 glass navbtn hover:border-emerald-400/30 rounded-lg text-xs flex items-center gap-2">
                    <i class="fa-solid fa-plus"></i>
                    <span>New Thread</span>
                </button>
            </div>
        </div>

        <!-- Today's PRD release window (shared across all sessions via GitHub) -->
        <div id="release-banner"
             class="mb-4 rounded-2xl border px-4 py-3 text-sm hidden border-slate-700 bg-slate-900">
            <div class="flex items-center gap-3">
                <i id="rb-icon" class="fa-solid fa-circle-notch fa-spin text-slate-400"></i>
                <div class="flex-1">
                    <div id="rb-title" class="font-semibold text-slate-200">Checking today's release window…</div>
                    <div id="rb-detail" class="text-xs text-slate-400 mt-0.5"></div>
                </div>
                <button onclick="loadReleaseStatus()" title="Refresh"
                        class="text-slate-500 hover:text-slate-300 text-xs">
                    <i class="fa-solid fa-rotate-right"></i>
                </button>
            </div>
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
                   placeholder="e.g. deploy my-chart 1.2.3 to uat"
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

    <script src="static/app.js"></script>
</body>
</html>
    """
    return HTMLResponse(content=html)


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
        try:
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


@app.get("/api/release-status")
async def release_status_endpoint():
    """Today's PRD release window — read live from GitHub so every session/developer
    sees the same answer (the PRD PR is the shared source of truth)."""
    from .tools.gh_tools import get_release_status

    try:
        return get_release_status()
    except Exception as e:
        logger.exception("Error computing release status")
        return {"error": str(e)}


@app.get("/api/deploy-template")
async def deploy_template_endpoint(env: str = "uat", name: str = "", version: str = ""):
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

    return {"environment": e, "deployment": {"include": include}, "from_repo": from_repo}


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
    print(f"Starting Release Copilot FastAPI on http://localhost:{port}")
    uvicorn.run("src.release_agent.app_fastapi:app", host="0.0.0.0", port=port, reload=True)
