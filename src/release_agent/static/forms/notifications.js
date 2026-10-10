import { escapeHtml as esc, timeAgo } from '../core/format.js';
import { DEFAULT_DAYS, dotStyle, emptyText, groupItems, kindLabel } from '../core/notify.js';
import { opening, withDismiss } from './common.js';

// ---- Notifications card ("See all") --------------------------------------------
// Everything the bell holds for the last fortnight, as a card in the chat: the
// whole body of each item, not the one line the dropdown shows. The bell owns
// the data and the clicks — what to fetch, how to mark, where a link goes come
// in as `deps`, so this module renders and decides nothing twice.
//
// deps: {fetch(): Promise<answer>, follow(item), dismiss(item): Promise, needsEmail}

export async function showNotificationsCard(deps) {
    const _ph = opening('your notifications');
    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm queue-table-card';
    _ph.replaceWith(wrap);
    await render(wrap, deps);
    withDismiss(wrap);
    chat.scrollTop = chat.scrollHeight;
}

function rowHtml(item, i) {
    const dot = dotStyle(item);
    const unread = item.status === 'unread';
    return '<div class="flex items-start gap-2 py-2 border-t border-slate-800" data-row="' + i + '">' +
        '<span class="w-2 h-2 rounded-full shrink-0" style="margin-top:5px;' + dot.style + '"></span>' +
        '<div class="flex-1 min-w-0">' +
        '<button type="button" data-open="' + i + '" class="text-left text-xs hover:underline ' +
        (unread ? 'text-slate-100 font-semibold' : 'text-slate-300') + '">' + esc(item.title) + '</button>' +
        (item.body ? '<div class="text-[11px] text-slate-400" style="white-space:pre-line">' + esc(item.body) + '</div>' : '') +
        '<div class="text-[10px] text-slate-500 mt-1">' + esc(kindLabel(item.kind)) +
        (item.scope ? ' · ' + esc(item.scope) : '') +
        ' · ' + esc(timeAgo(item.at)) + (unread ? ' · <span class="text-emerald-300">unread</span>' : ' · read') + '</div>' +
        '</div>' +
        '<button type="button" data-drop="' + i + '" title="Dismiss" aria-label="Dismiss" ' +
        'class="text-slate-600 hover:text-red-400 text-xs leading-none" style="margin-top:1px">' +
        '<i class="fa-solid fa-xmark"></i></button></div>';
}

async function render(wrap, deps) {
    wrap.querySelectorAll('.nt-body').forEach(n => n.remove());
    const body = document.createElement('div');
    body.className = 'nt-body';
    body.innerHTML = '<div class="text-[11px] text-slate-500"><span class="dots"><span></span><span></span>' +
        '<span></span></span> Reading your notifications…</div>';
    wrap.appendChild(body);

    let res;
    if (deps.needsEmail) {
        res = { ok: true, needsEmail: true, items: [] };
    } else {
        try { res = await deps.fetch(); } catch (e) { res = { ok: false, error: String((e && e.message) || e) }; }
    }
    if (!res || typeof res !== 'object') res = { ok: false, error: 'no answer' };
    const groups = groupItems(res.items);
    const items = groups.unread.concat(groups.read);

    let html =
        '<div class="mb-1 flex items-center gap-2 pr-6 flex-wrap">' +
        '<span class="font-semibold text-emerald-300"><i class="fa-solid fa-bell mr-1"></i>Notifications</span>' +
        '<span class="text-[11px] text-slate-400">last ' + DEFAULT_DAYS + ' days' +
        (items.length ? ' · ' + items.length + ' item' + (items.length === 1 ? '' : 's') +
            (groups.unread.length ? ', ' + groups.unread.length + ' unread' : '') : '') + '</span>' +
        (res.unverified ? '<span class="text-[10px] text-amber-300/80" title="Not signed in through the gateway: ' +
            'this is the address typed into the bell">unverified</span>' : '') +
        '<span class="flex-1"></span>' +
        '<button type="button" data-nt="refresh" title="Read again now" aria-label="Read again now" ' +
        'class="text-slate-400 hover:text-white text-xs"><i class="fa-solid fa-rotate-right"></i></button></div>';
    if (!items.length) {
        html += '<div class="text-[11px] ' + (res.ok === false ? 'text-amber-400' : 'text-slate-500') + '">' +
            esc(emptyText(res)) + '</div>';
    } else {
        html += '<div class="mt-2">' + items.map(rowHtml).join('') + '</div>';
    }
    body.innerHTML = html;

    body.querySelector('[data-nt="refresh"]').addEventListener('click', () => render(wrap, deps));
    body.querySelectorAll('button[data-open]').forEach(btn => {
        btn.addEventListener('click', () => deps.follow(items[+btn.dataset.open]));
    });
    body.querySelectorAll('button[data-drop]').forEach(btn => {
        btn.addEventListener('click', async () => {
            await deps.dismiss(items[+btn.dataset.drop]);
            render(wrap, deps);
        });
    });
}
