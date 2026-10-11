import { escapeHtml as esc } from '../core/format.js';
import {
    asInvestigation, askPrompt, counts, groupFindings, hhmm, lastRunText, metaLine, rowsOf, statusActions, statusText,
    watcherHealth,
} from '../core/auto_triage.js';
import { getAutoTriage, setAutoTriageStatus } from '../api.js';
import { llmEnabled } from '../core/capabilities.js';
import { renderMarkdown, sendMessage } from '../chat.js';
import { opening, withDismiss } from './common.js';
import { renderInvestigationFeedback } from './support_feedback.js';

// ---- Auto-triage -----------------------------------------------------------
// What the portal found in the control table on its own: each new problem
// investigated as the Investigate button would, grouped by what a person must
// do. Read-only: an action is a proposal; a person resolves or dismisses, and
// the pass clears what left the table. The server decides every finding
// (tools/support/watch.py); this renders it — with LLM_ENABLED=false too, where
// the finding is the model-free one and "Ask why" is not offered.
//
// Every value comes from the control table or a model — error text, unit
// names, the answer — so all of it goes through esc() or renderMarkdown().

const POLL_MS = 30000;   // a running pass shows up here once it is done

const PRIORITY_CLASS = {
    high: 'bg-red-600 text-white',
    medium: 'bg-amber-600 text-white',
    low: 'bg-slate-600 text-slate-100',
};
// The vendored Tailwind has no darker red than red-600: critical says so inline.
const CRITICAL_STYLE = 'background:#7f1d1d;color:#fff';
const TONE_CLASS = { ok: 'text-emerald-300', warn: 'text-amber-300', error: 'text-red-300', off: 'text-slate-400' };
const BUTTON = 'rounded border border-slate-700 bg-slate-800 px-2 py-0.5 text-[11px] font-semibold hover:bg-slate-600 whitespace-nowrap';

/** Opens the card. `opts.date` picks a business date, `opts.incident` scrolls
 *  to and highlights one finding (where the bell's portal:support link lands). */
export async function showAutoTriage(opts) {
    const o = opts && typeof opts === 'object' ? opts : {};
    const _ph = opening('Auto-triage');
    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm queue-table-card';
    wrap.dataset.autoTriage = '1';
    _ph.replaceWith(wrap);
    const state = { date: String(o.date || ''), focus: String(o.incident || ''), busy: false };
    await render(wrap, state);
    withDismiss(wrap);
    chat.scrollTop = chat.scrollHeight;
    // Keep the card current while it is on the page; the timer ends with it.
    const timer = setInterval(() => {
        if (!document.body.contains(wrap)) { clearInterval(timer); return; }
        if (document.visibilityState === 'visible' && !state.busy) render(wrap, state);
    }, POLL_MS);
}

function badge(priority) {
    const p = String(priority || 'low');
    const cls = PRIORITY_CLASS[p];
    return '<span class="rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ' + (cls || '') + '"' +
        (cls ? '' : ' style="' + CRITICAL_STYLE + '"') + '>' + esc(p) + '</span>';
}

function tile(label, value, note, cls) {
    return '<div class="rounded-xl border border-slate-700 bg-slate-900/60 px-3 py-2 min-w-0">' +
        '<div class="text-[10px] uppercase tracking-wide text-slate-500">' + esc(label) + '</div>' +
        '<div class="text-xl font-semibold truncate ' + (cls || 'text-slate-100') + '">' + esc(value) + '</div>' +
        '<div class="text-[10px] text-slate-500 truncate" title="' + esc(note) + '">' + esc(note) + '</div></div>';
}

// The deployment's Dataflow job link (SUPPORT_JOB_URL, with {job_id}); set from
// each feed answer so a finding's jobs link to their console page.
let jobUrl = '';

