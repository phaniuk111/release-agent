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
from fastapi import FastAPI
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

# Use shared Pydantic settings (repos come from env / .env / Helm ConfigMap).
settings = app_settings

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
            Messages are sent to LangGraph. Confirmations are required before any release actions.
        </p>
    </div>

    <script>
        let threadId = localStorage.getItem('thread_id') || 'fastapi-' + Math.random().toString(36).slice(2, 10);
        localStorage.setItem('thread_id', threadId);
        document.getElementById('thread-label').textContent = threadId;

        // Base path so the UI works at "/" AND under a shared-domain path prefix
        // (e.g. /release-copilot). Derived from where this page is served.
        const API_BASE = (function () {        // strip trailing slashes (regex-free)
            let p = window.location.pathname;
            while (p.endsWith('/')) p = p.slice(0, -1);
            return p;
        })();

        // Minimal, safe markdown -> HTML for streamed assistant text.
        function renderMarkdown(t) {
            t = t.split('&').join('&amp;').split('<').join('&lt;').split('>').join('&gt;');
            // [text](url) markdown links -> stash so the bare-URL linkifier below
            // doesn't double-wrap the URL inside the href attribute.
            const _links = [];
            t = t.replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)/g, function(m, txt, url) {
                _links.push('<a href="' + url + '" target="_blank" class="underline text-emerald-400">' + txt + '</a>');
                return 'LINKTOKEN' + (_links.length - 1) + 'ENDTOKEN';
            });
            t = t.replace(/(https?:\\/\\/[^\\s<]+)/g, '<a href="$1" target="_blank" class="underline text-emerald-400">$1</a>');
            t = t.replace(/LINKTOKEN(\\d+)ENDTOKEN/g, function(m, i) { return _links[+i]; });
            t = t.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
            t = t.replace(/`([^`]+)`/g, '<code class="bg-slate-800 px-1 rounded text-emerald-300">$1</code>');
            t = t.split('\\n').join('<br>');
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

        async function sendMessage(overrideText) {
            const input = document.getElementById('input');
            // overrideText lets callers send multi-line messages (the single-line
            // text input strips newlines, which breaks the PROD change-ticket form).
            const message = (typeof overrideText === 'string' ? overrideText : input.value).trim();
            if (!message) return;

            // A deploy command typed in the chat box opens the editable JSON instead
            // of going straight to the agent (the JSON payload from the editor, which
            // starts with '{', is sent normally).
            if (!message.startsWith('{')) {
                const di = parseDeployIntent(message);
                if (di) {
                    if (typeof overrideText !== 'string') input.value = '';
                    addMessage('user', message);
                    showDeployForm(di.env, di.name, di.version);
                    return;
                }
            }

            addMessage('user', message);
            if (typeof overrideText !== 'string') input.value = '';

            const botMsg = addMessage('bot', '<span class="dots"><span></span><span></span><span></span></span>', true);

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
                // A turn may have raised/blocked a PRD PR — refresh the window banner.
                loadReleaseStatus();
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
            showCapabilities();
        }

        // Quick actions — what the agent can do. mode 'send' runs immediately;
        // otherwise the text is pre-filled so the user edits the image:tag first.
        const CAPABILITIES = [
            {icon:'fa-flask',             label:'Deploy to UAT',        desc:'deploy a Helm chart to UAT',                  form:'uat'},
            {icon:'fa-shield-halved',     label:'Deploy to PROD',       desc:'deploy a Helm chart to PROD',                  form:'prod'},
            {icon:'fa-eraser',            label:'Remove from release',  desc:'unstage a chart before it ships',             send:false, text:"remove <chart-name> from today's release"},
            {icon:'fa-calendar-day',      label:'Sync status',           desc:'are UAT and PRD in sync?',                    send:true,  text:'what is the current UAT vs PRD sync status?'},
            {icon:'fa-circle-check',      label:'Verify a build',       desc:'tag-gen step + RLFT controls for a tag',      send:false, text:'verify <image>:<tag> was built in <owner/repo>'},
            {icon:'fa-list-check',        label:'Check PRD controls',   desc:'pass/fail RLFT/RFTL gates for a tag',         send:false, text:'check build controls for <image>:<tag> before a PRD release'},
            {icon:'fa-images',            label:'List allowed images',  desc:'what I can promote',                          send:true,  text:'what images can I promote?'},
            {icon:'fa-clock-rotate-left', label:'Recent workflow runs', desc:'status of the latest runs',                   send:true,  text:'show me the 5 most recent workflow runs and their status'},
            {icon:'fa-code-pull-request', label:'Track a PR',           desc:'find the PR & summarize CHG/RMG/RLFT',         send:false, text:'find the deployment PR for <image>:<tag> and summarize its CHG, RMG and RLFT controls'},
            {icon:'fa-rotate',            label:'Re-run a step',        desc:'re-run apply or dispatch',                    send:false, text:'re-run dispatch_workflow'},
        ];

        function runQuick(text, send) {
            if (send) {
                sendMessage(text);   // send directly so multi-line messages keep their newlines
                return;
            }
            const input = document.getElementById('input');
            input.value = text;
            input.focus();
            try { input.setSelectionRange(text.length, text.length); } catch (e) {}
        }

        function showCapabilities() {
            const chat = document.getElementById('chat');
            const wrap = document.createElement('div');
            wrap.className = 'message bot rounded-2xl p-4 text-sm';

            const title = document.createElement('div');
            title.className = 'mb-2 text-slate-300 font-semibold';
            title.textContent = 'What I can do — pick one to start:';
            wrap.appendChild(title);

            const grid = document.createElement('div');
            grid.className = 'grid grid-cols-1 sm:grid-cols-2 gap-2';
            CAPABILITIES.forEach(c => {
                const btn = document.createElement('button');
                btn.className = 'text-left bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-xl px-3 py-2 flex items-start gap-2';
                btn.innerHTML = '<i class="fa-solid ' + c.icon + ' text-emerald-400 mt-1"></i>' +
                    '<span><span class="font-medium">' + c.label + '</span><br>' +
                    '<span class="text-[11px] text-slate-400">' + c.desc + '</span></span>';
                btn.addEventListener('click', () => c.form ? showDeployForm(c.form) : runQuick(c.text, c.send));
                grid.appendChild(btn);
            });
            wrap.appendChild(grid);

            const note = document.createElement('div');
            note.className = 'text-[10px] text-slate-500 mt-2';
            note.textContent = 'Deploy opens an editable JSON entry; some actions run immediately; others pre-fill the box so you can edit, then Send.';
            wrap.appendChild(note);

            chat.appendChild(wrap);
            chat.scrollTop = chat.scrollHeight;
        }

        // Deploy form — three inputs: chart name, version, namespace.
        // On submit sends a JSON string through the normal /api/chat SSE flow.
        // The backend parses the JSON, assembles the Helm entry, previews it,
        // and replies with a CONFIRM-XXXXXX token; the existing interrupt UI
        // then handles the confirmation step unchanged.
        // Deploy editor — shows the FULL deployment.json entry as editable JSON
        // (pre-filled from /api/deploy-template: constants from config, name/version
        // from the dev). On submit it sends {environment, ...entry} through /api/chat;
        // the backend previews the exact JSON it will write and asks to confirm.
        async function showDeployForm(env, name, version) {
            const isProd = env === 'prod';
            const accentT = isProd ? 'text-amber-300' : 'text-emerald-300';
            const accentBtn = isProd ? 'bg-amber-600 hover:bg-amber-500' : 'bg-emerald-600 hover:bg-emerald-500';
            const icon = isProd ? 'fa-shield-halved' : 'fa-flask';
            const heading = isProd ? 'Deploy to PROD' : 'Deploy to UAT';

            // Pre-fill the WHOLE deployment.json file ({"include":[...]}) from the
            // backend — add more entries to deploy multiple charts at once.
            let fileDoc = { include: [ { helm_chart_name: name || '', helm_chart_version: version || '' } ] };
            try {
                const qs = new URLSearchParams({ env: env, name: name || '', version: version || '' });
                const r = await fetch(API_BASE + '/api/deploy-template?' + qs.toString());
                if (r.ok) { const d = await r.json(); fileDoc = d.deployment; }
            } catch (e) {}

            const chat = document.getElementById('chat');
            const wrap = document.createElement('div');
            wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm';

            const title = document.createElement('div');
            title.className = 'mb-2 font-semibold flex items-center gap-2 ' + accentT;
            title.innerHTML = '<i class="fa-solid ' + icon + '"></i> ' + heading +
                ' <span class="text-slate-400 font-normal text-xs">— edit the deployment.json (add entries to include[] for multiple charts); submit OVERWRITES the file</span>';
            wrap.appendChild(title);

            const taId = 'deploy-json-' + env;
            const ta = document.createElement('textarea');
            ta.id = taId;
            ta.rows = 12;
            ta.spellcheck = false;
            ta.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-xs font-mono text-white focus:outline-none mb-2';
            ta.value = JSON.stringify(fileDoc, null, 2);
            wrap.appendChild(ta);

            const row = document.createElement('div');
            row.className = 'flex items-center gap-3 mt-1';
            const submit = document.createElement('button');
            submit.className = accentBtn + ' px-4 py-1.5 rounded-lg text-sm font-medium';
            submit.textContent = heading;
            const err = document.createElement('span');
            err.className = 'text-[11px] text-red-400';

            submit.addEventListener('click', () => {
                err.textContent = '';
                const parsed = parseDeployInclude(document.getElementById(taId).value);
                if (!parsed || !parsed.include.length) {
                    err.textContent = 'Could not find any chart entries — each needs helm_chart_name + helm_chart_version.';
                    return;
                }
                for (const it of parsed.include) {
                    if (!it || !it.helm_chart_name || !it.helm_chart_version) {
                        err.textContent = 'Each entry needs a non-empty helm_chart_name + helm_chart_version.';
                        return;
                    }
                }
                // Re-render the normalized JSON so the user sees exactly what we parsed
                // (commas added / wrapped into include[] when they left them out).
                document.getElementById(taId).value = JSON.stringify({ include: parsed.include }, null, 2);
                sendMessage(JSON.stringify({ environment: env, include: parsed.include }));
            });
            row.appendChild(submit);
            row.appendChild(err);
            wrap.appendChild(row);

            chat.appendChild(wrap);
            chat.scrollTop = chat.scrollHeight;
        }

        // Detect a deploy command typed in the chat box so we can pop the editable
        // JSON instead of sending it straight to the agent. Needs a deploy verb, a
        // target env, and a <name>:<version> token.
        // Regex-free tokenizers (mirror the Python no-regex parsing style).
        function _isAlnum(ch) {
            return (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9');
        }
        function _wordSet(text) {            // lowercased alphanumeric words
            const words = new Set();
            let cur = '';
            for (const ch of text) {
                if (_isAlnum(ch)) { cur += ch.toLowerCase(); }
                else { if (cur) words.add(cur); cur = ''; }
            }
            if (cur) words.add(cur);
            return words;
        }
        function _wsTokens(text) {           // whitespace-separated raw tokens
            const out = [];
            let cur = '';
            for (const ch of text) {
                if (ch === ' ' || ch === '\\t' || ch === '\\n' || ch === '\\r') { if (cur) out.push(cur); cur = ''; }
                else { cur += ch; }
            }
            if (cur) out.push(cur);
            return out;
        }
        function parseDeployIntent(text) {
            const w = _wordSet(text);
            const hasVerb = w.has('deploy') || w.has('promote') || w.has('ship') ||
                            w.has('rollout') || (w.has('roll') && w.has('out'));
            if (!hasVerb) return null;
            const env = (w.has('prod') || w.has('prd') || w.has('production')) ? 'prod'
                      : (w.has('uat') ? 'uat' : null);
            if (!env) return null;
            // Find a <name>:<version> (or name=version) token without regex.
            for (const tok of _wsTokens(text)) {
                let i = tok.indexOf(':');
                if (i === -1) i = tok.indexOf('=');
                if (i <= 0) continue;
                const name = tok.slice(0, i);
                let version = tok.slice(i + 1);
                while (version && '.,;:)'.indexOf(version[version.length - 1]) !== -1) version = version.slice(0, -1);
                const c = name[0];
                if (((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')) && version) {
                    return { env: env, name: name, version: version };
                }
            }
            return null;
        }

        // Tolerant deploy-JSON parser. Accepts a clean {"include":[...]}, a bare array,
        // or a single entry; if strict JSON.parse fails (e.g. the user pasted objects
        // with no commas and no include[] wrapper), it brace-scans every balanced {...}
        // and keeps the chart-entry-shaped ones. Returns {include, recovered} or null.
        function _extractJsonObjects(text) {
            const out = [];
            for (let i = 0; i < text.length; i++) {
                if (text[i] !== '{') continue;
                let depth = 0, inStr = false, esc = false, end = -1;
                for (let j = i; j < text.length; j++) {
                    const ch = text[j];
                    if (inStr) { if (esc) esc = false; else if (ch === '\\\\') esc = true; else if (ch === '"') inStr = false; continue; }
                    if (ch === '"') inStr = true;
                    else if (ch === '{') depth++;
                    else if (ch === '}') { depth--; if (depth === 0) { end = j; break; } }
                }
                if (end === -1) break;
                try {
                    const e = JSON.parse(text.slice(i, end + 1));
                    if (e && typeof e === 'object' && !Array.isArray(e) &&
                        (e.helm_chart_name !== undefined || e.helm_chart_version !== undefined)) {
                        out.push(e);
                    }
                } catch (_) { /* this {...} isn't a standalone object — skip */ }
            }
            return out;
        }
        function parseDeployInclude(text) {
            text = (text || '').trim();
            try {
                const doc = JSON.parse(text);
                if (Array.isArray(doc)) return { include: doc, recovered: false };
                if (doc && Array.isArray(doc.include)) return { include: doc.include, recovered: false };
                if (doc && typeof doc === 'object' && doc.helm_chart_name !== undefined) return { include: [doc], recovered: false };
            } catch (_) { /* fall through to lenient recovery */ }
            const entries = _extractJsonObjects(text);
            return entries.length ? { include: entries, recovered: true } : null;
        }

        // Release status panel — reads the new Helm-chart-based API shape:
        // { date_utc, now_utc, uat_charts, prd_charts, pending, in_sync, reason }
        async function loadReleaseStatus() {
            const banner = document.getElementById('release-banner');
            const icon   = document.getElementById('rb-icon');
            const title  = document.getElementById('rb-title');
            const detail = document.getElementById('rb-detail');
            try {
                const res = await fetch(API_BASE + '/api/release-status');
                const s = await res.json();
                banner.classList.remove('hidden');
                // reset classes; add colour below based on state
                banner.className = 'mb-4 rounded-2xl border px-4 py-3 text-sm';
                if (s.error) {
                    banner.classList.add('border-slate-700', 'bg-slate-900');
                    icon.className = 'fa-solid fa-triangle-exclamation text-slate-400';
                    title.textContent = "Couldn't fetch release status";
                    detail.textContent = s.error;
                    return;
                }
                const pending = Array.isArray(s.pending) ? s.pending : [];
                const foot = `UTC ${s.now_utc} • ${s.date_utc}`;
                if (s.in_sync) {
                    banner.classList.add('border-emerald-600/50', 'bg-emerald-500/10');
                    icon.className = 'fa-solid fa-circle-check text-emerald-400';
                    title.textContent = '✅ UAT in sync with PRD';
                    detail.textContent = (s.reason ? s.reason + ' • ' : '') + foot;
                } else {
                    const n = pending.length;
                    banner.classList.add('border-amber-600/50', 'bg-amber-500/10');
                    icon.className = 'fa-solid fa-rocket text-amber-400';
                    title.textContent = '🚀 ' + n + ' chart' + (n === 1 ? '' : 's') + ' pending to PRD';
                    let detailHtml = (s.reason ? s.reason + ' • ' : '') + foot;
                    if (n > 0) {
                        detailHtml += '<br><span class="text-slate-400">';
                        detailHtml += pending.map(function(p) {
                            return p.helm_chart_name + ' ' + p.uat_version
                                + ' → ' + (p.prd_version || 'not deployed');
                        }).join(' &nbsp;│&nbsp; ');
                        detailHtml += '</span>';
                    }
                    detail.innerHTML = detailHtml;
                    return;   // detail already set via innerHTML; skip the textContent line below
                }
            } catch (e) {
                banner.classList.remove('hidden');
                icon.className = 'fa-solid fa-triangle-exclamation text-slate-400';
                title.textContent = "Couldn't reach the release-status endpoint";
                detail.textContent = String(e);
            }
        }

        // Welcome message
        window.onload = () => {
            const chat = document.getElementById('chat');
            if (chat.children.length === 0) {
                addMessage('bot', 'Hello! I can help you deploy Helm charts and manage release workflows.');
                showCapabilities();
            }
            loadReleaseStatus();
            // Keep it fresh so a release raised in another session shows up here.
            setInterval(loadReleaseStatus, 60000);
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
    """Return the full deployment.json entry to pre-fill the UI's editable JSON box.
    Constants (helm_chart_dir, env values-file, namespace) come from config; the dev
    fills helm_chart_name / helm_chart_version (passed through if provided)."""
    from .tools.gh_tools import assemble_entry

    e = "prod" if str(env).lower() in ("prod", "prd", "production") else "uat"
    entry = assemble_entry(name or "", version or "", e)
    # Return the FULL file the editor shows ({"include":[...]}); add more entries to
    # deploy multiple charts at once. Submit OVERRIDES the file with this content.
    return {"environment": e, "deployment": {"include": [entry]}}


@app.get("/health")
async def health():
    """Health check endpoint for Kubernetes liveness/readiness probes."""
    return {
        "status": "ok",
        "service": "release-copilot-fastapi",
        "build_repo": settings.build_repo,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    print(f"Starting Release Copilot FastAPI on http://localhost:{port}")
    uvicorn.run("src.release_agent.app_fastapi:app", host="0.0.0.0", port=port, reload=True)
