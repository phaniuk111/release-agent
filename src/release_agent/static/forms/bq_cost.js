import { escapeHtml as esc } from '../core/format.js';
import {
    ENABLE_HINT, costSummary, emptyText, explainPrompt, extrasText, formatBytes, formatCount, formatGb, formatUsd,
    insightText, orderShapes, reportMeta, whoText,
} from '../core/bq_cost.js';
import { bqCostReport, bqCostReportXlsx } from '../api.js';
import { sendMessage } from '../chat.js';
import { opening, withDismiss } from './common.js';

// ---- BigQuery cost -----------------------------------------------------------
// One table: the most expensive query shapes, straight from /api/bq-cost/report
// (tools/bq_cost.py measures — INFORMATION_SCHEMA reads, never a row of data).
// "Ask why" hands a row to the chat, which investigates and proves any rewrite
// with the bq_* tools; the full report (storage, writes, history) is the Excel
// download, built server-side from the same cached scan.
//
// Every value is BigQuery job metadata — SQL previews, user emails — so all of
// it goes through esc(), title attributes included.

export async function showBqCost() {
    const _ph = opening('BigQuery cost');
    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm queue-table-card';
    _ph.replaceWith(wrap);
    await render(wrap, false);
    withDismiss(wrap);
    chat.scrollTop = chat.scrollHeight;
}

const th = (t, cls) => '<th class="font-medium px-2 py-1 whitespace-nowrap ' + (cls || 'text-left') + '">' + t + '</th>';
const td = (t, cls) => '<td class="px-2 py-1 ' + (cls || '') + '">' + t + '</td>';

function rowHtml(s, i) {
    const users = (Array.isArray(s.users) ? s.users : []).filter(Boolean).map(String);
    const preview = String(s.sql_preview || '');
    const insight = insightText(s.insights);
    return '<tr class="border-b border-slate-800 align-top">' +
        td(i + 1, 'text-right text-slate-500') +
        td('<span title="' + esc(users.join(', ') || s.who || '') + '">' + esc(whoText(s)) + '</span>', 'text-slate-200 whitespace-nowrap') +
        td(esc(formatCount(s.runs)), 'text-right') +
        td(esc(formatGb(s.gb_billed)), 'text-right whitespace-nowrap') +
        td(esc(formatUsd(s.approx_usd)), 'text-right whitespace-nowrap') +
        td(esc(s.p50_bytes != null ? formatBytes(s.p50_bytes) : formatGb(s.p50_gb)), 'text-right whitespace-nowrap') +
        td(insight ? '<span class="text-amber-400">' + esc(insight) + '</span>' : '') +
        // The preview is 180 characters of someone's SQL: one truncated line, the whole of it on hover.
        td('<span class="font-mono text-[10px] text-slate-400 block truncate" style="max-width:26rem" title="' +
            esc(preview) + '">' + esc(preview) + '</span>') +
        td('<button type="button" data-ask="' + i + '" class="text-sky-400 hover:underline whitespace-nowrap">' +
            '<i class="fa-solid fa-comment-dots mr-1"></i>Ask why</button>', 'text-right') +
        '</tr>';
}

async function render(wrap, fresh) {
    wrap.querySelectorAll('.bqc-body').forEach(n => n.remove());
    const body = document.createElement('div');
    body.className = 'bqc-body';
    body.innerHTML = '<div class="text-[11px] text-slate-500"><span class="dots"><span></span><span></span>' +
        '<span></span></span> Reading INFORMATION_SCHEMA…</div>';
    wrap.appendChild(body);

    let res;
    try { res = await bqCostReport(fresh); } catch (e) { res = { ok: false, error: String((e && e.message) || e) }; }
    if (!res || typeof res !== 'object') res = { ok: false, error: 'no answer' };
    const shapes = orderShapes(res.shapes, res.billing);
    const extras = extrasText(res);

    let html =
        '<div class="mb-1 flex items-center gap-2 pr-6 flex-wrap">' +
        '<span class="font-semibold text-emerald-300"><i class="fa-solid fa-coins mr-1"></i>BigQuery cost report</span>' +
        '<span class="text-[11px] text-slate-400">' + esc(costSummary(res)) + '</span>' +
        '<span class="flex-1"></span>' +
        (res.ok === false ? '' :
            '<a href="' + esc(bqCostReportXlsx()) + '" download class="text-[11px] text-emerald-300 hover:underline whitespace-nowrap" ' +
            'title="The whole report as a workbook — Summary, Top queries, Storage, Writes, History">' +
            '<i class="fa-solid fa-file-excel mr-1"></i>Download Excel</a>') +
        '<button type="button" data-bqc="refresh" title="Scan again now" aria-label="Scan again now" ' +
        'class="text-slate-400 hover:text-white text-xs"><i class="fa-solid fa-rotate-right"></i></button></div>' +
        '<div class="text-[10px] text-slate-500 mb-2">' + esc(reportMeta(res)) + (extras ? ' · ' + esc(extras) : '') + '</div>';
    if (res.hint) {
        html += '<div class="text-[11px] text-amber-400 mb-2"><i class="fa-solid fa-triangle-exclamation mr-1"></i>' +
            esc(res.hint) + '</div>';
    }
    if (res.disabled) {
        html += '<div class="text-[11px] text-amber-400">' + esc(ENABLE_HINT) + '</div>';
    } else if (res.ok === false) {
        // The heading already says it is unavailable — this is the reason, once.
        html += '<div class="text-[11px] text-amber-400">' + esc(res.error || 'no answer') + '</div>';
    } else if (!shapes.length) {
        html += '<div class="text-[11px] text-slate-400">' + esc(emptyText(res)) + '</div>';
    } else {
        html += '<div class="overflow-x-auto"><table class="w-full text-[11px]">' +
            '<thead class="text-slate-500 border-b border-slate-700"><tr>' +
            th('#', 'text-right') + th('who') + th('runs', 'text-right') + th('billed', 'text-right') +
            th('≈ $', 'text-right') + th('p50 / run', 'text-right') + th('insight') + th('query') + th('', 'text-right') +
            '</tr></thead><tbody>' + shapes.map((s, i) => rowHtml(s && typeof s === 'object' ? s : {}, i)).join('') +
            '</tbody></table></div>';
    }
    body.innerHTML = html;

    body.querySelector('[data-bqc="refresh"]').addEventListener('click', () => render(wrap, true));
    body.querySelectorAll('button[data-ask]').forEach(btn => {
        btn.addEventListener('click', () => sendMessage(explainPrompt(shapes[+btn.dataset.ask])));
    });
}
