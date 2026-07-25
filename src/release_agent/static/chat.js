// Chat core: markdown rendering, message DOM, the /api/chat SSE stream, and
// the confirmation/approval replies. Everything here is rendering + transport;
// all decisions live server-side.
import { API_BASE, getThreadId, rotateThreadId } from './state.js';
import { parseDeployIntent, showDeployForm } from './forms.js';
import { renderConnectionStatus } from './connect.js';
import { showCapabilities } from './palette.js';
import { loadReleaseStatus } from './status.js';

// --- chart-spec blocks ------------------------------------------------------
// The agent answers data questions with a fenced ```chart block carrying
// {type, title?, labels: [...], series: [{label, data, color?}...]}. The block
// is swapped for a <canvas> and drawn with the VENDORED Chart.js (bundled in
// static/vendor — no CDN, works behind the corporate proxy). The LLM only
// picks the spec; all drawing is deterministic code here, and a malformed
// spec falls back to plain text — never a broken bubble.
const _chartSpecs = new Map();
let _chartSeq = 0;
const CHART_COLORS = ['#34d399', '#38bdf8', '#a78bfa', '#fbbf24', '#f87171', '#4ade80'];

function _extractChartBlocks(t) {
    // Complete blocks only — while streaming, an unterminated block stays text.
    let out = '', idx = 0;
    while (true) {
        const start = t.indexOf('```chart', idx);
        if (start === -1) { out += t.slice(idx); break; }
        const bodyStart = t.indexOf('\n', start);
        const end = bodyStart === -1 ? -1 : t.indexOf('```', bodyStart);
        if (end === -1) { out += t.slice(idx); break; }
        out += t.slice(idx, start);
        try {
            const spec = JSON.parse(t.slice(bodyStart + 1, end));
            const id = 'chat-chart-' + (++_chartSeq);
            _chartSpecs.set(id, spec);
            out += '\nCHARTSLOT' + id + 'ENDCHART\n';
        } catch (e) {
            out += t.slice(start, end + 3);
        }
        idx = end + 3;
    }
    return out;
}

export function renderCharts(root) {
    if (typeof Chart === 'undefined') return;
    (root || document).querySelectorAll('canvas.chat-chart:not([data-rendered])').forEach(cv => {
        const spec = _chartSpecs.get(cv.id);
        if (!spec) return;
        cv.dataset.rendered = '1';
        const type = String(spec.type || 'bar').toLowerCase();
        const horizontal = type === 'hbar';
        const circular = type === 'pie' || type === 'doughnut';
        const datasets = (spec.series || []).map((s, i) => {
            const base = s.color || CHART_COLORS[i % CHART_COLORS.length];
            return {
                label: s.label || '',
                data: s.data || [],
                backgroundColor: circular
                    ? (s.data || []).map((_, j) => CHART_COLORS[j % CHART_COLORS.length] + 'cc')
                    : base + (type === 'line' ? '22' : 'cc'),
                borderColor: base,
                borderWidth: type === 'line' ? 2 : 0,
                borderRadius: 4,
                fill: type === 'line',
                tension: 0.25,
                pointRadius: 3,
            };
        });
        try {
            new Chart(cv, {
                type: horizontal ? 'bar' : (circular ? type : (type === 'line' ? 'line' : 'bar')),
                data: { labels: spec.labels || [], datasets },
                options: {
                    indexAxis: horizontal ? 'y' : 'x',
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            display: circular || (spec.series || []).length > 1,
                            labels: { color: '#94a3b8', boxWidth: 10, font: { size: 10 } },
                        },
                        title: spec.title
                            ? { display: true, text: spec.title, color: '#94a3b8', font: { size: 11 } }
                            : { display: false },
                    },
                    scales: circular ? {} : {
                        x: { ticks: { color: '#94a3b8', font: { size: 10 } },
                             grid: { color: 'rgba(148,163,184,.08)' } },
                        y: { ticks: { color: '#94a3b8', font: { size: 10 } },
                             grid: { color: 'rgba(148,163,184,.08)' } },
                    },
                },
            });
        } catch (e) { console.error('chart render failed', e); }
    });
}

