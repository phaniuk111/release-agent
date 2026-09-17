// Insights drawer: collapsible report sections. A section registry, so adding
// the next report (quality gates, deploy history, ...) is one entry with a
// render() — no new layout code. Each section is a native <details> accordion.
// Static sections render once on first expand; sections marked live:true
// re-render every time they're opened (fresh data each look).
import { releaseInsights } from './api.js';
import { escapeHtml as esc } from './core/format.js';
import { showQueueTable } from './forms/queue_table.js';

// The queue itself is ONE screen: forms/queue_table.js. This drawer used to
// re-render it here — a second, thinner copy that withdrew against whatever
// email happened to be in localStorage (an empty one writes an empty requester
// into an append-only audit log) and swallowed every refusal, including the
// stale-version guard, in silence. It opens the real table instead.
function _renderQueueSection(body) {
    body.innerHTML = '<div class="text-[11px] text-slate-500 mb-1">What is queued for the next ' +
        'release — routing, JIRA, build and controls per chart, each row removable.</div>';
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'text-[11px] text-emerald-400 hover:text-emerald-300';
    open.innerHTML = '<i class="fa-solid fa-list-ul mr-1"></i>Open the release queue';
    open.addEventListener('click', () => showQueueTable());
    body.appendChild(open);
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
        data = await releaseInsights({
            pattern: body.querySelector('#ri-pattern').value.trim(),
            event_type: body.querySelector('#ri-type').value,
            days: '90',
        });
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
                '<span class="flex-1 uppercase">' + esc(e.environment) + '</span>' +
                '<span class="bg-slate-800 rounded px-1.5 text-emerald-300">' + e.count + '</span></div>' +
                e.images.map(i => '<div class="text-[10px] text-slate-600 truncate pl-1">' +
                    esc(i.artifact_name) + (i.version ? ':' + esc(i.version) : '') + '</div>').join('') +
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
            '<span class="flex-1 truncate">' + esc(c.artifact_name) + '</span>' +
            '<span class="bg-slate-800 rounded px-1.5 text-emerald-300">' + c.count + '</span></div>' +
            (sub ? '<div class="text-[10px] text-slate-600 truncate pl-0.5">' + esc(sub) +
                   (c.versions.length ? ' · v' + esc(c.versions[c.versions.length - 1]) : '') + '</div>' : '') +
            '</div>';
    });
    results.innerHTML = html + '</div>';
}

const INSIGHT_SECTIONS = [
    {
        id: 'next-release',
        title: 'Next release queue',
        icon: 'fa-cart-plus',
        render: _renderQueueSection,
    },
    {
        id: 'release-stats',
        title: 'Release stats',
        icon: 'fa-chart-simple',
        live: true,
        render: _renderStatsSection,
    },
    // A section belongs here once it has a data source. An empty box that says
    // "Placeholder" is a to-do list shipped to the people using the portal.
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
