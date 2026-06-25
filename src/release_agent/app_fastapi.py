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

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from .agent import get_compiled_graph, message_text
from .config import settings as app_settings

load_dotenv()

# Production-oriented logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("release_copilot")

# Use shared Pydantic settings
settings = app_settings

# Ensure target_repo is set (for health endpoint etc.)
if not settings.target_repo:
    settings.target_repo = "phaniuk111/devops"

app = FastAPI(title=settings.app_title, version="0.2.0")

# CORS (useful if you later want a separate frontend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Single graph instance. For multi-tenant or high scale, scope this per user/team.
graph = get_compiled_graph()


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
    <style>
        body { background: #0f172a; }
        .chat-container { max-height: calc(100vh - 140px); }
        .message { max-width: 85%; }
        .bot { background: #1e2937; }
        .user { background: #3b82f6; }
        .interrupt-box { 
            background: #451a03; 
            border: 1px solid #f59e0b;
        }
        .streaming { opacity: 0.9; }
    </style>
</head>
<body class="text-white">
    <div class="max-w-4xl mx-auto px-4 py-6">
        <!-- Header -->
        <div class="flex items-center justify-between mb-6">
            <div class="flex items-center gap-3">
                <div class="w-10 h-10 bg-emerald-500 rounded-xl flex items-center justify-center">
                    <i class="fa-solid fa-rocket text-white text-xl"></i>
                </div>
                <div>
                    <h1 class="text-2xl font-semibold">Release Copilot</h1>
                    <p class="text-sm text-slate-400">LangGraph + FastAPI • GitHub Actions</p>
                </div>
            </div>
            <div class="flex items-center gap-2 text-sm">
                <div class="px-3 py-1 bg-slate-800 rounded-lg flex items-center gap-2">
                    <i class="fa-solid fa-server text-emerald-400"></i>
                    <span id="thread-label" class="text-slate-300 font-mono text-xs"></span>
                </div>
                <button onclick="newThread()" 
                        class="px-3 py-1 bg-slate-800 hover:bg-slate-700 rounded-lg text-xs flex items-center gap-2">
                    <i class="fa-solid fa-plus"></i>
                    <span>New Thread</span>
                </button>
            </div>
        </div>

        <!-- Chat Area -->
        <div id="chat" 
             class="chat-container overflow-y-auto bg-slate-900 border border-slate-700 rounded-2xl p-4 mb-4 space-y-4">
            <!-- Messages injected here -->
        </div>

        <!-- Input -->
        <div class="flex gap-2">
            <input id="input" 
                   type="text" 
                   placeholder="e.g. promote payments-api:2.0.33 to prod"
                   class="flex-1 bg-slate-800 border border-slate-600 rounded-2xl px-5 py-3 text-white placeholder-slate-400 focus:outline-none focus:border-emerald-500">
            <button onclick="sendMessage()" 
                    class="bg-emerald-600 hover:bg-emerald-500 px-8 rounded-2xl font-medium flex items-center gap-2">
                <span>Send</span>
                <i class="fa-solid fa-paper-plane"></i>
            </button>
        </div>
        <p class="text-[10px] text-slate-500 mt-2 text-center">
            Messages are sent to LangGraph. Confirmations are required before any release actions.
        </p>
    </div>

    <script>
        let threadId = localStorage.getItem('thread_id') || 'fastapi-' + Math.random().toString(36).slice(2, 10);
        localStorage.setItem('thread_id', threadId);
        document.getElementById('thread-label').textContent = threadId;

        // Base path so the UI works at "/" AND under a shared-domain path prefix
        // (e.g. /release-copilot). Derived from where this page is served.
        const API_BASE = window.location.pathname.replace(/\/+$/, '');

        // Minimal, safe markdown -> HTML for streamed assistant text.
        function renderMarkdown(t) {
            t = t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            t = t.replace(/(https?:\\/\\/[^\\s<]+)/g, '<a href="$1" target="_blank" class="underline text-emerald-400">$1</a>');
            t = t.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
            t = t.replace(/`([^`]+)`/g, '<code class="bg-slate-800 px-1 rounded text-emerald-300">$1</code>');
            t = t.replace(/\\n/g, '<br>');
            return t;
        }

        function addMessage(role, content, isStreaming = false) {
            const chat = document.getElementById('chat');
            const div = document.createElement('div');
            
            if (role === 'interrupt') {
                // content may be the full interrupt object (preferred) or a bare string.
                const intr = (content && typeof content === 'object') ? content : { message: content };
                const isBudget = intr.type === 'budget_confirmation';
                const header = isBudget ? 'Budget Confirmation' : 'Confirmation Required';
                const bodyText = renderMarkdown(intr.message || 'Please confirm this action.')
                    + (intr.action ? ('<br><br>' + renderMarkdown(intr.action)) : '');
                const placeholder = isBudget
                    ? 'Type yes to continue, anything else to stop'
                    : 'Paste CONFIRM-XXXXXX here';
                div.className = 'message mx-auto interrupt-box rounded-2xl p-4 text-sm';
                div.innerHTML = `
                    <div class="flex items-center gap-2 mb-2 text-amber-400">
                        <i class="fa-solid fa-exclamation-triangle"></i>
                        <span class="font-semibold">${header}</span>
                    </div>
                    <div class="text-amber-200 mb-3">${bodyText}</div>
                    <div class="flex gap-2">
                        <input id="confirm-input" type="text" placeholder="${placeholder}"
                               class="flex-1 bg-slate-900 border border-amber-600 rounded-lg px-3 py-1.5 text-sm">
                        <button onclick="sendConfirmation()"
                                class="bg-amber-600 hover:bg-amber-500 px-4 rounded-lg text-sm font-medium">
                            Confirm
                        </button>
                    </div>
                `;
            } else {
                div.className = `message ${role === 'user' ? 'ml-auto user' : 'bot'} rounded-2xl px-4 py-3 text-sm`;
                div.innerHTML = `<div class="${isStreaming ? 'streaming' : ''}">${content}</div>`;
            }
            
            chat.appendChild(div);
            chat.scrollTop = chat.scrollHeight;
            return div;
        }

        function updateLastMessage(content) {
            const chat = document.getElementById('chat');
            const last = chat.lastElementChild;
            if (last) {
                const contentDiv = last.querySelector('div');
                if (contentDiv) contentDiv.innerHTML = content;
            }
        }

        async function sendMessage() {
            const input = document.getElementById('input');
            const message = input.value.trim();
            if (!message) return;

            addMessage('user', message);
            input.value = '';

            const botMsg = addMessage('bot', 'Thinking...', true);

            try {
                const res = await fetch(API_BASE + '/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message, thread_id: threadId })
                });

                if (!res.ok) throw new Error(await res.text());

                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                let fullText = '';
                let isInterrupt = false;
                let buffer = '';

                function handleEvent(rawEvent) {
                    for (const line of rawEvent.split('\\n')) {
                        if (!line.startsWith('data: ')) continue;
                        try {
                            const data = JSON.parse(line.slice(6));
                            if (data.type === 'token') {
                                fullText += (fullText ? '\\n\\n' : '') + data.content;
                                botMsg.querySelector('div').innerHTML = renderMarkdown(fullText);
                            } else if (data.type === 'interrupt') {
                                isInterrupt = true;
                                botMsg.remove();
                                addMessage('interrupt', data.data || {});
                            } else if (data.type === 'done') {
                                // finished
                            } else if (data.type === 'error') {
                                botMsg.querySelector('div').innerHTML =
                                    '<span class="text-red-400">' + (data.content || 'Error') + '</span>';
                            }
                        } catch (e) { console.error('SSE parse error', e, line); }
                    }
                }

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    // Accumulate across reads; SSE events are delimited by a blank line.
                    // A frame split mid-line would otherwise be dropped by the silent catch.
                    buffer += decoder.decode(value, { stream: true });
                    let sep;
                    while ((sep = buffer.indexOf('\\n\\n')) !== -1) {
                        const rawEvent = buffer.slice(0, sep);
                        buffer = buffer.slice(sep + 2);
                        handleEvent(rawEvent);
                    }
                }
                buffer += decoder.decode();
                if (buffer.trim()) handleEvent(buffer);

                if (!isInterrupt && botMsg) {
                    botMsg.querySelector('div').classList.remove('streaming');
                }
            } catch (err) {
                botMsg.querySelector('div').innerHTML = `<span class="text-red-400">Error: ${err.message}</span>`;
            }
        }

        function sendConfirmation() {
            const input = document.getElementById('confirm-input');
            if (!input) return;
            const value = input.value.trim();
            if (!value) return;

            // Send the confirmation token as a regular message
            const chat = document.getElementById('chat');
            // Remove the interrupt box
            const last = chat.lastElementChild;
            if (last) last.remove();

            // Send as normal message
            const hiddenInput = document.getElementById('input');
            hiddenInput.value = value;
            sendMessage();
        }

        function newThread() {
            threadId = 'fastapi-' + Math.random().toString(36).slice(2, 10);
            localStorage.setItem('thread_id', threadId);
            document.getElementById('thread-label').textContent = threadId;
            document.getElementById('chat').innerHTML = '';
            addMessage('bot', 'New conversation started. How can I help with releases?');
        }

        // Welcome message
        window.onload = () => {
            const chat = document.getElementById('chat');
            if (chat.children.length === 0) {
                addMessage('bot', 'Hello! I can help you update image tags and trigger release workflows.<br><br>Try: <code class="bg-slate-800 px-1 rounded">promote payments-api:2.0.33</code>');
            }
        };

        // Enter key support
        document.getElementById('input').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') sendMessage();
        });
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html)


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    """Streaming chat endpoint using Server-Sent Events (SSE).

    Production notes:
    - This endpoint streams tokens + special events (interrupt for confirmation).
    - For high load, consider running with multiple workers + a persistent
      checkpointer (Postgres) instead of in-memory.
    """
    thread_id = get_or_create_thread_id(req.thread_id)
    config = {"configurable": {"thread_id": thread_id}}

    logger.info(f"Chat request | thread={thread_id} | msg_len={len(req.message)}")

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            # Detect if we are resuming from an interrupt (HITL). Any pending
            # interrupt (confirmation gate OR budget) means the user's message is
            # a resume value, not a new turn.
            snapshot = graph.get_state(config)
            is_resuming = bool(getattr(snapshot, "interrupts", None))

            if is_resuming:
                input_data = Command(resume=req.message)
                logger.info(f"Resuming from interrupt | thread={thread_id}")
            else:
                input_data = {"messages": [HumanMessage(content=req.message)]}

            # Stream updates from the graph. Only assistant (AIMessage) text is
            # surfaced to the UI — never the system prompt, internal HumanMessages,
            # or raw ToolMessage JSON (which previously dumped the whole prompt).
            async for chunk in graph.astream(input_data, config=config, stream_mode="updates"):
                for node_name, update in chunk.items():
                    if not isinstance(update, dict):
                        continue
                    for msg in update.get("messages", []) or []:
                        if isinstance(msg, AIMessage):
                            text = message_text(msg)
                            if text:
                                payload = json.dumps({"type": "token", "content": text})
                                yield f"data: {payload}\n\n"

            # After the turn, check for pending confirmation (interrupt)
            snapshot = graph.get_state(config)
            if snapshot.interrupts:
                interrupt_data = snapshot.interrupts[0].value
                payload = json.dumps({"type": "interrupt", "data": interrupt_data})
                yield f"data: {payload}\n\n"
                logger.info(f"Interrupt emitted | thread={thread_id}")

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            logger.exception(f"Error in chat stream | thread={thread_id}")
            error_payload = json.dumps({"type": "error", "content": "Internal error processing request"})
            yield f"data: {error_payload}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # critical when behind nginx
        }
    )


@app.get("/health")
async def health():
    """Health check endpoint for Kubernetes liveness/readiness probes."""
    return {
        "status": "ok",
        "service": "release-copilot-fastapi",
        "target_repo": settings.target_repo,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"Starting Release Copilot FastAPI on http://localhost:{port}")
    uvicorn.run("src.release_agent.app_fastapi:app", host="0.0.0.0", port=port, reload=True)
