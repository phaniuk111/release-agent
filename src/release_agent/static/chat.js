// Chat core: markdown rendering, message DOM, the /api/chat SSE stream, and
// the confirmation/approval replies. Everything here is rendering + transport;
// all decisions live server-side.
import { API_BASE, getThreadId, rotateThreadId } from './state.js';
import { parseDeployIntent, showDeployForm } from './forms.js';
import { renderConnectionStatus } from './connect.js';
import { showCapabilities } from './palette.js';
import { loadReleaseStatus } from './status.js';

// Minimal, safe markdown -> HTML for streamed assistant text.
export function renderMarkdown(t) {
    t = t.split('&').join('&amp;').split('<').join('&lt;').split('>').join('&gt;');
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
    t = t.split('\n').join('<br>');
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

        function handleEvent(rawEvent) {
            for (const line of rawEvent.split('\n')) {
                if (!line.startsWith('data: ')) continue;
                try {
                    const data = JSON.parse(line.slice(6));
                    if (data.type === 'token') {
                        fullText += (fullText ? '\n\n' : '') + data.content;
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
