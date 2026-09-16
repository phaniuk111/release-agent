import { escapeHtml as esc } from '../core/format.js';
import { explainPrompt, formatValue, monitoringSummary, orderChecks, seriesLabel, watchingText }
    from '../core/monitoring.js';
import { monitoring, monitoringAlertPolicy } from '../api.js';
import { sendMessage } from '../chat.js';
import { opening, withDismiss } from './common.js';

// ---- Monitoring -------------------------------------------------------------
// The checks, run now, straight from /api/monitoring — the server decides the
// state (tools/monitoring.py); this card only shows it. Four states, none of
// them silent: firing, could not run, OK (with what it watched), and "not
// measured here" — a check with nothing to measure is not a healthy one.
const STATE = {
    firing:  { cls: 'text-red-400',     icon: 'fa-circle-xmark',    text: 'firing' },
    unknown: { cls: 'text-amber-400',   icon: 'fa-circle-question', text: 'could not run' },
    ok:      { cls: 'text-emerald-400', icon: 'fa-circle-check',    text: 'OK' },
    no_data: { cls: 'text-slate-500',   icon: 'fa-circle-minus',    text: 'not measured' },
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

function checkHtml(c, i) {
    const st = STATE[c.state] || STATE.unknown;
    const watch = c.state === 'ok' ? watchingText(c) : '';
    let html = '<div class="border-t border-slate-800 py-2" data-check="' + i + '">' +
        '<div class="flex items-center gap-2 flex-wrap">' +
        '<span class="' + st.cls + ' whitespace-nowrap"><i class="fa-solid ' + st.icon + ' mr-1"></i>' + st.text +
        (c.state === 'firing' ? ' (' + c.count + ')' : '') + '</span>' +
        '<span class="text-slate-200 font-medium">' + esc(c.name) + '</span>' +
        (c.severity === 'warn' ? '<span class="text-[10px] text-amber-300">warn</span>' : '') +
        (watch ? '<span class="text-[11px] text-slate-500">' + esc(watch) + '</span>' : '') +
        '<span class="flex-1"></span>' +
        (c.state === 'firing' || c.state === 'unknown'
            ? '<button type="button" data-ask="' + i + '" class="text-[11px] text-sky-400 hover:underline ' +
              'whitespace-nowrap"><i class="fa-solid fa-comment-dots mr-1"></i>Ask why</button>' : '') +
        (c.state !== 'no_data'
            ? '<button type="button" data-alert="' + i + '" class="text-[11px] text-slate-400 hover:text-white ' +
              'whitespace-nowrap" title="The Cloud Monitoring alert policy that notifies you when this fires">' +
              '<i class="fa-solid fa-bell mr-1"></i>Make it an alert</button>' : '') +
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
    return html + '<div class="alert-slot"></div></div>';
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
    const checks = orderChecks((res && res.checks) || []);
    const measured = checks.filter(c => c.state !== 'no_data');
    const unmeasured = checks.filter(c => c.state === 'no_data');
    let html =
        '<div class="mb-1 flex items-center gap-2 pr-6 flex-wrap">' +
        '<span class="font-semibold text-emerald-300"><i class="fa-solid fa-heart-pulse mr-1"></i>Monitoring</span>' +
        '<span class="text-[11px] text-slate-400">' + esc(monitoringSummary(res)) + '</span>' +
        '<span class="flex-1"></span>' +
        '<button type="button" data-m="refresh" title="Run the checks again" aria-label="Run the checks again" ' +
        'class="text-slate-400 hover:text-white text-xs"><i class="fa-solid fa-rotate-right"></i></button></div>' +
        '<div class="text-[10px] text-slate-500 mb-2">' + esc((res && res.source) || '') +
        (res && res.checked_at ? ' · ' + esc(res.checked_at) : '') + '</div>';
    if (res && res.config_error) {
        html += '<div class="text-[11px] text-amber-400 mb-2"><i class="fa-solid fa-triangle-exclamation mr-1"></i>' +
            esc(res.config_error) + '</div>';
    }
    if (res && res.ok === false && res.error && !checks.length) {
        html += '<div class="text-[11px] text-amber-400">Monitoring is unavailable: ' + esc(res.error) + '</div>';
    }
    if (checks.length && !measured.length) {
        html += '<div class="text-[11px] text-amber-400 mb-2">Nothing these checks watch reports into this ' +
            'project — the metrics may live in another one (set PROMETHEUS_PROJECT), or add checks for the ' +
            "team's own metrics (MONITOR_CHECKS).</div>";
    }
    measured.forEach(c => { html += checkHtml(c, checks.indexOf(c)); });
    if (unmeasured.length) {
        html += '<details class="border-t border-slate-800 pt-2 text-[11px]"><summary class="text-slate-500 cursor-pointer">' +
            unmeasured.length + ' not measured here — no data for them in this project</summary>';
        unmeasured.forEach(c => { html += checkHtml(c, checks.indexOf(c)); });
        html += '</details>';
    }
    body.innerHTML = html;

    body.querySelector('[data-m="refresh"]').addEventListener('click', () => render(wrap, true));
    body.querySelectorAll('button[data-ask]').forEach(btn => {
        btn.addEventListener('click', () => sendMessage(explainPrompt(checks[+btn.dataset.ask])));
    });
    body.querySelectorAll('button[data-alert]').forEach(btn => {
        btn.addEventListener('click', () => showAlertPolicy(btn.closest('[data-check]'), checks[+btn.dataset.alert]));
    });
}

// The alert is Cloud Monitoring's to run — it evaluates every minute whether or
// not this portal is up, and notifies the channels the team picks. Shown for
// someone with rights in the project to apply; the portal changes nothing.
async function showAlertPolicy(row, check) {
    const slot = row.querySelector('.alert-slot');
    if (slot.childElementCount) { slot.innerHTML = ''; return; }
    slot.innerHTML = '<div class="text-[11px] text-slate-500">Building the policy…</div>';
    let res;
    try { res = await monitoringAlertPolicy(check.name); } catch (e) { res = { ok: false, error: String(e) }; }
    if (!res || !res.ok) { slot.innerHTML = '<div class="text-[11px] text-amber-400">' + esc((res && res.error) || 'failed') + '</div>'; return; }
    slot.innerHTML =
        '<div class="text-[11px] text-slate-400 mt-1">Save as <span class="font-mono">policy.json</span>, add your ' +
        'notification channels, then apply it once — Cloud Monitoring checks it every minute and notifies them:</div>' +
        '<pre class="my-1 rounded-lg bg-slate-950/40 border border-slate-800 px-2 py-1 text-[11px] text-slate-200 ' +
        'overflow-x-auto"><code>' + esc(JSON.stringify(res.policy, null, 2)) + '</code></pre>' +
        '<div class="text-[10px] font-mono text-slate-500">gcloud alpha monitoring policies create --policy-from-file=policy.json</div>';
}
