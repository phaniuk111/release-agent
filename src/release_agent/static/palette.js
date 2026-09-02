// The capability catalog: the in-chat pill list plus the browse-first command
// palette (⌘K / "/" / the header button). Every capability lists here; typing
// in the palette only filters.
import { sendMessage } from './chat.js';
import { showDeployForm } from './forms.js';

// Groups follow the WEEK, not the code: a dev queues on Monday, DevOps cuts the
// release on Thursday, deploys are the per-chart pushes in between, and checks
// answer "is this safe / did it work". Rendered as rows here and as sections in
// ⌘K, so a capability is categorised in exactly one place.
export const GROUPS = [
    {name:'Release',  hint:'the weekly cut — queue it, build the file-set, ship it'},
    {name:'Deploy',   hint:'push one chart or DF template to an environment'},
    {name:'Check',    hint:'read-only — status, controls, builds, PRs'},
];

// Quick actions — what the agent can do. mode 'send' runs immediately;
// otherwise the text is pre-filled so the user edits the image:tag first.
export const CAPABILITIES = [
    {group:'Release', icon:'fa-cart-plus',         label:'Add to next release',  desc:'queue your chart:version now — DevOps picks it up on release day', form:'queue'},
    {group:'Release', icon:'fa-box-open',          label:'CARE Release',         desc:'full weekly release: helm artifacts + CHG + governance file-set (pre-filled from the queue)', form:'release'},
    {group:'Release', icon:'fa-water',             label:'DF Release',           desc:'Dataflow release: DF images + CHG + governance file-set (images excluded from helm deploys)', form:'df-release'},
    {group:'Release', icon:'fa-eraser',            label:'Remove from release',  desc:'unstage a chart before it ships',             send:false, text:"remove <chart-name> from the release"},
    {group:'Release', icon:'fa-shield-heart',      label:'Release to PROD',      desc:'promote the PRD release via SIT→UAT→PRD (finalizes the release)',  send:true,  text:'release prod'},
    {group:'Deploy',  icon:'fa-flask',             label:'Deploy to CARE UAT',   desc:'deploy a Helm chart to CARE UAT',             form:'uat'},
    {group:'Deploy',  icon:'fa-water',             label:'Deploy to DF UAT',     desc:'trigger the Dataflow flex-template deploy workflow', form:'df-uat'},
    // No "Deploy to PROD" pill: PROD is reached through the release ("Release to
    // PROD"), not by pushing a single chart. The form itself still exists and
    // still opens for a typed "deploy <chart>:<version> to prod" — this only
    // removes it from the offered actions.
    {group:'Check',   icon:'fa-calendar-day',      label:'Deploy status',        desc:'UAT, PRD & the release PR',                   send:true,  text:'what is the current deploy status of UAT, PRD and the PRD release PR?'},
    {group:'Check',   icon:'fa-circle-check',      label:'Verify a build',       desc:'tag-gen step + RCTLD controls for a tag',     send:false, text:'verify <image>:<tag> was built in <owner/repo>'},
    {group:'Check',   icon:'fa-list-check',        label:'Check PRD controls',   desc:'pass/fail RCTLD control gates for a tag',     send:false, text:'check build controls for <image>:<tag> before a PRD release'},
    {group:'Check',   icon:'fa-code-pull-request', label:'Track a PR',           desc:'find the PR & summarize CHG/RMG/controls',    send:false, text:'find the deployment PR for <image>:<tag> and summarize its CHG, RMG and RLFT controls'},
    {group:'Check',   icon:'fa-images',            label:'List allowed images',  desc:'what I can promote',                          send:true,  text:'what images can I promote?'},
    {group:'Check',   icon:'fa-clock-rotate-left', label:'Recent workflow runs', desc:'status of the latest runs',                   send:true,  text:'show me the 5 most recent workflow runs and their status'},
];

// One accent per group so the row a pill belongs to is readable at a glance,
// and mutating groups do not wear the same colour as the read-only one.
// Full class strings: Tailwind's runtime only sees classes that appear literally.
const GROUP_STYLE = {
    Release: {icon:'text-violet-300',  border:'hover:border-violet-400/50',  label:'text-violet-300/70'},
    Deploy:  {icon:'text-sky-300',     border:'hover:border-sky-400/50',     label:'text-sky-300/70'},
    Check:   {icon:'text-emerald-300', border:'hover:border-emerald-400/50', label:'text-emerald-300/70'},
};

export function runQuick(text, send) {
    if (send) {
        sendMessage(text);   // send directly so multi-line messages keep their newlines
        return;
    }
    const input = document.getElementById('input');
    input.value = text;
    input.focus();
    try { input.setSelectionRange(text.length, text.length); } catch (e) {}
}

export function showCapabilities() {
    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot rounded-2xl px-4 py-3 text-sm';

    const title = document.createElement('div');
    title.className = 'mb-2.5 text-slate-400 text-xs';
    title.textContent = 'Pick one, or just type — hover for details:';
    wrap.appendChild(title);

    // A labelled row per group. The label sits in a fixed-width column on wide
    // screens and above the pills once that no longer fits, so the pill rows
    // stay aligned without a media query.
    GROUPS.forEach(group => {
        const members = CAPABILITIES.filter(c => c.group === group.name);
        if (!members.length) return;              // a group with nothing in it is not a heading
        const style = GROUP_STYLE[group.name] || GROUP_STYLE.Check;

        const block = document.createElement('div');
        block.className = 'flex flex-col sm:flex-row sm:items-start gap-1 sm:gap-3 mb-2 last:mb-0';

        const heading = document.createElement('div');
        heading.className = 'shrink-0 sm:w-16 sm:pt-1 text-[10px] uppercase tracking-wider ' + style.label;
        heading.textContent = group.name;
        heading.title = group.hint;
        block.appendChild(heading);

        const row = document.createElement('div');
        row.className = 'flex flex-wrap gap-1.5';
        members.forEach(c => {
            const btn = document.createElement('button');
            btn.title = c.desc;
            btn.className = 'border border-slate-700/80 ' + style.border + ' ' +
                'hover:bg-slate-800/60 rounded-full px-3 py-1 text-xs text-slate-300 ' +
                'flex items-center gap-1.5 transition-colors';
            btn.innerHTML = '<i class="fa-solid ' + c.icon + ' ' + style.icon + ' text-[11px]"></i>' + c.label;
            btn.addEventListener('click', () => c.form ? showDeployForm(c.form) : runQuick(c.text, c.send));
            row.appendChild(btn);
        });
        block.appendChild(row);
        wrap.appendChild(block);
    });

    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
}

