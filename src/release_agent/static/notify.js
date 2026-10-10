// The notification bell in the header: a badge, a dropdown of the last two
// weeks, and the poll that keeps them fresh. Rules (ordering, the dot, ages,
// where a link goes) are core/notify.js; every call is api.js.
//
// Who the bell is for: the gateway-verified person (whoami). With identity off
// nobody is verified, so the dropdown asks for an email once and remembers it
// in this browser — the routes answer that address and say `unverified`.
//
// Hidden entirely — no DOM — when `notifications` is not in
// window.PORTAL_UI.features: a feature the server would refuse is not shown.
import { getNotifications, markNotifications, readAllNotifications, whoami } from './api.js';
import { escapeHtml as esc } from './core/format.js';
import {
    DEFAULT_DAYS, POLL_MS, age, badgeText, bodyLine, checkedText, dotStyle, emptyText, groupItems, routeLink,
    shouldPoll,
} from './core/notify.js';
import { sendMessage } from './chat.js';
import { showQueueTable } from './forms/queue_table.js';
import { showAutoTriage } from './forms/auto_triage.js';
import { showNotificationsCard } from './forms/notifications.js';

const EMAIL_KEY = 'notify_email';
const NAV_CLASS = 'px-3 py-1 glass navbtn hover:border-emerald-400/30 rounded-lg text-xs flex items-center gap-2';

const state = {
    slot: null, signedIn: false, email: '', items: [], unread: 0, checkedAt: 0, lastRes: null,
    open: false, poll: null, ticker: null,
};

export function notificationsOn(ui) {
    return Array.isArray(ui && ui.features) && ui.features.includes('notifications');
}

// ---- identity -----------------------------------------------------------------
async function resolveEmail() {
    const who = await whoami();
    if (who && who.signed_in && who.email) {
        state.signedIn = true;
        state.email = who.email;
        return;
    }
    state.signedIn = false;
    try { state.email = localStorage.getItem(EMAIL_KEY) || ''; } catch (e) { state.email = ''; }
}

function emailParams() {
    // The verified caller is read server-side from the request; a typed email
    // is sent only when there is no sign-in to read.
    return state.signedIn || !state.email ? {} : { email: state.email };
}

// ---- data ----------------------------------------------------------------------
export async function refreshNotifications() {
    await resolveEmail();
    if (!state.signedIn && !state.email) {
        state.lastRes = { ok: true, needsEmail: true };
        state.items = [];
        state.unread = 0;
        render();
        return;
    }
    let res;
    try {
        res = await getNotifications(Object.assign({ since_days: DEFAULT_DAYS }, emailParams()));
    } catch (e) {
        res = { ok: false, error: String((e && e.message) || e) };
    }
    if (res && res.status === 403) {
        // Refused server-side after all (the preview list changed under a
        // running page): a bell that cannot be read is not shown.
        unmount();
        return;
    }
    state.lastRes = res;
    state.items = Array.isArray(res.items) ? res.items : [];
    state.unread = Number(res.unread) || 0;
    state.checkedAt = Date.now();
    render();
}

async function mark(subjects, status) {
    try {
        await markNotifications(Object.assign({ subjects, status }, emailParams()));
    } catch (e) { /* a mark that failed shows as still unread on the next poll */ }
}

async function readAll() {
    try { await readAllNotifications(emailParams()); } catch (e) { /* same */ }
    await refreshNotifications();
}

// ---- where a click goes ----------------------------------------------------------
export function followLink(link) {
    const route = routeLink(link);
    if (route.kind === 'queue') showQueueTable();
    else if (route.kind === 'auto-triage') showAutoTriage({ date: route.date, incident: route.incidentId });
    else if (route.kind === 'chat') sendMessage(route.message);
    else if (route.kind === 'url') window.open(route.url, '_blank', 'noopener');
    return route.kind;
}

// ---- polling ---------------------------------------------------------------------
// Only while the tab is visible: a hidden tab stops, and a tab coming back
// (visibilitychange, focus) refreshes at once rather than waiting out the minute.
function startPolling() {
    if (state.poll) return;
    state.poll = setInterval(() => {
        if (shouldPoll(document.visibilityState)) refreshNotifications();
    }, POLL_MS);
}

function stopPolling() {
    if (state.poll) clearInterval(state.poll);
    state.poll = null;
}

