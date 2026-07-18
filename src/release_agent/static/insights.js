// Insights drawer: collapsible report sections. A section registry, so adding
// the next report (quality gates, deploy history, ...) is one entry with a
// render() — no new layout code. Each section is a native <details> accordion.
// Static sections render once on first expand; sections marked live:true
// re-render every time they're opened (fresh data each look).
import { API_BASE } from './state.js';
import { showQueueForm } from './forms.js';
import { loadReleaseStatus } from './status.js';

function _timeAgo(iso) {
    if (!iso) return '';
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
}

async function _renderQueueSection(body) {
    body.innerHTML = '<div class="text-[11px] text-slate-500">Loading queue…</div>';
    let ctx = null;
    try {
        const r = await fetch(API_BASE + '/api/release-queue');
        ctx = await r.json();
    } catch (e) { ctx = { ok: false, error: String(e) }; }

    const addBtn = '<button id="q-add-btn" class="text-[11px] text-emerald-400 hover:text-emerald-300">' +
        '<i class="fa-solid fa-plus mr-1"></i>Add yours</button>';
    if (!ctx || !ctx.ok) {
        body.innerHTML = '<div class="text-[11px] text-slate-500">' +
            (ctx && ctx.disabled ? 'Queue disabled (no BigQuery configured).'
                : 'Queue unavailable: ' + ((ctx && ctx.error) || 'unknown error')) + '</div>';
        return;
    }
    if (!ctx.queue.length) {
        body.innerHTML = '<div class="text-[11px] text-slate-500 mb-1">Nothing queued yet — ' +
            'devs can register charts for the next release any day of the week.</div>' + addBtn;
    } else {
        let html = '<div class="space-y-1.5">';
        ctx.queue.forEach(q => {
            const badge = q.build_verified === true
                ? '<i class="fa-solid fa-circle-check text-emerald-400" title="build verified"></i>'
                : q.build_verified === false
                    ? '<i class="fa-solid fa-triangle-exclamation text-amber-400" title="no traceable build at queue time"></i>'
                    : '';
            const tip = [q.change_details, q.note].filter(Boolean).join(' · ');
            html += '<div class="flex items-center gap-1.5 text-[11px] font-mono text-slate-300" ' +
                (tip ? 'title="' + tip.split('"').join('&quot;') + '"' : '') + '>' +
                '<span class="flex-1 truncate">' + q.artifact_name + ':' + q.artifact_version + '</span>' +
                badge +
                (q.jira_ticket ? '<span class="text-amber-300/80">' + q.jira_ticket + '</span>' : '') +
                (q.prl1_only ? '<span class="text-violet-400">PRL1</span>' : '') +
                (q.df_only ? '<span class="text-sky-400">DF</span>' : '') +
                '<span class="text-slate-600">' + (q.requested_by || '').split('@')[0] + '</span>' +
                '<span class="text-slate-700">' + _timeAgo(q.requested_at) + '</span>' +
                '<button data-wd="' + q.artifact_name + '" title="Withdraw from the queue" ' +
                'class="text-slate-600 hover:text-red-400"><i class="fa-solid fa-xmark"></i></button>' +
                '</div>';
        });
        html += '</div><div class="mt-2 flex items-center justify-between">' + addBtn +
            '<span class="text-[10px] text-slate-600">' + ctx.queue.length + ' queued</span></div>';
        body.innerHTML = html;
        body.querySelectorAll('button[data-wd]').forEach(btn => {
            btn.addEventListener('click', async () => {
                btn.disabled = true;
                try {
                    await fetch(API_BASE + '/api/release-queue/withdraw', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            artifact_name: btn.dataset.wd,
                            requested_by: localStorage.getItem('queue_email') || '',
                        }),
                    });
                } catch (e) {}
                _renderQueueSection(body);
                loadReleaseStatus();
            });
        });
    }
    const add = body.querySelector('#q-add-btn');
    if (add) add.addEventListener('click', () => showQueueForm());
}

