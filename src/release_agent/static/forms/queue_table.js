import { escapeHtml as esc, shortName, timeAgo } from '../core/format.js';
import { queueDestination } from '../core/queue.js';
import { getContext, QUEUE_PATH, withdrawFromQueue } from '../api.js';
import { loadReleaseStatus } from '../status.js';
import { ctxNote, opening, withDismiss } from './common.js';
import { showQueueForm } from './queue_form.js';

// ---- Release queue table --------------------------------------------------
// "Check release queue": what is queued, as a table, each row removable. Read
// straight from /api/release-queue — not a model's summary of it — so the row
// you remove is exactly the row you see. Removing writes a 'withdrawn' event
// (append-only: the history keeps it) and records who removed it; the version
// shown travels with the request, so a chart someone re-queued at a new
// version in the meantime is refused rather than silently taken out.
export async function showQueueTable() {
    const _ph = opening('the release queue');
    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm queue-table-card';
    _ph.replaceWith(wrap);
    wrap.innerHTML = '<div class="text-[11px] text-slate-500"><span class="dots"><span></span><span></span>' +
        '<span></span></span> Loading the release queue…</div>';
    await _renderQueueTable(wrap, null);
    chat.scrollTop = chat.scrollHeight;
}

async function _renderQueueTable(wrap, flash) {
    const ctx = await getContext(QUEUE_PATH, { queue: [] });
    const items = ctx.queue || [];
    wrap.innerHTML =
        '<div class="mb-2 flex items-center gap-2 pr-6">' +
        '<span class="font-semibold text-emerald-300"><i class="fa-solid fa-list-ul mr-1"></i>Release queue</span>' +
        '<span class="text-[11px] text-slate-500">' + (ctx.ok === false ? '' : items.length + ' queued') + '</span>' +
        '<span class="flex-1"></span>' +
        '<button type="button" data-q="refresh" title="Refresh" class="text-slate-400 hover:text-white text-xs">' +
        '<i class="fa-solid fa-rotate-right"></i></button>' +
        '<button type="button" data-q="add" class="text-[11px] text-emerald-400 hover:text-emerald-300">' +
        '<i class="fa-solid fa-plus mr-1"></i>Add to next release</button></div>';

    if (flash) {
        const f = document.createElement('div');
        f.className = 'text-[11px] mb-2 ' + (flash.ok ? 'text-emerald-400' : 'text-amber-400');
        f.innerHTML = '<i class="fa-solid ' + (flash.ok ? 'fa-circle-check' : 'fa-triangle-exclamation') +
            ' mr-1"></i>' + esc(flash.text);
        wrap.appendChild(f);
    }
    const note = ctxNote(ctx, 'the queue'); if (note) wrap.appendChild(note);

    if (ctx.ok === false) {
        const d = document.createElement('div');
        d.className = 'text-[11px] text-slate-500';
        d.textContent = ctx.disabled ? 'The queue is off — no BigQuery dataset is configured.'
            : 'The queue is unavailable: ' + (ctx.error || 'unknown error');
        wrap.appendChild(d);
    } else if (!items.length) {
        const d = document.createElement('div');
        d.className = 'text-[11px] text-slate-500';
        d.textContent = 'Nothing is queued for the next release yet.';
        wrap.appendChild(d);
    } else {
        const scroller = document.createElement('div');
        scroller.className = 'overflow-x-auto';
        const th = (t, cls) => '<th class="text-left font-medium px-2 py-1 whitespace-nowrap ' + (cls || '') + '">' + t + '</th>';
        let html = '<table class="w-full text-[11px] queue-table"><thead class="text-slate-500 border-b border-slate-700"><tr>' +
            th('Chart') + th('Destination') + th('JIRA') + th('Build') + th('Queued by') +
            th('', 'text-right') + '</tr></thead><tbody>';
        items.forEach((q, i) => {
            const label = esc(q.artifact_name) + ':' + esc(q.artifact_version || '');
            const detail = [q.change_details, q.note].filter(Boolean).join(' · ');
            const build = q.build_verified === true
                ? '<span class="text-emerald-400"><i class="fa-solid fa-circle-check mr-1"></i>verified</span>'
                : '<span class="text-amber-400" title="no verified build at queue time">' +
                  '<i class="fa-solid fa-triangle-exclamation mr-1"></i>not verified</span>';
            const run = q.build_run_url
                ? ' <a href="' + esc(q.build_run_url) + '" target="_blank" rel="noopener" ' +
                  'class="text-sky-400 hover:underline" title="The GitHub Actions run that built it">run</a>' : '';
            html += '<tr class="border-b border-slate-800 align-top" data-row="' + i + '">' +
                '<td class="px-2 py-1.5 font-mono text-slate-200 whitespace-nowrap">' + label +
                (detail ? '<div class="font-sans text-slate-500 max-w-[16rem] truncate" title="' + esc(detail) + '">' +
                    esc(detail) + '</div>' : '') + '</td>' +
                '<td class="px-2 py-1.5 text-slate-300 whitespace-nowrap">' + esc(queueDestination(q)) + '</td>' +
                '<td class="px-2 py-1.5 text-amber-300/80 whitespace-nowrap">' + esc(q.jira_ticket || '—') + '</td>' +
                '<td class="px-2 py-1.5 whitespace-nowrap">' + build + run + '</td>' +
                '<td class="px-2 py-1.5 text-slate-400 whitespace-nowrap" title="' +
                    esc((q.requested_by || '') + (q.requested_at ? ' · ' + q.requested_at : '')) + '">' +
                    esc(shortName(q.requested_by) || '—') +
                    '<div class="text-slate-600">' + timeAgo(q.requested_at) + '</div></td>' +
                '<td class="px-2 py-1.5 text-right whitespace-nowrap">' +
                '<button type="button" data-remove="' + i + '" class="text-slate-400 hover:text-red-400" ' +
                'title="Remove from the next release"><i class="fa-solid fa-trash-can mr-1"></i>Remove</button></td></tr>';
        });
        html += '</tbody></table>';
        scroller.innerHTML = html;
        wrap.appendChild(scroller);

        scroller.querySelectorAll('button[data-remove]').forEach(btn => {
            btn.addEventListener('click', () => _confirmRemove(wrap, scroller, items[+btn.dataset.remove], +btn.dataset.remove));
        });
    }

    wrap.querySelector('[data-q="refresh"]').addEventListener('click', () => _renderQueueTable(wrap, null));
    wrap.querySelector('[data-q="add"]').addEventListener('click', () => showQueueForm());
    withDismiss(wrap);
}

