import { escapeHtml as esc } from '../core/format.js';
import { checkLine, overall, prHeading, prStory } from '../core/deploy_tracking.js';
import { deployTracking } from '../api.js';
import { opening, withDismiss } from './common.js';

// ---- Release notifications --------------------------------------------------
// What the deployment tracker saw and sent for each release PR into PRL1/PRD,
// and why a notification did not go out — straight from /api/deploy-tracking
// (adk_release_agent/deploy_tracker.report). Read-only: it sends nothing.
const LOOK = {
    ok:      { cls: 'text-emerald-400', icon: 'fa-circle-check' },
    warn:    { cls: 'text-amber-400',   icon: 'fa-triangle-exclamation' },
    problem: { cls: 'text-red-400',     icon: 'fa-circle-xmark' },
    info:    { cls: 'text-sky-400',     icon: 'fa-circle-info' },
};

export async function showDeployTracking() {
    const _ph = opening('release notifications');
    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm queue-table-card';
    _ph.replaceWith(wrap);
    await render(wrap);
    withDismiss(wrap);
    chat.scrollTop = chat.scrollHeight;
}

function lineHtml(l) {
    const look = LOOK[l.state] || LOOK.info;
    return '<div class="text-[12px] flex gap-2 items-start py-0.5"><i class="fa-solid ' + look.icon + ' ' + look.cls +
        ' mt-0.5"></i><span class="text-slate-300">' + esc(l.text) +
        (l.url ? ' <a href="' + esc(l.url) + '" target="_blank" rel="noopener" class="text-sky-400 hover:underline">' +
                 'run <i class="fa-solid fa-arrow-up-right-from-square text-[9px]"></i></a>' : '') +
        '</span></div>';
}

function prHtml(p) {
    const when = p.updated_at ? String(p.updated_at).replace('T', ' ').slice(0, 16) + ' UTC' : '';
    return '<div class="border-t border-slate-800 py-2">' +
        '<div class="flex items-center gap-2 flex-wrap">' +
        '<span class="text-slate-200 font-medium">' + esc(prHeading(p)) + '</span>' +
        (p.link ? '<a href="' + esc(p.link) + '" target="_blank" rel="noopener" class="text-[11px] text-sky-400 ' +
                  'hover:underline">open PR</a>' : '') +
        '<span class="text-[11px] text-slate-500">' + esc(p.title || '') + '</span>' +
        '<span class="flex-1"></span><span class="text-[10px] text-slate-500">' + esc(when) + '</span></div>' +
        prStory(p).map(lineHtml).join('') + '</div>';
}

async function render(wrap) {
    wrap.querySelectorAll('.dt-body').forEach(n => n.remove());
    const body = document.createElement('div');
    body.className = 'dt-body';
    body.innerHTML = '<div class="text-[11px] text-slate-500"><span class="dots"><span></span><span></span>' +
        '<span></span></span> Checking the tracker and GitHub…</div>';
    wrap.appendChild(body);

    let res;
    try { res = await deployTracking(); } catch (e) { res = { ok: false, error: String((e && e.message) || e) }; }
    const head = overall(res);
    const look = LOOK[head.state] || LOOK.info;
    const cfg = (res && res.config) || {};
    let html =
        '<div class="mb-1 flex items-center gap-2 pr-6 flex-wrap">' +
        '<span class="font-semibold text-emerald-300"><i class="fa-solid fa-bell mr-1"></i>Release notifications</span>' +
        '<span class="flex-1"></span>' +
        '<button type="button" data-dt="refresh" title="Check again" aria-label="Check again" ' +
        'class="text-slate-400 hover:text-white text-xs"><i class="fa-solid fa-rotate-right"></i></button></div>' +
        '<div class="text-[12px] ' + look.cls + ' mb-2"><i class="fa-solid ' + look.icon + ' mr-1"></i>' +
        esc(head.text) + '</div>';
    if (res && res.ok !== false) {
        html += '<div class="text-[11px] text-slate-500 uppercase tracking-wide mt-2">Checks</div>' +
            (res.checks || []).map(c => lineHtml(checkLine(c))).join('');
        if (res.last_error) html += lineHtml({ state: 'problem', text: 'Last check failed: ' + res.last_error });
        if (res.history_error) html += lineHtml({ state: 'warn', text: 'Could not read what was sent: ' + res.history_error });
        const prs = res.prs || [];
        html += '<div class="text-[11px] text-slate-500 uppercase tracking-wide mt-3">Release PRs — last 14 days</div>' +
            (prs.length ? prs.map(prHtml).join('')
                : '<div class="text-[12px] text-slate-500 py-1">No release PR into ' +
                  esc((cfg.branches || []).join(' or ') || 'PRL1 or PRD') + ' in the last 14 days.</div>');
        html += '<div class="text-[10px] text-slate-600 mt-2">Sends to ' +
            esc(cfg.notify_repo ? cfg.notify_repo + ':' + cfg.notify_workflow : 'nothing (NOTIFY_WORKFLOW empty)') +
            (cfg.notify_test ? ' as a test' : '') + ' · watches ' + esc(cfg.deploy_repo || '—') +
            ' · counts ' + esc((cfg.deploy_workflows || []).join(', ') || 'every run on the merge commit') +
            ' · kept in ' + esc(cfg.store || '—') + '</div>';
    }
    body.innerHTML = html;
    body.querySelector('[data-dt="refresh"]').addEventListener('click', () => render(wrap));
}
