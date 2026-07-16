// Insights drawer: collapsible report sections. A section registry, so adding
// the next report (quality gates, deploy history, ...) is one entry with a
// render() — no new layout code. Each section is a native <details> accordion;
// content renders lazily on first expand. All placeholders until their data
// sources are wired.
const INSIGHT_SECTIONS = [
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
            if (d.open && !rendered) { rendered = true; sec.render(body); }
        });
        d.appendChild(sum); d.appendChild(body);
        box.appendChild(d);
    });
}