// Removing is a real change to the release — ask once, inline, under the row.
function _confirmRemove(wrap, scroller, q, index) {
    scroller.querySelectorAll('tr.queue-confirm').forEach(r => r.remove());
    const anchor = scroller.querySelector('tr[data-row="' + index + '"]');
    const label = q.artifact_name + ':' + (q.artifact_version || '');
    const tr = document.createElement('tr');
    tr.className = 'queue-confirm';
    tr.innerHTML = '<td colspan="6" class="px-2 py-2 bg-slate-900/60">' +
        '<div class="flex flex-wrap items-center gap-2">' +
        '<span class="text-slate-300">Remove <b class="font-mono">' + esc(label) + '</b> from the next release? ' +
        '<span class="text-slate-500">It stays in the history.</span></span>' +
        '<input type="email" placeholder="your email" class="q-remove-email bg-slate-900 border border-slate-700 ' +
        'rounded-lg px-2 py-1 text-xs text-white focus:outline-none w-52">' +
        '<button type="button" class="q-remove-go bg-red-600 hover:bg-red-500 text-white text-xs rounded-lg px-3 py-1">Remove</button>' +
        '<button type="button" class="q-remove-cancel text-slate-400 hover:text-white text-xs">Cancel</button>' +
        '<span class="q-remove-err text-amber-400 text-[11px]"></span></div></td>';
    anchor.after(tr);
    const email = tr.querySelector('.q-remove-email');
    try { email.value = localStorage.getItem('queue_email') || ''; } catch (e) {}
    const go = tr.querySelector('.q-remove-go');
    const err = tr.querySelector('.q-remove-err');
    (email.value ? go : email).focus();
    tr.querySelector('.q-remove-cancel').addEventListener('click', () => tr.remove());
    go.addEventListener('click', async () => {
        const who = email.value.trim();
        if (!who.includes('@')) { err.textContent = 'Your email is needed — the removal is recorded against it.'; email.focus(); return; }
        go.disabled = true; go.textContent = 'Removing…'; err.textContent = '';
        let res = null;
        try {
            res = await withdrawFromQueue({ artifact_name: q.artifact_name,
                                            artifact_version: q.artifact_version || '', requested_by: who });
        } catch (e) { res = { ok: false, error: String((e && e.message) || e) }; }
        if (res && res.ok) {
            try { localStorage.setItem('queue_email', who); } catch (e) {}
            loadReleaseStatus(true);
            _renderQueueTable(wrap, { ok: true, text: 'Removed ' + label + ' from the next release.' });
        } else if (res && res.stale) {
            _renderQueueTable(wrap, { ok: false, text: res.error });   // the queue moved on — show it as it is now
        } else {
            go.disabled = false; go.textContent = 'Remove';
            err.textContent = (res && res.error) || 'Could not remove it.';
        }
    });
}
