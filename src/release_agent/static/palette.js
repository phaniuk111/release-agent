// The capability catalog: the in-chat pill list plus the browse-first command
// palette (⌘K / "/" / the header button). Every capability lists here; typing
// in the palette only filters.
import { sendMessage } from './chat.js';
import { showDeployForm } from './forms.js';

// Capability categories: the single source of truth for grouping in BOTH the
// in-chat catalog and the command palette. Ordered by a developer's day:
// stage a release, deploy, then verify/track, then utilities.
export const CATEGORIES = [
    { key: 'release', label: 'Release',        icon: 'fa-box-open',         accent: 'text-amber-400/90' },
    { key: 'deploy',  label: 'Deploy',         icon: 'fa-rocket',           accent: 'text-emerald-400/90' },
    { key: 'inspect', label: 'Verify & Track', icon: 'fa-magnifying-glass', accent: 'text-sky-400/90' },
    { key: 'ops',     label: 'Utilities',      icon: 'fa-wrench',           accent: 'text-slate-400' },
];

// Quick actions — what the agent can do. mode 'send' runs immediately;
// otherwise the text is pre-filled so the user edits the image:tag first.
export const CAPABILITIES = [
    {cat:'release', icon:'fa-cart-plus',         label:'Add to next release',  desc:'queue your chart:version now — DevOps picks it up on release day', form:'queue'},
    {cat:'release', icon:'fa-box-open',          label:'CARE Release',         desc:'full weekly release: helm artifacts + CHG + governance file-set (pre-filled from the queue)', form:'release'},
    {cat:'release', icon:'fa-water',             label:'DF Release',           desc:'Dataflow release: DF images + CHG + governance file-set (images excluded from helm deploys)', form:'df-release'},
    {cat:'release', icon:'fa-shield-heart',      label:'Release to PROD',      desc:'promote the PRD release via SIT→UAT→PRD (finalizes the release)',  send:true,  text:'release prod'},
    {cat:'release', icon:'fa-eraser',            label:'Remove from release',  desc:'unstage a chart before it ships',             send:false, text:"remove <chart-name> from the release"},
    {cat:'deploy',  icon:'fa-flask',             label:'Deploy to UAT',        desc:'deploy a Helm chart to UAT',                  form:'uat'},
    {cat:'deploy',  icon:'fa-shield-halved',     label:'Deploy to PROD',       desc:'deploy a Helm chart to PROD',                  form:'prod'},
    {cat:'deploy',  icon:'fa-water',             label:'Deploy to DF UAT',     desc:'trigger the Dataflow flex-template deploy workflow', form:'df-uat'},
    {cat:'inspect', icon:'fa-calendar-day',      label:'Deploy status',        desc:'UAT, PRD & the release PR',                   send:true,  text:'what is the current deploy status of UAT, PRD and the PRD release PR?'},
    {cat:'inspect', icon:'fa-circle-check',      label:'Verify a build',       desc:'tag-gen step + RLFT controls for a tag',      send:false, text:'verify <image>:<tag> was built in <owner/repo>'},
    {cat:'inspect', icon:'fa-list-check',        label:'Check PRD controls',   desc:'pass/fail RLFT/RFTL gates for a tag',         send:false, text:'check build controls for <image>:<tag> before a PRD release'},
    {cat:'inspect', icon:'fa-code-pull-request', label:'Track a PR',           desc:'find the PR & summarize CHG/RMG/RLFT',         send:false, text:'find the deployment PR for <image>:<tag> and summarize its CHG, RMG and RLFT controls'},
    {cat:'ops',     icon:'fa-images',            label:'List allowed images',  desc:'what I can promote',                          send:true,  text:'what images can I promote?'},
    {cat:'ops',     icon:'fa-clock-rotate-left', label:'Recent workflow runs', desc:'status of the latest runs',                   send:true,  text:'show me the 5 most recent workflow runs and their status'},
    {cat:'ops',     icon:'fa-rotate',            label:'Re-run a step',        desc:'re-run apply or dispatch',                    send:false, text:'re-run dispatch_workflow'},
];

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
    title.className = 'mb-1 text-slate-400 text-xs';
    title.textContent = 'Pick one, or just type — hover for details:';
    wrap.appendChild(title);

    CATEGORIES.forEach(cat => {
        const caps = CAPABILITIES.filter(c => c.cat === cat.key);
        if (!caps.length) return;

        const head = document.createElement('div');
        head.className = 'mt-2.5 mb-1.5 flex items-center gap-2';
        head.innerHTML =
            '<i class="fa-solid ' + cat.icon + ' ' + cat.accent + ' text-[10px]"></i>' +
            '<span class="text-[10px] uppercase tracking-widest font-semibold text-slate-400">' + cat.label + '</span>' +
            '<span class="flex-1 border-t border-slate-700/50"></span>';
        wrap.appendChild(head);

        const row = document.createElement('div');
        row.className = 'flex flex-wrap gap-1.5';
        caps.forEach(c => {
            const btn = document.createElement('button');
            btn.title = c.desc;
            btn.className = 'border border-slate-700/80 hover:border-emerald-400/40 ' +
                'hover:bg-slate-800/60 rounded-full px-3 py-1 text-xs text-slate-300 ' +
                'flex items-center gap-1.5 transition-colors';
            btn.innerHTML = '<i class="fa-solid ' + c.icon + ' ' + cat.accent + ' text-[11px]"></i>' + c.label;
            btn.addEventListener('click', () => c.form ? showDeployForm(c.form) : runQuick(c.text, c.send));
            row.appendChild(btn);
        });
        wrap.appendChild(row);
    });

    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
}

// ---- Command palette -----------------------------------------------------
// Grouping comes from each capability's explicit `cat` (same data as the
// in-chat catalog) — no label heuristics.
const CATEGORY_ORDER = CATEGORIES.map(c => c.label);
function paletteActions() {
    const labelByKey = Object.fromEntries(CATEGORIES.map(c => [c.key, c.label]));
    return CAPABILITIES.map(c => ({
        label: c.label, desc: c.desc, icon: c.icon,
        category: labelByKey[c.cat] || 'Utilities',
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
        '<div class="glass rounded-2xl w-full max-w-lg overflow-hidden border border-slate-700">' +
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
            h.className = 'px-4 pt-2 pb-0.5 text-[10px] uppercase tracking-wide text-slate-500';
            h.textContent = a.category;
            list.appendChild(h);
        }
        const row = document.createElement('button');
        row.className = 'w-full text-left px-4 py-1.5 flex items-center justify-between gap-3 ' +
            (idx === _palSelected ? 'bg-emerald-500/15' : 'hover:bg-slate-800/70');
        row.innerHTML = '<span class="text-sm text-slate-200"><i class="fa-solid ' + a.icon +
            ' text-emerald-400 mr-2 w-4 text-center"></i>' + a.label + '</span>' +
            '<span class="text-[11px] text-slate-500">' + a.desc + '</span>';
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