function onVisibility() {
    if (shouldPoll(document.visibilityState)) {
        refreshNotifications();
        startPolling();
    } else {
        stopPolling();
    }
}

// ---- rendering --------------------------------------------------------------------
function rowHtml(item) {
    const dot = dotStyle(item);
    const unread = item.status === 'unread';
    return '<div class="flex items-start gap-2 px-3 py-2 hover:bg-slate-800/60 cursor-pointer" data-subject="' +
        esc(item.subject) + '" title="' + esc(item.link ? 'Open' : '') + '">' +
        '<span class="w-2 h-2 rounded-full shrink-0" style="margin-top:5px;' + dot.style + '"></span>' +
        '<div class="flex-1 min-w-0">' +
        '<div class="text-xs ' + (unread ? 'text-slate-100 font-semibold' : 'text-slate-300') + '">' + esc(item.title) + '</div>' +
        (item.body ? '<div class="text-[11px] text-slate-400 truncate">' + esc(bodyLine(item.body)) + '</div>' : '') +
        '</div>' +
        '<span class="text-[10px] text-slate-500 whitespace-nowrap" style="margin-top:2px">' + esc(age(item.at)) + '</span>' +
        '<button type="button" data-dismiss="' + esc(item.subject) + '" title="Dismiss" aria-label="Dismiss" ' +
        'class="text-slate-600 hover:text-red-400 text-xs leading-none" style="margin-top:1px">' +
        '<i class="fa-solid fa-xmark"></i></button></div>';
}

function listHtml() {
    const res = state.lastRes;
    const groups = groupItems(state.items);
    if (!res || res.ok === false || res.needsEmail || (!groups.unread.length && !groups.read.length)) {
        return '<div class="px-3 py-4 text-[11px] text-slate-500 text-center">' + esc(emptyText(res)) + '</div>';
    }
    let html = groups.unread.map(rowHtml).join('');
    if (groups.read.length) {
        html += '<div class="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wider text-slate-600' +
            (groups.unread.length ? ' border-t border-slate-800' : '') + '">Earlier</div>' +
            groups.read.map(rowHtml).join('');
    }
    return html;
}

function emailHtml() {
    if (state.signedIn) return '';
    return '<div class="px-3 py-2 border-b border-slate-800 flex items-center gap-2">' +
        '<span class="text-[10px] text-slate-500 whitespace-nowrap">Your email</span>' +
        '<input type="email" data-notify="email" value="' + esc(state.email) + '" placeholder="you@example.com" ' +
        'class="flex-1 min-w-0 bg-slate-900 border border-slate-700 rounded px-2 py-0.5 text-[11px] text-slate-200" ' +
        'title="Not signed in through the gateway: notifications for this address, remembered in this browser"></div>';
}

function render() {
    const slot = state.slot;
    if (!slot) return;
    const badge = slot.querySelector('[data-notify="badge"]');
    const text = badgeText(state.unread);
    badge.textContent = text;
    badge.style.display = text ? '' : 'none';
    const btn = slot.querySelector('[data-notify="bell"]');
    btn.title = state.unread ? state.unread + ' unread notification' + (state.unread === 1 ? '' : 's') : 'Notifications';
    if (!state.open) return;
    slot.querySelector('[data-notify="list"]').innerHTML = listHtml();
    const emailBox = slot.querySelector('[data-notify="emailbox"]');
    emailBox.innerHTML = emailHtml();
    const input = emailBox.querySelector('[data-notify="email"]');
    if (input) {
        input.addEventListener('change', () => {
            state.email = input.value.trim();
            try { localStorage.setItem(EMAIL_KEY, state.email); } catch (e) { /* private mode */ }
            refreshNotifications();
        });
    }
    renderChecked();
    slot.querySelector('[data-notify="readall"]').style.display = state.unread ? '' : 'none';
}

function renderChecked() {
    const el = state.slot && state.slot.querySelector('[data-notify="checked"]');
    if (el) el.textContent = checkedText(state.checkedAt);
}

function setOpen(open) {
    state.open = open;
    const panel = state.slot.querySelector('[data-notify="panel"]');
    panel.classList.toggle('hidden', !open);
    if (state.ticker) clearInterval(state.ticker);
    state.ticker = null;
    if (open) {
        render();
        state.ticker = setInterval(renderChecked, 5000);
        refreshNotifications();
    }
}