// The rows behind the finding, on the card itself: how wide the incident is,
// the error text, the runs and their times, the jobs. Open while it is the
// incident the bell pointed at.
function rowsHtml(f, url, open) {
    const r = rowsOf(f, url);
    if (r.empty) return '';
    const n = Number(f.count) || r.runs.length;
    const runs = r.runs.map(x => '<span class="font-mono">' + esc(x.id) + '</span>' +
        (x.at ? ' <span class="text-slate-500">' + esc(x.at) + '</span>' : '')).join(' · ');
    const jobs = r.jobs.map(j => j.href
        ? '<a href="' + esc(j.href) + '" target="_blank" rel="noopener" class="text-sky-400 hover:underline font-mono">' + esc(j.id) + '</a>'
        : '<span class="font-mono">' + esc(j.id) + '</span>').join(', ');
    return '<details class="text-[11px] mt-1"' + (open ? ' open' : '') + '>' +
        '<summary class="cursor-pointer text-slate-500 hover:text-slate-300">Rows' + (n ? ' (' + n + ')' : '') + '</summary>' +
        '<div class="rounded-lg bg-slate-800 px-3 py-2 mt-1 text-slate-300">' +
        r.facts.map(x => '<div>' + esc(x) + '</div>').join('') +
        (r.error ? '<div class="font-mono text-[10px] text-slate-400 mt-1" style="white-space:pre-wrap">' + esc(r.error) + '</div>' : '') +
        (runs ? '<div class="mt-1"><span class="text-slate-500">runs:</span> ' + runs + '</div>' : '') +
        (jobs ? '<div class="mt-1"><span class="text-slate-500">jobs:</span> ' + jobs + '</div>' : '') +
        '</div></details>';
}

function timelineHtml(f) {
    const items = Array.isArray(f.timeline) ? f.timeline : [];
    if (!items.length) return '';
    return '<div class="text-slate-500 mt-2">Timeline</div><ul class="pl-4 mt-1" style="list-style:disc">' +
        items.map(t => '<li><span class="font-mono text-[10px]">' + esc(hhmm(t.time)) + '</span> ' +
            (t.source ? esc(t.source) + ': ' : '') + esc(t.text || '') + '</li>').join('') + '</ul>';
}

function findingHtml(f, i, focused) {
    const open = f.status === 'open';
    const since = f.investigated_at ? 'since ' + esc(hhmm(f.investigated_at)) : '';
    const investigated = f.model ? 'investigated (' + esc(f.model) + ', ' + esc(f.model_calls || 0) + ' call' +
        (Number(f.model_calls) === 1 ? '' : 's') + ')' : 'investigated without a model';
    const doNow = f.do_now || (f.action ? f.action + (f.action_why ? ' — ' + f.action_why : '') : '');
    const actions = statusActions(f.status).map(a =>
        '<button type="button" data-status="' + esc(a.status) + '" data-i="' + i + '" class="' + BUTTON +
        ' text-slate-200">' + esc(a.label) + '</button>').join('');
    // The vendored Tailwind has no amber border class: the focused row says so inline.
    return '<div class="rounded-xl border border-slate-700 p-3 mb-2 bg-slate-900/60' + (open ? '' : ' opacity-70') + '"' +
        (focused ? ' style="border-color:#fbbf24"' : '') + ' data-incident="' + esc(f.incident_id) + '">' +
        '<div class="flex items-center gap-2 flex-wrap">' + badge(f.priority) +
        '<span class="font-semibold text-slate-100">' + esc(f.title || f.incident_id) + '</span>' +
        '<span class="text-[10px] text-slate-500">' + [since, investigated].filter(Boolean).join(' · ') + '</span>' +
        '<span class="flex-1"></span>' +
        (open ? '' : '<span class="text-[11px] text-slate-400">' + esc(statusText(f)) + '</span>') +
        (f.notified ? '<span class="text-[10px] text-slate-500" title="The bell was rung for this finding"><i class="fa-solid fa-bell mr-1"></i>told</span>' : '') +
        '</div>' +
        '<div class="text-[11px] text-slate-400 mt-1">' + esc(metaLine(f)) + '</div>' +
        '<div class="text-[11px] mt-2"><span class="text-slate-500">Runbook:</span> ' +
        (f.runbook ? '<span class="text-emerald-300">' + esc(f.runbook) + '</span>'
            : '<span class="text-slate-400">no runbook match</span>') + '</div>' +
        '<div class="text-[11px]"><span class="text-slate-500">Finding:</span> <span class="text-slate-200">' + esc(f.cause || '') + '</span> ' +
        (f.established ? '<span class="text-emerald-300 text-[10px]">· established by evidence</span>'
            : '<span class="text-amber-300 text-[10px]">· not established</span>') +
        (f.fallback ? '<span class="text-slate-500 text-[10px]"> · without the model</span>' : '') + '</div>' +
        (doNow ? '<div class="text-[11px]"><span class="text-slate-500">Do now:</span> <span class="text-slate-200">' + esc(doNow) + '</span>' +
            '<span class="text-slate-500 text-[10px]"> (a proposal; nothing is changed for you)</span></div>' : '') +
        rowsHtml(f, jobUrl, focused) +
        '<details class="text-[11px] mt-1"><summary class="cursor-pointer text-slate-500 hover:text-slate-300">Details</summary>' +
        '<div class="rounded-lg bg-slate-800 px-3 py-2 mt-1 text-slate-300">' + timelineHtml(f) +
        (f.answer ? '<div class="text-slate-500 mt-2">The investigation</div><div class="mt-1">' + renderMarkdown(f.answer) + '</div>' : '') +
        (f.note ? '<div class="text-slate-500 mt-2">Ticket note <button type="button" data-copy="' + i + '" class="text-sky-400 hover:underline">' +
            '<i class="fa-solid fa-copy mr-1"></i>Copy</button></div>' +
            '<pre class="font-mono text-[10px] text-slate-400 mt-1" style="white-space:pre-wrap">' + esc(f.note) + '</pre>' : '') +
        '</div></details>' +
        '<div class="flex items-center gap-2 flex-wrap mt-2 text-[11px]">' +
        (llmEnabled(window.PORTAL_UI)
            ? '<button type="button" data-ask="' + i + '" class="text-sky-400 hover:underline whitespace-nowrap"><i class="fa-solid fa-comment-dots mr-1"></i>Ask why</button>' : '') +
        actions +
        '<button type="button" data-feedback-for="' + i + '" class="text-slate-400 hover:underline whitespace-nowrap">Was this right?</button>' +
        '</div><div data-feedback-slot="' + i + '"></div></div>';
}

