import { escapeHtml as esc } from '../core/format.js';
import {
    ENABLE_HINT, actionLabel, askPrompt, emptyText, jobHref, metaLine, summaryLine,
} from '../core/support_triage.js';
import { supportTriage } from '../api.js';
import { llmEnabled } from '../core/capabilities.js';
import { sendMessage } from '../chat.js';
import { opening, withDismiss } from './common.js';

// ---- Support triage (L1) -----------------------------------------------------
// The control table's problems for one business date as an L1 work list, most
// urgent first: each incident says what happened, whether it is a known issue,
// what to do now and who owns it, with a ticket note to copy. The server
// decides all of that (tools/support_triage.py); this renders it.
//
// Every value comes from the control table — error text, unit names — so all
// of it goes through esc(), title attributes included.

export async function showSupportTriage() {
    const _ph = opening('Support triage');
    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm queue-table-card';
    _ph.replaceWith(wrap);
    await render(wrap, '', false);
    withDismiss(wrap);
    chat.scrollTop = chat.scrollHeight;
}

const PRIORITY_CLASS = {
    high: 'bg-red-600 text-white',
    medium: 'bg-amber-600 text-white',
    low: 'bg-slate-600 text-slate-100',
};
const ACTION_CLASS = {
    wait: 'text-sky-300',
    retrigger: 'text-emerald-300',
    check: 'text-amber-300',
    escalate: 'text-red-300',
};

function jobLinks(ids, template) {
    return (ids || []).map(id => {
        const href = jobHref(template, id);
        return href
            ? '<a href="' + esc(href) + '" target="_blank" rel="noopener" class="text-sky-400 hover:underline font-mono">' + esc(id) + '</a>'
            : '<span class="font-mono">' + esc(id) + '</span>';
    }).join(', ');
}

function incidentHtml(inc, i, report) {
    const pri = PRIORITY_CLASS[inc.priority] || PRIORITY_CLASS.low;
    const act = ACTION_CLASS[inc.action] || ACTION_CLASS.check;
    const facts = (inc.facts || []).map(f => '<div>' + esc(f) + '</div>').join('');
    const steps = (inc.steps || []).map(s => '<li>' + esc(s) + '</li>').join('');
    const error = inc.error_text
        ? '<div class="font-mono text-[10px] text-slate-400 mt-1 truncate" style="max-width:48rem" title="' +
          esc(inc.error_text) + '">' + esc(inc.error_text) + '</div>' : '';
    const jobs = (inc.job_ids || []).length
        ? '<div class="text-[10px] text-slate-500 mt-1">jobs: ' + jobLinks(inc.job_ids, report.job_url) + '</div>' : '';
    return '<div class="rounded-xl border border-slate-700 bg-slate-900/60 p-3 mb-2">' +
        '<div class="flex items-center gap-2 flex-wrap">' +
        '<span class="rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ' + pri + '">' + esc(inc.priority || 'low') + '</span>' +
        '<span class="font-semibold text-slate-100">' + esc(inc.title || '') + '</span>' +
        (inc.priority_reason ? '<span class="text-[10px] text-slate-500">' + esc(inc.priority_reason) + '</span>' : '') +
        '<span class="flex-1"></span>' +
        '<span class="text-[11px] font-semibold ' + act + '"><i class="fa-solid fa-circle-arrow-right mr-1"></i>' +
        esc(actionLabel(inc.action)) + '</span></div>' +
        '<div class="text-[11px] text-slate-300 mt-1">' + facts + '</div>' + error + jobs +
        '<div class="text-[11px] mt-2"><span class="text-slate-500">Known issue:</span> ' +
        (inc.runbook ? '<span class="text-emerald-300">' + esc(inc.runbook) + '</span>'
            : '<span class="text-slate-400">no runbook match</span>') + '</div>' +
        '<ol class="text-[11px] text-slate-200 mt-1 pl-4" style="list-style:decimal">' + steps + '</ol>' +
        '<div class="flex items-center gap-3 mt-2 text-[11px]">' +
        '<span><span class="text-slate-500">Owner:</span> <span class="text-slate-200">' + esc(inc.owner || '') + '</span></span>' +
        '<span class="flex-1"></span>' +
        '<button type="button" data-copy="' + i + '" class="text-sky-400 hover:underline whitespace-nowrap">' +
        '<i class="fa-solid fa-copy mr-1"></i>Copy ticket note</button>' +
        (llmEnabled(window.PORTAL_UI)
            ? '<button type="button" data-ask="' + i + '" class="text-sky-400 hover:underline whitespace-nowrap">' +
              '<i class="fa-solid fa-comment-dots mr-1"></i>Ask why</button>' : '') +
        '</div></div>';
}

