import { escapeHtml as esc, shortName, timeAgo } from '../core/format.js';
import { queueDestination, requeueRows } from '../core/queue.js';
import { getContext, HISTORY_PATH, queueBatch } from '../api.js';
import { loadReleaseStatus } from '../status.js';
import { ctxNote, lockToSignedIn, opening, withDismiss } from './common.js';
import { showQueueTable } from './queue_table.js';

// ---- Release history ---------------------------------------------------------
// Past releases, newest first, each with the charts it shipped — read from the
// event log's 'released' events, joined to the 'queued' event that carried each
// chart. The point of the table is the tick box: when a release had to be
// redone, tick the charts that must ship again and they go back into the next
// release's queue THROUGH THE SAME GATE as the first time, with the run that
// built them — so eligibility is checked afresh, and a refusal reads like any
// other. Nothing here deploys, releases or edits history.
export async function showReleaseHistory() {
    const _ph = opening('the release history');
    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm history-card';
    _ph.replaceWith(wrap);
    await _render(wrap, null);
    chat.scrollTop = chat.scrollHeight;
}

async function _render(wrap, flash) {
    wrap.innerHTML = '<div class="text-[11px] text-slate-500"><span class="dots"><span></span><span></span>' +
        '<span></span></span> Loading the release history…</div>';
    const ctx = await getContext(HISTORY_PATH, { releases: [] });
    const releases = ctx.releases || [];
    wrap.innerHTML =
        '<div class="mb-2 flex items-center gap-2 pr-6">' +
        '<span class="font-semibold text-sky-300"><i class="fa-solid fa-clock-rotate-left mr-1"></i>Release history</span>' +
        '<span class="text-[11px] text-slate-500">' + (ctx.ok === false ? '' :
            releases.length + ' release' + (releases.length === 1 ? '' : 's') + ' in ' + (ctx.days || 90) + ' days') + '</span>' +
        '<span class="flex-1"></span>' +
        '<button type="button" data-h="refresh" title="Refresh" class="text-slate-400 hover:text-white text-xs">' +
        '<i class="fa-solid fa-rotate-right"></i></button>' +
        '<button type="button" data-h="queue" class="text-[11px] text-emerald-400 hover:text-emerald-300">' +
        '<i class="fa-solid fa-list-ul mr-1"></i>Open the queue</button></div>';
    if (flash) {
        const f = document.createElement('div');
        f.className = 'text-[11px] mb-2 ' + (flash.ok ? 'text-emerald-400' : 'text-amber-400');
        f.innerHTML = flash.html;
        wrap.appendChild(f);
    }
    const note = ctxNote(ctx, 'the release history'); if (note) wrap.appendChild(note);

    if (ctx.ok === false) {
        const d = document.createElement('div');
        d.className = 'text-[11px] text-slate-500';
        d.textContent = ctx.disabled ? 'The history is off — no BigQuery dataset is configured.'
            : 'The history is unavailable: ' + (ctx.error || 'unknown error');
        wrap.appendChild(d);
    } else if (!releases.length) {
        const d = document.createElement('div');
        d.className = 'text-[11px] text-slate-500';
        d.textContent = 'No release has shipped from the queue in this window.';
        wrap.appendChild(d);
    } else {
        const hint = document.createElement('div');
        hint.className = 'text-[11px] text-slate-400 mb-2';
        hint.textContent = 'Tick the charts to put back into the next release. Each goes through the eligibility ' +
            'check again with the run that built it.';
        wrap.appendChild(hint);
        const scroller = document.createElement('div');
        scroller.className = 'overflow-x-auto';
        const th = (t, cls) => '<th class="text-left font-medium px-2 py-1 whitespace-nowrap ' + (cls || '') + '">' + t + '</th>';
        let html = '<table class="w-full text-[11px] history-table"><thead class="text-slate-500 border-b border-slate-700"><tr>' +
            th('') + th('Chart') + th('Destination') + th('JIRA') + th('Build') + th('Queued by') + '</tr></thead><tbody>';
        releases.forEach((rel, ri) => {
            html += '<tr class="bg-slate-900/60"><td colspan="6" class="px-2 py-1.5">' +
                '<span class="font-semibold text-slate-200">' + esc(rel.release_name) + '</span>' +
                (rel.pr_number ? ' <span class="text-slate-500">PR #' + esc(String(rel.pr_number)) + '</span>' : '') +
                ' <span class="text-slate-500">· ' + esc(timeAgo(rel.released_at)) +
                (rel.released_by ? ' by ' + esc(shortName(rel.released_by)) : '') +
                ' · ' + rel.items.length + ' chart' + (rel.items.length === 1 ? '' : 's') + '</span></td></tr>';
            rel.items.forEach((it, ii) => {
                const label = esc(it.artifact_name) + ':' + esc(it.artifact_version || '');
                const why = it.in_queue ? 'Already queued for the next release'
                    : (!it.build_run_url ? 'No build run was recorded when it was queued — queue it by hand with its run' : '');
                html += '<tr class="border-b border-slate-800 align-top" data-item="' + ri + ':' + ii + '">' +
                    '<td class="px-2 py-1.5"><input type="checkbox" data-pick="' + ri + ':' + ii + '"' +
                    (it.requeueable ? '' : ' disabled') + (why ? ' title="' + esc(why) + '"' : '') + '></td>' +
                    '<td class="px-2 py-1.5 font-mono text-slate-200 whitespace-nowrap">' + label +
                    (it.in_queue ? ' <span class="font-sans text-emerald-400">queued again</span>' : '') + '</td>' +
                    '<td class="px-2 py-1.5 text-slate-300 whitespace-nowrap">' + esc(queueDestination(it)) + '</td>' +
                    '<td class="px-2 py-1.5 text-amber-300/80 whitespace-nowrap">' + esc(it.jira_ticket || '—') + '</td>' +
                    '<td class="px-2 py-1.5 whitespace-nowrap">' + (it.build_run_url
                        ? '<a href="' + esc(it.build_run_url) + '" target="_blank" rel="noopener" class="text-sky-400 hover:underline">run</a>'
                        : '<span class="text-slate-500" title="' + esc(why) + '">no run recorded</span>') + '</td>' +
                    '<td class="px-2 py-1.5 text-slate-400 whitespace-nowrap">' + esc(shortName(it.queued_by) || '—') + '</td></tr>';
            });
        });
        html += '</tbody></table>';
        scroller.innerHTML = html;
        wrap.appendChild(scroller);

        // The action row: who is asking (recorded against the verified user when
        // signed in), and one button for every tick.
        const act = document.createElement('div');
        act.className = 'mt-3 flex flex-wrap items-center gap-2';
        act.innerHTML = '<input type="email" placeholder="your email" class="h-email bg-slate-900 border border-slate-700 ' +
            'rounded-lg px-2 py-1 text-xs text-white focus:outline-none w-52">' +
            '<button type="button" class="h-go bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-xs rounded-lg px-3 py-1" disabled>' +
            '<i class="fa-solid fa-cart-plus mr-1"></i>Add selected to the next release</button>' +
            '<span class="h-err text-amber-400 text-[11px]"></span>';
        wrap.appendChild(act);
        const email = act.querySelector('.h-email'), go = act.querySelector('.h-go'), err = act.querySelector('.h-err');
        try { email.value = localStorage.getItem('queue_email') || ''; } catch (e) {}
        lockToSignedIn(email);
        const picked = () => [...scroller.querySelectorAll('input[data-pick]:checked')]
            .map(cb => { const [ri, ii] = cb.dataset.pick.split(':').map(Number); return { rel: releases[ri], it: releases[ri].items[ii] }; });
        scroller.addEventListener('change', () => {
            const n = picked().length;
            go.disabled = !n;
            go.innerHTML = '<i class="fa-solid fa-cart-plus mr-1"></i>Add ' + (n || 'selected') + ' to the next release';
        });
        go.addEventListener('click', async () => {
            const who = email.value.trim();
            if (!who.includes('@')) { err.textContent = 'Your email is needed — the queue records who asked.'; email.focus(); return; }
            const chosen = picked();
            const { rows, skipped } = requeueRows(chosen.map(c => c.it));
            if (!rows.length) { err.textContent = skipped.map(s => s.artifact + ': ' + s.reason).join('; '); return; }
            if (!email.dataset.signedIn) { try { localStorage.setItem('queue_email', who); } catch (e) {} }
            go.disabled = true; go.textContent = rows.length > 1 ? 'Checking ' + rows.length + ' builds…' : 'Checking the build…';
            err.textContent = '';
            const from = [...new Set(chosen.map(c => c.rel.release_name))].join(', ');
            let res = null;
            try { res = await queueBatch({ requested_by: who, change_details: 'Re-queued from ' + from, rows }); }
            catch (e) { res = { ok: false, error: String((e && e.message) || e) }; }
            const queued = (res && res.queued) || [], refused = (res && res.refused) || [];
            if (!res || (!queued.length && !refused.length)) {
                go.disabled = false; go.innerHTML = '<i class="fa-solid fa-cart-plus mr-1"></i>Add selected to the next release';
                err.textContent = (res && res.error) || 'Could not queue — try again.';
                return;
            }
            loadReleaseStatus(true);
            const lines = queued.map(q => '<div>✅ ' + esc(q.artifact) + ' is back in the next release.</div>')
                .concat(refused.map(r => '<div>❌ ' + esc(r.artifact) + ' — ' + esc(r.error || 'not eligible') + '</div>'))
                .concat(skipped.map(s => '<div>⏭ ' + esc(s.artifact) + ' — ' + esc(s.reason) + '</div>'));
            _render(wrap, { ok: !refused.length, html: lines.join('') });
        });
    }
    wrap.querySelector('[data-h="refresh"]').addEventListener('click', () => _render(wrap, null));
    wrap.querySelector('[data-h="queue"]').addEventListener('click', () => showQueueTable());
    withDismiss(wrap);
}