function unmount() {
    stopPolling();
    if (state.ticker) clearInterval(state.ticker);
    document.removeEventListener('visibilitychange', onVisibility);
    window.removeEventListener('focus', refreshNotifications);
    if (state.slot) {
        state.slot.innerHTML = '';
        state.slot.classList.add('hidden');
    }
    state.slot = null;
}

// ---- mount ------------------------------------------------------------------------
export function mountNotifyBell() {
    if (!notificationsOn(window.PORTAL_UI)) return;
    const slot = document.getElementById('notify-slot');
    if (!slot) return;
    state.slot = slot;
    slot.classList.remove('hidden');
    slot.innerHTML =
        '<button type="button" data-notify="bell" title="Notifications" aria-label="Notifications" class="' + NAV_CLASS + ' relative">' +
        '<i class="fa-solid fa-bell text-emerald-400"></i>' +
        '<span data-notify="badge" style="display:none;position:absolute;top:-6px;right:-6px;min-width:16px;height:16px;' +
        'padding:0 4px;border-radius:999px;background:#ef4444;color:#fff;font-size:10px;line-height:16px;text-align:center;' +
        'font-weight:600"></span></button>' +
        '<div data-notify="panel" class="hidden absolute z-50 rounded-xl border border-slate-700 text-left" ' +
        'style="right:0;top:calc(100% + 6px);width:22rem;max-width:calc(100vw - 2rem);background:rgba(15,23,42,.97);' +
        'box-shadow:0 20px 40px rgba(0,0,0,.45)">' +
        '<div class="flex items-center gap-2 px-3 py-2 border-b border-slate-800">' +
        '<span class="text-xs font-semibold text-slate-200"><i class="fa-solid fa-bell text-emerald-400 mr-1"></i>Notifications</span>' +
        '<span class="flex-1"></span>' +
        '<button type="button" data-notify="readall" class="text-[11px] text-sky-400 hover:underline">Mark all read</button>' +
        '</div>' +
        '<div data-notify="emailbox"></div>' +
        '<div data-notify="list" class="overflow-y-auto" style="max-height:60vh"></div>' +
        '<div class="flex items-center gap-2 px-3 py-2 border-t border-slate-800 text-[10px] text-slate-500">' +
        '<span data-notify="checked"></span><span class="flex-1"></span>' +
        '<button type="button" data-notify="seeall" class="text-sky-400 hover:underline">See all</button>' +
        '</div></div>';

    slot.querySelector('[data-notify="bell"]').addEventListener('click', (e) => {
        e.stopPropagation();
        setOpen(!state.open);
    });
    slot.querySelector('[data-notify="readall"]').addEventListener('click', readAll);
    slot.querySelector('[data-notify="seeall"]').addEventListener('click', () => {
        setOpen(false);
        showNotificationsCard({
            fetch: () => getNotifications(Object.assign({ since_days: DEFAULT_DAYS }, emailParams())),
            follow: (item) => { mark([item.subject], 'read').then(refreshNotifications); followLink(item.link); },
            dismiss: (item) => mark([item.subject], 'dismissed').then(refreshNotifications),
            needsEmail: !state.signedIn && !state.email,
        });
    });
    slot.querySelector('[data-notify="list"]').addEventListener('click', (e) => {
        const dismiss = e.target.closest('[data-dismiss]');
        if (dismiss) {
            e.stopPropagation();
            mark([dismiss.dataset.dismiss], 'dismissed').then(refreshNotifications);
            return;
        }
        const row = e.target.closest('[data-subject]');
        if (!row) return;
        const item = state.items.find(i => i.subject === row.dataset.subject);
        if (!item) return;
        // Read first, then go: the mark is best-effort and the click must not
        // wait on it.
        mark([item.subject], 'read').then(refreshNotifications);
        setOpen(false);
        followLink(item.link);
    });
    // Clicks inside the panel stay inside; one anywhere else closes it.
    slot.querySelector('[data-notify="panel"]').addEventListener('click', (e) => e.stopPropagation());
    document.addEventListener('click', () => { if (state.open) setOpen(false); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && state.open) setOpen(false); });

    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('focus', refreshNotifications);
    refreshNotifications();
    if (shouldPoll(document.visibilityState)) startPolling();
}
