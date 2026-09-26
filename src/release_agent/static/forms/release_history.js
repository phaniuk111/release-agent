import { escapeHtml as esc, shortName, timeAgo } from '../core/format.js';
import {
    filterReleases, itemKind, itemState, missingInputs, releaseSummary, requeueAllIndices, safeRunHref, withTyped,
} from '../core/history.js';
import { queueDestination, requeuePlan } from '../core/queue.js';
import { getContext, HISTORY_PATH, queueBatch, requeueFromHistory } from '../api.js';
import { loadReleaseStatus } from '../status.js';
import { ctxNote, lockToSignedIn, opening, withDismiss } from './common.js';
import { showQueueTable } from './queue_table.js';

// ---- Release history ---------------------------------------------------------
// Past releases, newest first, each with the charts it shipped — read from the
// event log's 'released' events, joined to the 'queued' event that carried each
// chart. The point of the card is the tick box: when a release had to be
// redone, tick the charts that must ship again and they go straight back into
// the next release's queue: each qualified once, at that version, and the run
// it was verified against has not changed, so the build/controls gate is not
// run again. Only a chart that never went through the queue — typed straight
// into a release form — takes the gate, with a run and a ticket given here.
// Nothing here deploys, releases or edits history.
const HISTORY_DAYS = 21;

const KINDS = [['all', 'All'], ['CARE', 'CARE'], ['DF', 'DF']];
const CHIP_ON = 'bg-slate-800 text-slate-100 border border-slate-600';
const CHIP_OFF = 'border border-transparent text-slate-500 hover:text-slate-300';
const ROW_INPUT = 'bg-slate-900 border border-amber-600 rounded px-2 py-0.5 text-[11px] text-white ' +
    'placeholder-slate-500 focus:outline-none';
const ADD_ICON = '<i class="fa-solid fa-cart-plus mr-1"></i>';

// Two history cards can be open in one chat; the ids that tie a release header
// to its rows (aria-controls) must not collide between them.
let _cards = 0;

export async function showReleaseHistory() {
    const _ph = opening('the release history');
    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm history-card';
    _ph.replaceWith(wrap);
    await _render(wrap, null, null);
    chat.scrollTop = chat.scrollHeight;
}

// `keep` carries the search and the kind filter across a refresh. Ticks, typed
// values and open releases are keyed by position, and a refresh can add a
// release at the top — so those start over.
async function _render(wrap, flash, keep) {
    wrap.innerHTML = '<div class="text-[11px] text-slate-500"><span class="dots"><span></span><span></span>' +
        '<span></span></span> Loading the release history…</div>';
    // Three weeks: a release that has to be redone is days old, not months —
    // and a shorter window is a smaller BigQuery scan on every open.
    const ctx = await getContext(HISTORY_PATH + '?days=' + HISTORY_DAYS, { releases: [] });
    const releases = ctx.releases || [];
    // Everything the person has done on this card lives here, not in the DOM:
    // the release list is rebuilt on every keystroke of the search.
    const view = {
        query: (keep && keep.query) || '', kind: (keep && keep.kind) || 'all',
        open: { 0: true }, searchClosed: {}, picks: {}, typed: {},
    };
    wrap.innerHTML =
        '<div class="mb-2 flex flex-wrap items-center gap-2 pr-6">' +
        '<span class="font-semibold text-sky-300 whitespace-nowrap"><i class="fa-solid fa-clock-rotate-left mr-1"></i>Release history</span>' +
        '<span class="text-[11px] text-slate-500">' + (ctx.ok === false ? '' :
            releases.length + ' release' + (releases.length === 1 ? '' : 's') + ' in the last 3 weeks') + '</span>' +
        '<span class="flex-1"></span>' +
        '<button type="button" data-h="refresh" title="Refresh" class="text-slate-400 hover:text-white text-xs">' +
        '<i class="fa-solid fa-rotate-right"></i></button>' +
        '<button type="button" data-h="queue" class="text-[11px] text-emerald-400 hover:text-emerald-300 whitespace-nowrap">' +
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
        d.textContent = 'No release has shipped from the queue in the last 3 weeks.';
        wrap.appendChild(d);
    } else {
        _mountReleases(wrap, releases, view);
    }
    wrap.querySelector('[data-h="refresh"]').addEventListener('click', () => _render(wrap, null, view));
    wrap.querySelector('[data-h="queue"]').addEventListener('click', () => showQueueTable());
    withDismiss(wrap);
}

