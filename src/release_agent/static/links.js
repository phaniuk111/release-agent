// Console links strip: the read-only "where do I go to LOOK at this" row —
// per environment, the GKE workload view and that environment's dashboards.
//
// These are plain hyperlinks, deliberately NOT capability pills: a pill sends a
// message to the agent, this leaves the portal. Keeping them in their own strip
// (outside #chat) means they survive New Thread and stay put while chat scrolls.
//
// ONE environment is shown at a time, chosen by the tabs on the left. Environments
// carry several dashboards each, so showing them all at once would grow the strip
// with every dashboard added; switching keeps it a single line at any size. The
// choice is remembered because a given person watches one environment most days.
//
// Every URL is config (CONSOLE_LINKS) and ships EMPTY. An unset link still
// renders — as a dead chip whose tooltip names the variable — so the strip
// documents what is left to fill in instead of silently disappearing.
import { API_BASE } from './state.js';
import { escapeHtml as esc } from './chat.js';

const ENV_KEY = 'console_env';

let _envs = [];
let _envVar = 'CONSOLE_LINKS';
let _selected = '';

function _label(text, cls) {
    const el = document.createElement('span');
    el.className = cls;
    el.textContent = text;
    return el;
}

// The env tabs. Rendered as buttons, not links: switching changes what this
// strip shows, it does not navigate anywhere.
function _tab(env) {
    const on = env.name === _selected;
    const btn = document.createElement('button');
    btn.className = 'rounded-full px-2.5 py-0.5 transition-colors ' + (on
        ? 'bg-slate-800 text-slate-100 border border-slate-600'
        : 'border border-transparent text-slate-500 hover:text-slate-300');
    btn.textContent = env.name;
    const n = env.links.filter(l => l.configured).length;
    btn.title = n
        ? n + ' link' + (n === 1 ? '' : 's') + ' for ' + env.name
        : env.name + ' has no links configured yet';
    btn.addEventListener('click', () => _select(env.name));
    return btn;
}

function _chip(link, envName) {
    const inner = '<i class="fa-solid ' + esc(link.icon) + ' text-[11px]"></i>' + esc(link.label);
    if (!link.configured) {
        // A span, not an <a> — an anchor with no href is still focusable and
        // reads as a link to a screen reader, which this is not (yet).
        const dead = document.createElement('span');
        dead.className = 'border border-dashed border-slate-700/70 rounded-full px-2.5 py-0.5 ' +
            'text-slate-600 flex items-center gap-1.5 cursor-help';
        dead.title = 'No URL for ' + envName + ' → ' + link.label +
            ' — set it in ' + _envVar + ' (env or Helm values.yaml config:)';
        dead.innerHTML = inner + '<i class="fa-solid fa-link-slash text-[9px] opacity-70"></i>';
        return dead;
    }
    const a = document.createElement('a');
    a.href = link.url;
    a.target = '_blank';
    // noopener: the opened console must not get a handle on this window.
    a.rel = 'noopener noreferrer';
    a.title = link.url;
    a.className = 'border border-slate-700/80 hover:border-sky-400/50 hover:bg-slate-800/60 ' +
        'rounded-full px-2.5 py-0.5 text-slate-300 flex items-center gap-1.5 transition-colors';
    a.innerHTML = inner + '<i class="fa-solid fa-arrow-up-right-from-square text-[9px] text-slate-500"></i>';
    return a;
}

function _select(name) {
    _selected = name;
    try { localStorage.setItem(ENV_KEY, name); } catch (e) {}
    _paint();
}

function _paint() {
    const row = document.getElementById('cl-row');
    if (!row) return;
    row.innerHTML = '';
    row.appendChild(_label('Consoles', 'text-[10px] uppercase tracking-wider text-slate-500 shrink-0'));

    const tabs = document.createElement('div');
    tabs.className = 'flex items-center gap-0.5 text-xs shrink-0';
    _envs.forEach(env => tabs.appendChild(_tab(env)));
    row.appendChild(tabs);

    const env = _envs.find(e => e.name === _selected) || _envs[0];
    const links = document.createElement('div');
    links.className = 'flex flex-wrap items-center gap-1.5';
    if (!env.links.length) {
        links.appendChild(_label('No links defined for ' + env.name + ' in ' + _envVar,
            'text-slate-600 italic'));
    } else {
        env.links.forEach(link => links.appendChild(_chip(link, env.name)));
    }
    row.appendChild(links);
}

export async function renderConsoleLinks() {
    const box = document.getElementById('console-links');
    if (!box || !document.getElementById('cl-row')) return;
    let data;
    try {
        const res = await fetch(API_BASE + '/api/console-links');
        data = await res.json();
    } catch (e) {
        return;                 // decoration, not a feature — a failed fetch stays silent
    }
    _envs = (data.envs || []).filter(e => e && e.name);
    if (!_envs.length) return;
    _envVar = data.env_var || _envVar;

    // Restore the last environment, but only if it still exists — an env renamed
    // or dropped from CONSOLE_LINKS must not leave the strip stuck on nothing.
    let saved = '';
    try { saved = localStorage.getItem(ENV_KEY) || ''; } catch (e) {}
    _selected = _envs.some(e => e.name === saved) ? saved : _envs[0].name;

    _paint();
    box.classList.remove('hidden');
}
