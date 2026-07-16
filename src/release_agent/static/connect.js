// GitHub PAT connection (per session). The PAT is sent once to the server and
// never stored in the browser; all GitHub actions in this session then run as
// that user against the server-configured repositories.
import { API_BASE, getThreadId } from './state.js';
import { addMessage } from './chat.js';

export function renderConnectionStatus(s) {
    const icon = document.getElementById('repo-chip-icon');
    const label = document.getElementById('repo-chip-label');
    if (!icon || !label) return;
    if (s && s.connected) {
        icon.className = 'fa-brands fa-github text-emerald-400';
        label.textContent = 'GitHub connected' + (s.token_preview ? (' (' + s.token_preview + ')') : '');
        label.className = 'text-emerald-300';
    } else {
        icon.className = 'fa-brands fa-github text-slate-400';
        label.textContent = 'Connect with GitHub';
        label.className = 'text-slate-300';
    }
}

export async function refreshConnectionStatus() {
    try {
        const r = await fetch(API_BASE + '/api/session/status?thread_id=' + encodeURIComponent(getThreadId()));
        if (r.ok) renderConnectionStatus(await r.json());
    } catch (e) {}
}

export function showConnectForm() {
    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm';
    wrap.innerHTML =
        '<div class="mb-2 font-semibold flex items-center gap-2 text-emerald-300">' +
        '<i class="fa-brands fa-github"></i> Connect with GitHub</div>' +
        '<div class="text-slate-400 text-xs mb-3">Provide your GitHub PAT — all GitHub actions this session run as you. ' +
        'The token stays in memory on the server, is never logged, and is dropped when you start a new thread.</div>';

    const box = document.createElement('div');
    box.className = 'mb-2';
    const l = document.createElement('label');
    l.className = 'text-[11px] text-slate-400 block mb-0.5';
    l.textContent = 'PAT token';
    const pat = document.createElement('input');
    pat.id = 'conn-pat'; pat.type = 'password';
    pat.placeholder = 'ghp_… (never stored in the browser)';
    pat.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
    box.appendChild(l); box.appendChild(pat);
    wrap.appendChild(box);

    const row = document.createElement('div');
    row.className = 'flex items-center gap-3 mt-1';
    const submit = document.createElement('button');
    submit.className = 'bg-emerald-600 hover:bg-emerald-500 px-4 py-1.5 rounded-lg text-sm font-medium';
    submit.textContent = 'Connect';
    const err = document.createElement('span');
    err.className = 'text-[11px] text-red-400';

    submit.addEventListener('click', async () => {
        err.textContent = '';
        const token = document.getElementById('conn-pat').value.trim();
        if (!token) { err.textContent = 'PAT token is required.'; return; }
        submit.disabled = true; submit.textContent = 'Connecting…';
        try {
            const r = await fetch(API_BASE + '/api/session/connect', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ thread_id: getThreadId(), pat_token: token })
            });
            const d = await r.json();
            if (!d.ok) { err.textContent = d.error || 'Could not connect.'; return; }
            renderConnectionStatus(d);
            wrap.remove();
            addMessage('bot', 'Connected to GitHub (token ' + (d.token_preview || 'set') +
                '). GitHub actions this session will run with your token.');
        } catch (e) {
            err.textContent = 'Network error: ' + e.message;
        } finally {
            submit.disabled = false; submit.textContent = 'Connect';
        }
    });
    row.appendChild(submit); row.appendChild(err);
    wrap.appendChild(row);
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
}