// Release stats: which images shipped, how often — with a pattern filter
// (glob like acme-capability* or plain substring) over the BQ event log.
async function _renderStatsSection(body) {
    if (!body.querySelector('#ri-pattern')) {
        body.innerHTML =
            '<div class="flex gap-1.5 mb-2">' +
            '<input id="ri-pattern" type="text" placeholder="filter: acme-capability* (empty = all)" ' +
            'class="flex-1 bg-slate-900 border border-slate-700 rounded-lg px-2 py-1 text-[11px] text-white focus:outline-none">' +
            '<select id="ri-type" class="bg-slate-900 border border-slate-700 rounded-lg px-1.5 py-1 text-[11px] text-slate-300 focus:outline-none">' +
            '<option value="released">released</option>' +
            '<option value="deployed">deployed</option>' +
            '<option value="state">deployed state</option>' +
            '<option value="all">all events</option></select>' +
            '</div><div id="ri-results" class="text-[11px] text-slate-500">Loading…</div>';
        const rerun = () => _renderStatsSection(body);
        body.querySelector('#ri-pattern').addEventListener('change', rerun);
        body.querySelector('#ri-pattern').addEventListener('keydown', e => { if (e.key === 'Enter') rerun(); });
        body.querySelector('#ri-type').addEventListener('change', rerun);
    }
    const results = body.querySelector('#ri-results');
    results.innerHTML = '<span class="text-slate-600">Loading…</span>';
    let data = null;
    try {
        const qs = new URLSearchParams({
            pattern: body.querySelector('#ri-pattern').value.trim(),
            event_type: body.querySelector('#ri-type').value,
            days: '90',
        });
        const r = await fetch(API_BASE + '/api/release-insights?' + qs.toString());
        data = await r.json();
    } catch (e) { data = { ok: false, error: String(e) }; }
    if (!data || !data.ok) {
        results.innerHTML = data && data.disabled ? 'Stats disabled (no BigQuery configured).'
            : 'Unavailable: ' + ((data && data.error) || 'unknown error');
        return;
    }
    if (data.event_type === 'state') {
        // Per-environment deployed state: env — distinct image count — images.
        if (!data.environments.length) {
            results.innerHTML = 'No deployed state recorded yet.';
            return;
        }
        let html = '<div class="text-[10px] text-slate-600 mb-1">' + data.distinct_images +
            ' distinct image(s) across ' + data.environments.length + ' env(s)</div><div class="space-y-1.5">';
        data.environments.forEach(e => {
            html += '<div class="text-[11px] font-mono">' +
                '<div class="flex items-center gap-1.5 text-slate-300">' +
                '<span class="flex-1 uppercase">' + e.environment + '</span>' +
                '<span class="bg-slate-800 rounded px-1.5 text-emerald-300">' + e.count + '</span></div>' +
                e.images.map(i => '<div class="text-[10px] text-slate-600 truncate pl-1">' +
                    i.artifact_name + (i.version ? ':' + i.version : '') + '</div>').join('') +
                '</div>';
        });
        results.innerHTML = html + '</div>';
        return;
    }
    if (!data.charts.length) {
        results.innerHTML = 'No matches in the last ' + data.days + ' days.';
        return;
    }
    let html = '<div class="text-[10px] text-slate-600 mb-1">' + data.total_events + ' event(s) · ' +
        data.chart_count + ' chart(s) · last ' + data.days + 'd</div><div class="space-y-1">';
    data.charts.forEach(c => {
        const rel = c.releases.length ? c.releases[c.releases.length - 1] : null;
        const sub = rel ? ('last: ' + (rel.release || '') + (rel.pr ? ' (PR #' + rel.pr + ')' : ''))
            : Object.keys(c.environments || {}).map(e => e + '×' + c.environments[e]).join(' ');
        html += '<div class="text-[11px] font-mono text-slate-300">' +
            '<div class="flex items-center gap-1.5">' +
            '<span class="flex-1 truncate">' + c.artifact_name + '</span>' +
            '<span class="bg-slate-800 rounded px-1.5 text-emerald-300">' + c.count + '</span></div>' +
            (sub ? '<div class="text-[10px] text-slate-600 truncate pl-0.5">' + sub +
                   (c.versions.length ? ' · v' + c.versions[c.versions.length - 1] : '') + '</div>' : '') +
            '</div>';
    });
    results.innerHTML = html + '</div>';
}

const INSIGHT_SECTIONS = [
    {
        id: 'next-release',
        title: 'Next release queue',
        icon: 'fa-cart-plus',
        live: true,
        render: _renderQueueSection,
    },
    {
        id: 'release-stats',
        title: 'Release stats',
        icon: 'fa-chart-simple',
        live: true,
        render: _renderStatsSection,
    },
    {
        id: 'uat-status',
        title: 'UAT status',
        icon: 'fa-flask',
        render: (body) => {
            body.innerHTML =
                '<div class="text-[11px] text-slate-500 leading-relaxed">' +
                'Placeholder — UAT environment status will render here once its ' +
                'data source is wired.</div>' +
                '<div class="mt-2 border border-dashed border-slate-700 rounded-lg p-3 text-center text-[11px] text-slate-600">' +
                '<i class="fa-solid fa-flask mr-1"></i>UAT status</div>';
        },
    },
    {
        id: 'sealights',
        title: 'Sealights coverage',
        icon: 'fa-bullseye',
        render: (body) => {
            body.innerHTML =
                '<div class="text-[11px] text-slate-500 leading-relaxed">' +
                'Placeholder — the Sealights quality/coverage report for the ' +
                'charts in today\'s release will render here once the ' +
                'Sealights API is wired (per-build coverage, quality gate ' +
                'status, test-gap alerts).</div>' +
                '<div class="mt-2 border border-dashed border-slate-700 rounded-lg p-3 text-center text-[11px] text-slate-600">' +
                '<i class="fa-solid fa-bullseye mr-1"></i>coverage report</div>';
        },
    },
];

export function toggleInsights() {
    const panel = document.getElementById('insights-panel');
    const open = panel.classList.toggle('hidden');
    localStorage.setItem('insights_open', open ? '0' : '1');
    if (!panel.classList.contains('hidden')) renderInsights();
}

let _insightsRendered = false;
export function renderInsights() {
    if (_insightsRendered) return;
    _insightsRendered = true;
    const box = document.getElementById('insights-sections');
    box.innerHTML = '';
    INSIGHT_SECTIONS.forEach(sec => {
        const d = document.createElement('details');
        d.className = 'border border-slate-700/70 rounded-xl overflow-hidden';
        const sum = document.createElement('summary');
        sum.className = 'cursor-pointer select-none px-3 py-2 text-xs text-slate-300 hover:bg-slate-800/60 flex items-center gap-2';
        sum.innerHTML = '<i class="fa-solid ' + sec.icon + ' text-emerald-400/90 text-[11px]"></i>' + sec.title +
            '<i class="fa-solid fa-chevron-down ml-auto text-[9px] text-slate-600"></i>';
        const body = document.createElement('div');
        body.className = 'px-3 pb-3 pt-1';
        let rendered = false;
        d.addEventListener('toggle', () => {
            if (d.open && (sec.live || !rendered)) { rendered = true; sec.render(body); }
        });
        d.appendChild(sum); d.appendChild(body);
        box.appendChild(d);
    });
}