async function copyText(text, btn) {
    try {
        await navigator.clipboard.writeText(text);
    } catch (e) {
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (e2) {}
        ta.remove();
    }
    const was = btn.innerHTML;
    btn.innerHTML = '<i class="fa-solid fa-check mr-1"></i>Copied';
    setTimeout(() => { btn.innerHTML = was; }, 1500);
}

function emptyText(res) {
    const s = res.status || {};
    if (s.configured === false) return 'Auto-triage is off: set SUPPORT_TABLE — it reads what Support triage reads.';
    if (s.last_run_at && res.business_date) return 'Nothing open — Auto-triage found no new problems for ' + res.business_date + '.';
    if (s.last_run_at) return 'Nothing open — Auto-triage found no new problems.';
    return 'Auto-triage has not run yet — it checks the control table every few minutes on its own.';
}

async function render(wrap, state) {
    let res;
    try { res = await getAutoTriage(state.date); } catch (e) { res = { ok: false, error: String((e && e.message) || e) }; }
    if (!res || typeof res !== 'object') res = { ok: false, error: 'no answer' };
    const findings = Array.isArray(res.findings) ? res.findings.filter(f => f && typeof f === 'object') : [];
    const status = res.status && typeof res.status === 'object' ? res.status : {};
    jobUrl = typeof res.job_url === 'string' ? res.job_url : '';
    if (!state.date && res.business_date) state.date = res.business_date;
    const health = watcherHealth(status);
    const n = counts(findings);
    const groups = groupFindings(findings);
    const running = !!status.running || state.busy;
    const index = new Map();
    findings.forEach((f, i) => index.set(i, f));

    let html =
        '<div class="mb-1 flex items-center gap-2 pr-6 flex-wrap">' +
        '<span class="font-semibold text-amber-300"><i class="fa-solid fa-eye mr-1"></i>Auto-triage</span>' +
        '<span class="text-[11px] text-slate-400">checks the control table on its own and investigates what is new' +
        (state.date ? ' — business date ' + esc(state.date) : '') + '</span>' +
        '<span class="flex-1"></span>' +
        (running ? '<span class="text-[11px] text-amber-300">Checking…</span>' : '') +
        '<button type="button" data-at="refresh" title="Read again now" aria-label="Read again now" class="text-slate-400 hover:text-white text-xs">' +
        '<i class="fa-solid fa-rotate-right"></i></button></div>' +
        '<div class="mb-2" style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px">' +
        tile('Auto-triage', health.text, health.detail, TONE_CLASS[health.tone]) +
        tile('Needs attention', String(n.attention), 'open, high or critical', n.attention ? 'text-amber-300' : 'text-slate-100') +
        tile('Open', String(n.open), lastRunText(status.last_summary)) +
        tile('Notifications', status.notifications ? 'On — the bell' : 'Off', 'findings kept in ' + (status.store || 'memory')) +
        '</div>';
    if (res.ok === false) {
        html += '<div class="text-[11px] text-amber-400">' + esc(res.error || 'The findings could not be read.') + '</div>';
        if (res.hint) html += '<div class="text-[11px] text-amber-300/80 mt-1">' + esc(res.hint) + '</div>';
    } else if (status.error) {
        html += '<div class="text-[11px] text-red-300 mb-2"><i class="fa-solid fa-triangle-exclamation mr-1"></i>Last pass failed: ' + esc(status.error) + '</div>';
    }
    if (res.ok !== false && !groups.length) {
        html += '<div class="text-[11px] text-slate-400"><i class="fa-solid fa-circle-check mr-1"></i>' + esc(emptyText(res)) + '</div>';
    }
    groups.forEach(g => {
        html += '<div class="text-[10px] uppercase tracking-wide text-slate-500 mt-2 mb-1">' + esc(g.label) + ' (' + g.items.length + ')</div>';
        g.items.forEach(f => { html += findingHtml(f, findings.indexOf(f), f.incident_id === state.focus); });
    });

    wrap.querySelectorAll('.at-body').forEach(el => el.remove());
    const body = document.createElement('div');
    body.className = 'at-body';
    body.innerHTML = html;
    wrap.appendChild(body);

    const reload = () => render(wrap, state);
    body.querySelector('[data-at="refresh"]').addEventListener('click', reload);
    body.querySelectorAll('button[data-status]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const f = index.get(+btn.dataset.i);
            if (!f || state.busy) return;
            state.busy = true;
            body.querySelectorAll('button[data-status]').forEach(b => { b.disabled = true; });
            let out;
            try {
                out = await setAutoTriageStatus({ business_date: f.business_date, incident_id: f.incident_id, status: btn.dataset.status });
            } catch (e) { out = { ok: false, error: String((e && e.message) || e) }; }
            state.busy = false;
            await reload();
            if (out && out.ok === false) {
                const note = document.createElement('div');
                note.className = 'text-[11px] text-amber-400 mt-1';
                note.textContent = out.error || 'The change was not saved.';
                wrap.querySelector('.at-body').appendChild(note);
            }
        });
    });
    body.querySelectorAll('button[data-ask]').forEach(btn => {
        btn.addEventListener('click', () => sendMessage(askPrompt(index.get(+btn.dataset.ask))));
    });
    body.querySelectorAll('button[data-copy]').forEach(btn => {
        btn.addEventListener('click', () => copyText(String((index.get(+btn.dataset.copy) || {}).note || ''), btn));
    });
    body.querySelectorAll('button[data-feedback-for]').forEach(btn => {
        btn.addEventListener('click', () => {
            const f = index.get(+btn.dataset.feedbackFor);
            const slot = body.querySelector('[data-feedback-slot="' + btn.dataset.feedbackFor + '"]');
            if (!f || !slot) return;
            renderInvestigationFeedback(slot, asInvestigation(f));
            btn.remove();
        });
    });
    if (state.focus) {
        const row = body.querySelector('[data-incident="' + state.focus.split('"').join('') + '"]');
        if (row && typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'center' });
    }
}
