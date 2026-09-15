import { escapeHtml as esc } from '../core/format.js';
import { explainPrompt, formatValue, monitoringSummary, seriesLabel } from '../core/monitoring.js';
import { monitoring } from '../api.js';
import { sendMessage } from '../chat.js';
import { opening, withDismiss } from './common.js';

// ---- Monitoring -------------------------------------------------------------
// The team's PromQL checks, run now, straight from /api/monitoring — the server
// decides what fires (tools/monitoring.py); this card only shows it. A check
// that could not run says so: a blank would read as "all clear".
const STATE = {
    firing:  { cls: 'text-red-400',     icon: 'fa-circle-xmark',    text: 'firing' },
    ok:      { cls: 'text-emerald-400', icon: 'fa-circle-check',    text: 'OK' },
    unknown: { cls: 'text-amber-400',   icon: 'fa-circle-question', text: 'could not run' },
};

export async function showMonitoring() {
    const _ph = opening('monitoring');
    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm queue-table-card';
    _ph.replaceWith(wrap);
    await render(wrap, false);
    withDismiss(wrap);
    chat.scrollTop = chat.scrollHeight;
}

async function render(wrap, fresh) {
    wrap.querySelectorAll('.mon-body').forEach(n => n.remove());
    const body = document.createElement('div');
    body.className = 'mon-body';
    body.innerHTML = '<div class="text-[11px] text-slate-500"><span class="dots"><span></span><span></span>' +
        '<span></span></span> Running the checks…</div>';
    wrap.appendChild(body);

    let res;
    try { res = await monitoring(fresh); } catch (e) { res = { ok: false, error: String((e && e.message) || e) }; }
    const checks = (res && res.checks) || [];
    let html =
        '<div class="mb-2 flex items-center gap-2 pr-6">' +
        '<span class="font-semibold text-emerald-300"><i class="fa-solid fa-heart-pulse mr-1"></i>Monitoring</span>' +
        '<span class="text-[11px] text-slate-500">' + esc(monitoringSummary(res)) +
        (res && res.checked_at ? ' · ' + esc(res.checked_at) : '') + '</span>' +
        '<span class="flex-1"></span>' +
        '<button type="button" data-m="refresh" title="Run the checks again" aria-label="Run the checks again" ' +
        'class="text-slate-400 hover:text-white text-xs"><i class="fa-solid fa-rotate-right"></i></button></div>';
    if (res && res.config_error) {
        html += '<div class="text-[11px] text-amber-400 mb-2"><i class="fa-solid fa-triangle-exclamation mr-1"></i>' +
            esc(res.config_error) + '</div>';
    }
    if (res && res.error && !checks.length) {
        html += '<div class="text-[11px] text-amber-400">Monitoring is unavailable: ' + esc(res.error) + '</div>';
    }
    checks.forEach((c, i) => {
        const st = STATE[c.state] || STATE.unknown;
        html += '<div class="border-t border-slate-800 py-2" data-check="' + i + '">' +
            '<div class="flex items-center gap-2">' +
            '<span class="' + st.cls + ' whitespace-nowrap"><i class="fa-solid ' + st.icon + ' mr-1"></i>' + st.text +
            (c.state === 'firing' ? ' (' + c.count + ')' : '') + '</span>' +
            '<span class="text-slate-200 font-medium">' + esc(c.name) + '</span>' +
            (c.severity === 'warn' ? '<span class="text-[10px] text-amber-300">warn</span>' : '') +
            '<span class="flex-1"></span>' +
            (c.state !== 'ok' ? '<button type="button" data-ask="' + i + '" class="text-[11px] text-sky-400 ' +
                'hover:underline whitespace-nowrap"><i class="fa-solid fa-comment-dots mr-1"></i>Ask why</button>' : '') +
            '</div>' +
            (c.description ? '<div class="text-[11px] text-slate-500">' + esc(c.description) + '</div>' : '') +
            '<div class="text-[10px] font-mono text-slate-600 truncate" title="' + esc(c.query) + '">' + esc(c.query) + '</div>';
        if (c.state === 'unknown') {
            html += '<div class="text-[11px] text-amber-400">' + esc(c.error || 'no answer') +
                (c.hint ? ' — ' + esc(c.hint) : '') + '</div>';
        }
        if ((c.series || []).length) {
            html += '<div class="overflow-x-auto"><table class="text-[11px] mt-1">';
            c.series.forEach(s => {
                html += '<tr><td class="pr-3 font-mono text-slate-300 whitespace-nowrap">' + esc(seriesLabel(s.labels)) +
                    '</td><td class="text-red-300 whitespace-nowrap">' + esc(formatValue(s.value)) + '</td></tr>';
            });
            if (c.count > c.series.length) {
                html += '<tr><td class="text-slate-500" colspan="2">…and ' + (c.count - c.series.length) + ' more</td></tr>';
            }
            html += '</table></div>';
        }
        html += '</div>';
    });
    body.innerHTML = html;

    body.querySelector('[data-m="refresh"]').addEventListener('click', () => render(wrap, true));
    body.querySelectorAll('button[data-ask]').forEach(btn => {
        btn.addEventListener('click', () => sendMessage(explainPrompt(checks[+btn.dataset.ask])));
    });
}