async function copyText(text, btn) {
    try {
        await navigator.clipboard.writeText(text);
    } catch (e) {
        // No clipboard permission (plain http, an old browser): select it instead.
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

async function render(wrap, date, fresh) {
    wrap.querySelectorAll('.st-body').forEach(n => n.remove());
    const body = document.createElement('div');
    body.className = 'st-body';
    body.innerHTML = '<div class="text-[11px] text-slate-500"><span class="dots"><span></span><span></span>' +
        '<span></span></span> Reading the control table…</div>';
    wrap.appendChild(body);

    let res;
    try { res = await supportTriage(date, fresh); } catch (e) { res = { ok: false, error: String((e && e.message) || e) }; }
    if (!res || typeof res !== 'object') res = { ok: false, error: 'no answer' };
    const incidents = Array.isArray(res.incidents) ? res.incidents : [];

    let html =
        '<div class="mb-1 flex items-center gap-2 pr-6 flex-wrap">' +
        '<span class="font-semibold text-amber-300"><i class="fa-solid fa-headset mr-1"></i>Support triage</span>' +
        '<span class="text-[11px] text-slate-400">' + esc(summaryLine(res)) + '</span>' +
        '<span class="flex-1"></span>' +
        '<input type="date" data-st="date" value="' + esc(date || res.business_date || '') + '" ' +
        'class="bg-slate-800 border border-slate-700 rounded px-1.5 py-0.5 text-[11px] text-slate-200" ' +
        'title="Business date" aria-label="Business date">' +
        '<button type="button" data-st="refresh" title="Read again now" aria-label="Read again now" ' +
        'class="text-slate-400 hover:text-white text-xs"><i class="fa-solid fa-rotate-right"></i></button></div>' +
        '<div class="text-[10px] text-slate-500 mb-2">' + esc(metaLine(res)) + '</div>';
    if (res.disabled) {
        html += '<div class="text-[11px] text-amber-400">' + esc(ENABLE_HINT) + '</div>';
    } else if (res.ok === false) {
        html += '<div class="text-[11px] text-amber-400">' + esc(res.error || 'no answer') + '</div>';
        if (res.hint) html += '<div class="text-[11px] text-amber-300/80 mt-1">' + esc(res.hint) + '</div>';
    } else if (!incidents.length) {
        html += '<div class="text-[11px] text-emerald-300"><i class="fa-solid fa-circle-check mr-1"></i>' +
            esc(emptyText(res)) + '</div>';
    } else {
        html += incidents.map((inc, i) => incidentHtml(inc && typeof inc === 'object' ? inc : {}, i, res)).join('');
    }
    (res.notes || []).forEach(n => {
        html += '<div class="text-[10px] text-slate-500 mt-1"><i class="fa-solid fa-circle-info mr-1"></i>' + esc(n) + '</div>';
    });
    body.innerHTML = html;

    const dateInput = body.querySelector('[data-st="date"]');
    dateInput.addEventListener('change', () => render(wrap, dateInput.value, false));
    body.querySelector('[data-st="refresh"]').addEventListener('click', () => render(wrap, dateInput.value, true));
    body.querySelectorAll('button[data-copy]').forEach(btn => {
        btn.addEventListener('click', () => copyText(String(incidents[+btn.dataset.copy].note || ''), btn));
    });
    body.querySelectorAll('button[data-ask]').forEach(btn => {
        btn.addEventListener('click', () => sendMessage(askPrompt(incidents[+btn.dataset.ask], res)));
    });
}