// Markdown pipe tables -> styled HTML tables (emitted as one line so the later
// \n -> <br> pass can't inject breaks inside the markup).
function _renderTables(t) {
    const isRow = (s) => { s = s.trim(); return s.startsWith('|') && s.endsWith('|') && s.length > 2; };
    const isSep = (s) => isRow(s) && /^[|\s:-]+$/.test(s.trim());
    const cells = (s) => s.trim().slice(1, -1).split('|').map(c => c.trim());
    const lines = t.split('\n');
    const out = [];
    for (let i = 0; i < lines.length; i++) {
        if (isRow(lines[i]) && i + 1 < lines.length && isSep(lines[i + 1])) {
            const head = cells(lines[i]);
            let j = i + 2;
            const rows = [];
            while (j < lines.length && isRow(lines[j]) && !isSep(lines[j])) { rows.push(cells(lines[j])); j++; }
            let html = '<div class="overflow-x-auto my-2"><table class="text-xs font-mono w-full border-collapse">';
            html += '<thead><tr>' + head.map(h =>
                '<th class="text-left text-[10px] uppercase tracking-wide text-slate-500 border-b border-slate-700 px-2 py-1">' + h + '</th>').join('') + '</tr></thead><tbody>';
            rows.forEach(r => {
                html += '<tr>' + r.map(c =>
                    '<td class="px-2 py-1 border-b border-slate-800 text-slate-300">' + c + '</td>').join('') + '</tr>';
            });
            html += '</tbody></table></div>';
            out.push(html);
            i = j - 1;
        } else {
            out.push(lines[i]);
        }
    }
    return out.join('\n');
}

// HTML-escape untrusted text before it enters innerHTML. Quotes MUST be escaped
// too: model output and tool results (PR titles, branch names, BQ notes) can
// carry a `"` that would otherwise break out of an attribute value — the
// indirect-prompt-injection path ADK's safety guidance calls out.
export function escapeHtml(value) {
    return String(value == null ? '' : value)
        .split('&').join('&amp;')
        .split('<').join('&lt;')
        .split('>').join('&gt;')
        .split('"').join('&quot;')
        .split("'").join('&#39;');
}

