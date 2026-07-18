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
            html += '<div class="flex items-center gap-1.5 text-[11px] font-mono text-slate-300" ' +
                (q.note ? 'title="' + q.note.split('"').join('&quot;') + '"' : '') + '>' +
                '<span class="flex-1 truncate">' + q.artifact_name + ':' + q.artifact_version + '</span>' +
                badge +
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

const INSIGHT_SECTIONS = [
    {
        id: 'next-release',
        title: 'Next release queue',
        icon: 'fa-cart-plus',
        live: true,
        render: _renderQueueSection,
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