function _mountReleases(wrap, releases, view) {
    const uid = ++_cards;
    const key = (ri, ii) => ri + ':' + ii;
    const itemsOf = (rel) => (rel && rel.items) || [];

    // ---- search + kind chips (rendered once; only the list below re-renders) --
    const bar = document.createElement('div');
    bar.className = 'mb-2 flex flex-wrap items-center gap-2';
    const kindCount = (k) => releases.reduce((n, rel) =>
        n + itemsOf(rel).filter(it => k === 'all' || itemKind(it) === k).length, 0);
    bar.innerHTML =
        '<input type="search" data-h="search" placeholder="Search chart or ticket" aria-label="Search chart or ticket" ' +
        'class="flex-1 min-w-0 bg-slate-900 border border-slate-700 rounded-lg px-2 py-1 text-xs text-white ' +
        'placeholder-slate-500 focus:outline-none">' +
        '<div class="flex items-center gap-1 text-[11px]" role="group" aria-label="Show which charts">' +
        KINDS.map(([k, label]) => {
            const n = kindCount(k);
            const what = k === 'all' ? 'Every chart' : (k === 'DF' ? 'Dataflow images only' : 'CARE charts only');
            return '<button type="button" data-kind="' + k + '" title="' + esc(what + ' — ' + n + ' in the last 3 weeks') +
                '" class="rounded-full px-2.5 py-0.5 transition-colors whitespace-nowrap">' + esc(label) +
                ' <span class="text-slate-500">' + n + '</span></button>';
        }).join('') + '</div>';
    wrap.appendChild(bar);
    const search = bar.querySelector('[data-h="search"]');
    search.value = view.query;
    const paintChips = () => bar.querySelectorAll('button[data-kind]').forEach(b => {
        const on = b.dataset.kind === view.kind;
        b.className = 'rounded-full px-2.5 py-0.5 transition-colors whitespace-nowrap ' + (on ? CHIP_ON : CHIP_OFF);
        b.setAttribute('aria-pressed', String(on));
    });
    paintChips();

    const hint = document.createElement('div');
    hint.className = 'text-[11px] text-slate-400 mb-2';
    hint.textContent = 'Tick the charts to put back into the next release, or use “Re-queue all” on a release to tick ' +
        'every one that can go. A chart that came through the queue goes straight back — it already qualified; ' +
        'one that never did needs its run and ticket.';
    wrap.appendChild(hint);

    const list = document.createElement('div');
    list.className = 'space-y-2 text-[11px]';
    wrap.appendChild(list);

    // ---- the action row: who is asking (recorded against the verified user
    // when signed in), and one button for every tick ------------------------
    const act = document.createElement('div');
    act.className = 'mt-2 flex flex-wrap items-center gap-2';
    act.innerHTML = '<input type="email" placeholder="your email" aria-label="Your email" class="h-email bg-slate-900 ' +
        'border border-slate-700 rounded-lg px-2 py-1 text-xs text-white focus:outline-none w-52">' +
        '<button type="button" class="h-go bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-xs ' +
        'rounded-lg px-3 py-1" disabled>' + ADD_ICON + 'Add selected to the next release</button>' +
        '<span class="h-err text-amber-400 text-[11px]"></span>';
    wrap.appendChild(act);
    const email = act.querySelector('.h-email'), go = act.querySelector('.h-go'), err = act.querySelector('.h-err');
    try { email.value = localStorage.getItem('queue_email') || ''; } catch (e) {}
    lockToSignedIn(email);

    // ---- rules over the state ---------------------------------------------
    // What the gate will see for a row: the record, with anything typed over it.
    const merged = (ri, ii) => withTyped(itemsOf(releases[ri])[ii], view.typed[key(ri, ii)]);
    const ready = (it) => itemState(it) === 'ready';
    const missingWhy = (it) => {
        const miss = missingInputs(it);
        if (miss.length === 2) return 'Never went through the queue — paste the run that built it and its JIRA ticket, then tick';
        if (miss[0] === 'run') return 'Never went through the queue — paste the GitHub Actions run that built it (…/actions/runs/<id>), then tick';
        return 'Never went through the queue — add its JIRA ticket, then tick';
    };
    const tickedKeys = () => Object.keys(view.picks).filter(k => view.picks[k]);
    const searching = () => !!view.query.trim();
    // While searching every hit is shown open; the person's own open/closed
    // choices are kept apart so clearing the search puts them back.
    const isOpen = (ri) => (searching() ? !view.searchClosed[ri] : !!view.open[ri]);
    let visible = new Set();
    let busy = false;

    const updateGo = () => {
        if (busy) return;
        const keys = tickedKeys();
        const hidden = keys.filter(k => !visible.has(k)).length;
        go.disabled = !keys.length;
        go.innerHTML = ADD_ICON + (keys.length
            ? 'Add ' + keys.length + ' to the next release' + (hidden ? ' (' + hidden + ' hidden by the filter)' : '')
            : 'Add selected to the next release');
    };
    // A collapsed release still says how many of its rows are ticked.
    const tickedText = (ri) => {
        const n = itemsOf(releases[ri]).filter((_, ii) => view.picks[key(ri, ii)]).length;
        return n ? ' · ' + n + ' ticked' : '';
    };
    const paintTicked = (ri) => {
        const el = list.querySelector('[data-ticked="' + ri + '"]');
        if (el) el.textContent = tickedText(ri);
    };

    // ---- rendering ----------------------------------------------------------
    const chip = (text, cls, title) => '<span class="min-w-0 truncate rounded-full px-1.5 text-[10px] ' + cls + '"' +
        (title ? ' title="' + esc(title) + '"' : '') + '>' + esc(text) + '</span>';

    const rowHtml = (ri, ii, first) => {
        const it = itemsOf(releases[ri])[ii];
        const k = key(ri, ii);
        // The record decides the layout; the typed values decide the tick.
        const state = itemState(it);
        const m = merged(ri, ii);
        const ok = ready(m);
        const label = it.artifact_name + ':' + (it.artifact_version || '');
        const detail = [it.change_details, it.note].filter(Boolean).join(' · ');
        const why = state === 'in-queue' ? 'Already queued for the next release' : (ok ? '' : missingWhy(m));
        const origin = it.queued_by ? 'Queued by ' + it.queued_by
            : (!it.from_queue && !it.in_queue
                ? 'Never went through the queue — typed straight into a release form, so the gate never ran for it' : '');
        const rowTitle = [origin, state === 'needs-input' ? detail : ''].filter(Boolean).join('\n');
        const badge = state === 'in-queue' ? chip('in next release', 'bg-emerald-500/15 text-emerald-300')
            : (state === 'needs-input' ? chip('never queued — needs run + ticket', 'bg-amber-500/15 text-amber-300',
                'Typed straight into a release form: the gate never ran for it, and it needs both to run now') : '');

        let line2 = '';
        if (state === 'needs-input') {
            // A chart that never went through the queue has no verified run on
            // record — the gate still needs one and a ticket, so the row takes
            // them here and the tick enables once both are valid.
            const t = view.typed[k] || {};
            const runVal = t.run != null ? t.run : (it.build_run_url || '');
            const jiraVal = t.jira != null ? t.jira : (it.jira_ticket || '');
            line2 = '<div class="mt-1 flex flex-wrap items-center gap-1.5">' +
                '<input type="url" data-run="' + k + '" value="' + esc(runVal) + '" placeholder="paste the run that built it" ' +
                'aria-label="' + esc('Build run URL for ' + label) + '" class="' + ROW_INPUT + ' flex-1">' +
                '<input type="text" data-jira="' + k + '" value="' + esc(jiraVal) + '" placeholder="ticket" ' +
                'aria-label="' + esc('JIRA ticket for ' + label) + '" class="' + ROW_INPUT + ' w-24 uppercase"></div>';
        } else {
            const bits = [];
            if (it.jira_ticket) bits.push('<span class="whitespace-nowrap text-amber-300/80">' + esc(it.jira_ticket) + '</span>');
            const href = safeRunHref(it.build_run_url);
            if (href) {
                bits.push('<a href="' + esc(href) + '" target="_blank" rel="noopener" class="text-sky-400 hover:underline" ' +
                    'title="The GitHub Actions run that built it">run</a>');
            }
            if (detail) bits.push('<span class="min-w-0 truncate" title="' + esc(detail) + '">' + esc(detail) + '</span>');
            if (bits.length) {
                line2 = '<div class="mt-0.5 flex flex-wrap items-center gap-x-3 text-[10px] text-slate-500">' +
                    bits.join('') + '</div>';
            }
        }
        return '<div class="flex items-start gap-2 py-1.5' + (first ? '' : ' border-t border-slate-800') + '" ' +
            'data-item="' + k + '"' + (rowTitle ? ' title="' + esc(rowTitle) + '"' : '') + '>' +
            '<input type="checkbox" data-pick="' + k + '" aria-label="' + esc(label) + '" class="mt-0.5 shrink-0"' +
            (ok ? '' : ' disabled') + (ok && view.picks[k] ? ' checked' : '') + (why ? ' title="' + esc(why) + '"' : '') + '>' +
            '<div class="min-w-0 flex-1">' +
            '<div class="flex flex-wrap items-center gap-1.5">' +
            '<span class="min-w-0 truncate font-mono text-slate-200">' + esc(label) + '</span>' +
            chip(queueDestination(it), 'border border-slate-700 text-slate-400') + badge + '</div>' +
            line2 + '</div></div>';
    };

    const noneWhy = (rel) => (itemsOf(rel).every(it => itemState(it) === 'in-queue')
        ? 'Every chart in this release is already in the next release'
        : 'None can go straight back: a chart that never went through the queue needs its run and ticket first — ' +
          'add them in its row, then tick it');

    const releaseHtml = ({ rel, index: ri, items }) => {
        const open = isOpen(ri);
        // Only rows the current search/filter shows: nothing is ticked out of sight.
        const n = requeueAllIndices(rel, items.map(x => x.index)).length;
        const total = itemsOf(rel).length;
        const meta = [rel.pr_number ? 'PR #' + rel.pr_number : '', timeAgo(rel.released_at),
                      rel.released_by ? 'by ' + shortName(rel.released_by) : ''].filter(Boolean).join(' · ');
        const bodyId = 'history-' + uid + '-' + ri;
        return '<div class="rounded-lg border border-slate-700/60 bg-slate-950/40" data-rel="' + ri + '">' +
            '<div class="flex items-center gap-2 px-2 py-1.5">' +
            '<button type="button" data-toggle="' + ri + '" aria-expanded="' + open + '" aria-controls="' + bodyId + '" ' +
            'class="flex-1 min-w-0 flex items-start gap-1.5 text-left" title="' +
            esc(rel.release_name + (meta ? ' · ' + meta : '')) + '">' +
            '<i class="fa-solid fa-fw ' + (open ? 'fa-chevron-down' : 'fa-chevron-right') +
            ' text-[10px] text-slate-500 mt-1 shrink-0"></i>' +
            '<span class="min-w-0 flex-1">' +
            '<span class="block truncate text-xs"><span class="font-semibold text-slate-200">' + esc(rel.release_name) + '</span>' +
            (meta ? ' <span class="text-slate-500">· ' + esc(meta) + '</span>' : '') + '</span>' +
            '<span class="block truncate text-slate-400">' + esc(releaseSummary(rel)) +
            (items.length < total ? '<span class="text-slate-500"> · ' + items.length + ' of ' + total + ' shown</span>' : '') +
            '<span data-ticked="' + ri + '" class="text-emerald-400">' + esc(tickedText(ri)) + '</span></span>' +
            '</span></button>' +
            // A sibling of the header, not inside it: ticking must not collapse.
            '<button type="button" data-requeue-all="' + ri + '"' + (n ? '' : ' disabled') + ' title="' +
            esc(n ? (n === 1 ? 'Tick the 1 chart that can go straight back — nothing is sent until you add it below'
                : 'Tick the ' + n + ' charts that can go straight back — nothing is sent until you add them below')
                : noneWhy(rel)) + '" class="shrink-0 whitespace-nowrap ' +
            (n ? 'text-emerald-400 hover:text-emerald-300' : 'text-slate-600') + '">' +
            '<i class="fa-solid fa-check-double mr-1"></i>Re-queue all (' + n + ')</button></div>' +
            '<div id="' + bodyId + '" class="border-t border-slate-800 px-2"' + (open ? '' : ' hidden') + '>' +
            items.map((x, j) => rowHtml(ri, x.index, j === 0)).join('') + '</div></div>';
    };

    const renderList = () => {
        const shown = filterReleases(releases, { query: view.query, kind: view.kind });
        visible = new Set();
        shown.forEach(s => s.items.forEach(x => visible.add(key(s.index, x.index))));
        if (shown.length) {
            list.innerHTML = shown.map(releaseHtml).join('');
        } else {
            const q = view.query.trim();
            const what = view.kind === 'all' ? 'chart' : view.kind + ' chart';
            list.innerHTML = '<div class="text-slate-500">' + (q
                ? 'No ' + esc(what) + ' matches “' + esc(q) + '” in the last 3 weeks. ' +
                  '<button type="button" data-clear class="text-sky-400 hover:underline">Clear search</button>'
                : 'No ' + esc(what) + ' shipped in the last 3 weeks. ' +
                  '<button type="button" data-all-kinds class="text-sky-400 hover:underline">Show all</button>') + '</div>';
        }
        updateGo();
    };

    // ---- events ---------------------------------------------------------------
    const clearSearch = () => {
        search.value = '';
        view.query = '';
        view.searchClosed = {};
        renderList();
    };
    search.addEventListener('input', () => {
        view.query = search.value;
        view.searchClosed = {};      // a new query shows every hit open
        renderList();
    });
    // Escape closes the whole card (withDismiss listens on the document); in a
    // search box with text it should only clear the search.
    search.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape' || !search.value) return;
        e.preventDefault();
        e.stopPropagation();
        clearSearch();
    });
    bar.querySelectorAll('button[data-kind]').forEach(b => b.addEventListener('click', () => {
        view.kind = b.dataset.kind;
        paintChips();
        renderList();
    }));

    const toggle = (btn) => {
        const ri = +btn.dataset.toggle;
        const open = !isOpen(ri);
        if (searching()) view.searchClosed[ri] = !open; else view.open[ri] = open;
        btn.setAttribute('aria-expanded', String(open));
        const icon = btn.querySelector('i');
        icon.classList.toggle('fa-chevron-down', open);
        icon.classList.toggle('fa-chevron-right', !open);
        const body = document.getElementById(btn.getAttribute('aria-controls'));
        if (body) body.hidden = !open;
    };
    list.addEventListener('click', (e) => {
        const btn = e.target.closest && e.target.closest('button');
        if (!btn || !list.contains(btn)) return;
        if (btn.dataset.toggle != null) { toggle(btn); return; }
        if (btn.dataset.requeueAll != null) {
            // Ticks only — the person still sees the rows and confirms with the
            // one "Add N" button below.
            const ri = +btn.dataset.requeueAll;
            const shown = filterReleases(releases, { query: view.query, kind: view.kind }).find(x => x.index === ri);
            requeueAllIndices(releases[ri], shown ? shown.items.map(x => x.index) : []).forEach(ii => { view.picks[key(ri, ii)] = true; });
            view.open[ri] = true;
            view.searchClosed[ri] = false;
            renderList();
            const again = list.querySelector('button[data-requeue-all="' + ri + '"]');
            if (again) again.focus();
            return;
        }
        if (btn.hasAttribute('data-clear')) {
            clearSearch();
            search.focus();
            return;
        }
        if (btn.hasAttribute('data-all-kinds')) {
            view.kind = 'all';
            paintChips();
            renderList();
        }
    });
    list.addEventListener('change', (e) => {
        const k = e.target && e.target.dataset && e.target.dataset.pick;
        if (!k) return;
        view.picks[k] = e.target.checked;
        paintTicked(+k.split(':')[0]);
        updateGo();
    });
    // A row missing what the gate will ask for can be ticked once it has been
    // given: a real run URL, and a ticket. The list is not re-rendered here —
    // that would take the cursor out of the field being typed in.
    list.addEventListener('input', (e) => {
        const d = e.target && e.target.dataset;
        const k = d && (d.run || d.jira);
        if (!k) return;
        view.typed[k] = { ...(view.typed[k] || {}), [d.run ? 'run' : 'jira']: e.target.value };
        const [ri, ii] = k.split(':').map(Number);
        const m = merged(ri, ii);
        const ok = ready(m);
        const cb = list.querySelector('input[data-pick="' + k + '"]');
        if (cb) {
            cb.disabled = !ok;
            if (!ok) cb.checked = false;
            cb.title = ok ? '' : missingWhy(m);
        }
        if (!ok) view.picks[k] = false;
        paintTicked(ri);
        updateGo();
    });

    go.addEventListener('click', async () => {
        const who = email.value.trim();
        if (!who.includes('@')) { err.textContent = 'Your email is needed — the queue records who asked.'; email.focus(); return; }
        // Every tick counts, including rows the current filter hides.
        const chosen = tickedKeys().map(k => {
            const [ri, ii] = k.split(':').map(Number);
            return { rel: releases[ri], it: merged(ri, ii) };
        });
        const { direct, gated, skipped } = requeuePlan(chosen.map(c => c.it));
        if (!direct.length && !gated.length) { err.textContent = skipped.map(s => s.artifact + ': ' + s.reason).join('; '); return; }
        if (!email.dataset.signedIn) { try { localStorage.setItem('queue_email', who); } catch (e) {} }
        busy = true;
        go.disabled = true;
        go.textContent = gated.length ? (gated.length > 1 ? 'Checking ' + gated.length + ' builds…' : 'Checking the build…') : 'Putting back…';
        err.textContent = '';
        const from = [...new Set(chosen.map(c => c.rel.release_name))].join(', ');
        // Charts that qualified once go straight back; never-queued ones take the
        // gate, carrying their own details (the shared line is only for one with none).
        const fail = (e) => ({ ok: false, error: String((e && e.message) || e) });
        const [back, checked] = await Promise.all([
            direct.length ? requeueFromHistory({ requested_by: who, items: direct }).catch(fail) : null,
            gated.length ? queueBatch({ requested_by: who, change_details: 'Re-queued from ' + from, rows: gated }).catch(fail) : null,
        ]);
        const queued = [...((back && back.queued) || []), ...((checked && checked.queued) || [])];
        // A call that failed outright answered for none of its charts — name each
        // one, or a failure beside another call's success reads as nothing at all.
        const failedCall = (res, artifacts) => (res && res.error && !res.queued && !res.refused)
            ? artifacts.map(artifact => ({ artifact, error: res.error })) : [];
        const refused = [...((back && back.refused) || []), ...((checked && checked.refused) || []),
            ...(queued.length ? failedCall(back, direct.map(d => d.artifact_name + ':' + d.artifact_version)) : []),
            ...(queued.length ? failedCall(checked, gated.map(g => g.artifact)) : [])];
        if (!queued.length && !refused.length) {
            const failed = [back, checked].find(r => r && r.error);
            busy = false;
            updateGo();
            err.textContent = (failed && failed.error) || 'Could not queue — try again.';
            return;
        }
        loadReleaseStatus(true);
        const lines = queued.map(q => '<div>✅ ' + esc(q.artifact) + ' is back in the next release.</div>')
            .concat(refused.map(r => '<div>❌ ' + esc(r.artifact) + ' — ' + esc(r.error || 'not eligible') + '</div>'))
            .concat(skipped.map(s => '<div>⏭ ' + esc(s.artifact) + ' — ' + esc(s.reason) + '</div>'));
        _render(wrap, { ok: !refused.length, html: lines.join('') }, view);
    });

    renderList();
}