// Minimal, safe markdown -> HTML for streamed assistant text.
export function renderMarkdown(t) {
    t = _extractChartBlocks(t);
    t = escapeHtml(t);
    // [text](url) markdown links -> stash so the bare-URL linkifier below
    // doesn't double-wrap the URL inside the href attribute.
    const _links = [];
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function(m, txt, url) {
        _links.push('<a href="' + url + '" target="_blank" class="underline text-emerald-400">' + txt + '</a>');
        return 'LINKTOKEN' + (_links.length - 1) + 'ENDTOKEN';
    });
    t = t.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" class="underline text-emerald-400">$1</a>');
    t = t.replace(/LINKTOKEN(\d+)ENDTOKEN/g, function(m, i) { return _links[+i]; });
    t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/`([^`]+)`/g, '<code class="bg-slate-800 px-1 rounded text-emerald-300">$1</code>');
    t = _renderTables(t);
    // Chart placeholder -> canvas (post-markdown so the token survives escaping).
    t = t.replace(/CHARTSLOT(chat-chart-\d+)ENDCHART/g, function(m, id) {
        return '<div class="my-2 rounded-xl border border-slate-700/70 bg-slate-950/40 p-3" style="height:230px">' +
               '<canvas id="' + id + '" class="chat-chart"></canvas></div>';
    });
    // \n -> <br>, but never adjacent to block elements (tables/charts render
    // their own spacing; stray <br> around them doubles the gaps).
    t = t.split('\n').join('<br>');
    t = t.replace(/(<br>)+(<div)/g, '$2').replace(/(<\/div>)(<br>)+/g, '$1');
    return t;
}

export function addMessage(role, content, isStreaming = false) {
    const chat = document.getElementById('chat');
    const div = document.createElement('div');

    if (role === 'interrupt') {
        // content may be the full interrupt object (preferred) or a bare string.
        const intr = (content && typeof content === 'object') ? content : { message: content };
        const isBudget = intr.type === 'budget_confirmation';
        // A yes/no tool-approval (e.g. merge_prod_release): no CONFIRM token —
        // identified by the function name / the 'Reply "yes"' instruction. Render
        // Approve/Reject buttons; a pasted token here would otherwise reject it.
        const isApproval = !intr.token && !isBudget &&
            (!!intr.function || ((intr.action || intr.message || '').toLowerCase().includes('"yes"')));
        const header = isBudget ? 'Budget Confirmation'
            : (isApproval ? 'Approval Required' : 'Confirmation Required');
        const bodyText = renderMarkdown(intr.message || 'Please confirm this action.')
            + (intr.action && !isApproval ? ('<br><br>' + renderMarkdown(intr.action)) : '');
        const placeholder = isBudget
            ? 'Type yes to continue, anything else to stop'
            : 'Paste CONFIRM-XXXXXX here';
        const controls = isApproval ? `
            <div class="flex gap-2">
                <button onclick="sendApproval('yes')"
                        class="bg-emerald-600 hover:bg-emerald-500 px-4 py-1.5 rounded-lg text-sm font-medium">
                    <i class="fa-solid fa-check mr-1"></i>Approve
                </button>
                <button onclick="sendApproval('no')"
                        class="bg-slate-700 hover:bg-slate-600 px-4 py-1.5 rounded-lg text-sm font-medium">
                    <i class="fa-solid fa-xmark mr-1"></i>Reject
                </button>
            </div>
        ` : `
            <div class="flex gap-2">
                <input id="confirm-input" type="text" placeholder="${placeholder}"
                       class="flex-1 bg-slate-900 border border-amber-600 rounded-lg px-3 py-1.5 text-sm">
                <button onclick="sendConfirmation()"
                        class="bg-amber-600 hover:bg-amber-500 px-4 rounded-lg text-sm font-medium">
                    Confirm
                </button>
            </div>
        `;
        div.className = 'message mx-auto interrupt-box rounded-2xl p-4 text-sm';
        div.innerHTML = `
            <div class="flex items-center gap-2 mb-2 text-amber-400">
                <i class="fa-solid fa-exclamation-triangle"></i>
                <span class="font-semibold">${header}</span>
            </div>
            <div class="text-amber-200 mb-3">${bodyText}</div>
            ${controls}
        `;
    } else {
        div.className = `message ${role === 'user' ? 'ml-auto user' : 'bot'} rounded-2xl px-4 py-3 text-sm`;
        div.innerHTML = `<div class="${isStreaming ? 'streaming' : ''}">${content}</div>`;
    }

    chat.appendChild(div);
    renderCharts(div);
    chat.scrollTop = chat.scrollHeight;
    return div;
}

export function updateLastMessage(content) {
    const chat = document.getElementById('chat');
    const last = chat.lastElementChild;
    if (last) {
        const contentDiv = last.querySelector('div');
        if (contentDiv) contentDiv.innerHTML = content;
    }
}

export async function sendMessage(overrideText) {
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
            body: JSON.stringify({ message, thread_id: getThreadId() })
        });

        if (!res.ok) throw new Error(await res.text());

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let fullText = '';
        let isInterrupt = false;
        let buffer = '';
        const steps = [];                     // progress labels for this turn
        const chat = document.getElementById('chat');

        function handleEvent(rawEvent) {
            for (const line of rawEvent.split('\n')) {
                if (!line.startsWith('data: ')) continue;
                try {
                    const data = JSON.parse(line.slice(6));
                    if (data.type === 'progress') {
                        // Multi-tool turns can run for a minute; show what the
                        // agent is doing (steps accumulate, dimmed) instead of
                        // silent dots. Cleared as soon as real text arrives.
                        if (!fullText) {
                            steps.push(escapeHtml(data.content));
                            botMsg.querySelector('div').innerHTML =
                                '<div class="text-[11px] text-slate-500 space-y-0.5">' +
                                steps.map((s, i) => '<div>' +
                                    (i === steps.length - 1
                                        ? '<span class="dots"><span></span><span></span><span></span></span> '
                                        : '<i class="fa-solid fa-check text-emerald-500/70 mr-1"></i>') +
                                    s + '</div>').join('') + '</div>';
                            chat.scrollTop = chat.scrollHeight;
                        }
                    } else if (data.type === 'token') {
                        fullText += (fullText ? '\n\n' : '') + data.content;
                        botMsg.querySelector('div').innerHTML = renderMarkdown(fullText);
                        renderCharts(botMsg);
                    } else if (data.type === 'interrupt') {
                        isInterrupt = true;
                        // Keep any streamed preview text (deploy diff / release
                        // file-set / RCTL timeline) visible above the confirm box;
                        // only drop the message if it's still the typing dots.
                        if (fullText) {
                            botMsg.querySelector('div').classList.remove('streaming');
                        } else {
                            botMsg.remove();
                        }
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
            while ((sep = buffer.indexOf('\n\n')) !== -1) {
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

export function sendApproval(answer) {
    // Yes/no tool-approval buttons: remove the interrupt box, send the reply.
    const chat = document.getElementById('chat');
    const last = chat.lastElementChild;
    if (last) last.remove();
    sendMessage(answer);
}

export function sendConfirmation() {
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

export async function newThread() {
    // Drop the old thread's stored repo + PAT on the server, then rotate.
    try {
        await fetch(API_BASE + '/api/session/disconnect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ thread_id: getThreadId() })
        });
    } catch (e) {}
    const threadId = rotateThreadId();
    document.getElementById('thread-label').textContent = threadId;
    document.getElementById('chat').innerHTML = '';
    renderConnectionStatus({ connected: false });
    addMessage('bot', 'New conversation started. How can I help with releases?');
    showCapabilities();
}