// ---- Command palette -----------------------------------------------------
// Sections come from each capability's declared group — the palette used to
// re-derive them by pattern-matching the label, which silently mis-filed
// anything whose wording drifted (every form counted as "Deploy", so the
// release forms landed there too).
const CATEGORY_ORDER = GROUPS.map(g => g.name);
function paletteActions() {
    return CAPABILITIES.map(c => ({
        label: c.label, desc: c.desc, icon: c.icon, category: c.group,
        run: () => c.form ? showDeployForm(c.form) : runQuick(c.text, c.send),
    }));
}

let _palette = null, _palSelected = 0, _palItems = [];
function _buildPalette() {
    const overlay = document.createElement('div');
    overlay.id = 'palette-overlay';
    overlay.className = 'fixed inset-0 z-50 hidden bg-black/50 flex items-start justify-center pt-24';
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closePalette(); });
    overlay.innerHTML =
        '<div class="glass rounded-2xl w-full max-w-2xl overflow-hidden border border-slate-700">' +
        '<div class="flex items-center gap-2 px-4 py-2.5 border-b border-slate-700/60">' +
        '<i class="fa-solid fa-magnifying-glass text-slate-500 text-xs"></i>' +
        '<input id="palette-input" type="text" placeholder="Type to filter — or just browse" ' +
        'class="flex-1 bg-transparent text-sm text-white placeholder-slate-500 focus:outline-none"></div>' +
        '<div id="palette-list" class="max-h-80 overflow-y-auto py-1"></div>' +
        '<div class="px-4 py-1.5 border-t border-slate-700/60 text-[10px] text-slate-500">' +
        '↑↓ browse · ↵ open · esc close</div></div>';
    document.body.appendChild(overlay);
    const input = overlay.querySelector('#palette-input');
    input.addEventListener('input', () => _renderPalette(input.value));
    input.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown') { e.preventDefault(); _palSelected = Math.min(_palSelected + 1, _palItems.length - 1); _renderPalette(input.value, true); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); _palSelected = Math.max(_palSelected - 1, 0); _renderPalette(input.value, true); }
        else if (e.key === 'Enter') { e.preventDefault(); const it = _palItems[_palSelected]; if (it) { closePalette(); it.run(); } }
        else if (e.key === 'Escape') { closePalette(); }
    });
    return overlay;
}
function _renderPalette(filter, keepSelection) {
    const list = document.getElementById('palette-list');
    const q = (filter || '').trim().toLowerCase();
    _palItems = paletteActions().filter(a =>
        !q || (a.label + ' ' + a.desc + ' ' + a.category).toLowerCase().includes(q));
    _palItems.sort((a, b) => CATEGORY_ORDER.indexOf(a.category) - CATEGORY_ORDER.indexOf(b.category));
    if (!keepSelection) _palSelected = 0;
    list.innerHTML = '';
    if (!_palItems.length) {
        list.innerHTML = '<div class="px-4 py-3 text-xs text-slate-500">Nothing matches — ask in plain English instead.</div>';
        return;
    }
    let lastCat = null;
    _palItems.forEach((a, idx) => {
        if (a.category !== lastCat) {
            lastCat = a.category;
            const h = document.createElement('div');
            h.className = 'px-4 pt-2 pb-0.5 text-[10px] uppercase tracking-wide ' +
                (GROUP_STYLE[a.category] || GROUP_STYLE.Check).label;
            h.textContent = a.category;
            list.appendChild(h);
        }
        const row = document.createElement('button');
        row.className = 'w-full text-left px-4 py-1.5 flex items-center justify-between gap-3 ' +
            (idx === _palSelected ? 'bg-emerald-500/15' : 'hover:bg-slate-800/70');
        // The label is the thing being chosen, so it never wraps or truncates;
        // the description gives up its space and gets ellipsised instead.
        const icon = (GROUP_STYLE[a.category] || GROUP_STYLE.Check).icon;
        row.innerHTML = '<span class="text-sm text-slate-200 shrink-0 whitespace-nowrap">' +
            '<i class="fa-solid ' + a.icon + ' ' + icon + ' mr-2 w-4 text-center"></i>' + a.label + '</span>' +
            // min-w-0 is what lets a flex child shrink far enough to ellipsise.
            '<span class="text-[11px] text-slate-500 truncate text-right min-w-0">' + a.desc + '</span>';
        row.addEventListener('click', () => { closePalette(); a.run(); });
        list.appendChild(row);
    });
}
export function openPalette() {
    if (!_palette) _palette = _buildPalette();
    _palette.classList.remove('hidden');
    const input = _palette.querySelector('#palette-input');
    input.value = '';
    _renderPalette('');
    input.focus();
}
export function closePalette() {
    if (_palette) _palette.classList.add('hidden');
    document.getElementById('input').focus();
}
