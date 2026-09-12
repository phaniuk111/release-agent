import { escapeHtml as esc } from '../core/format.js';
import { batchRow, queueSubmissionProblems, tickHint } from '../core/queue.js';
import { getContext, QUEUE_PATH, queueBatch } from '../api.js';
import { loadReleaseStatus } from '../status.js';
import { ctxNote, opening, withDismiss } from './common.js';

// ---- Add to next release (intake queue) ----------------------------------
// A dev ready on Monday registers chart:version + routing here; DevOps sees
// the accumulated list on release day (and the Create-release form pre-fills
// from it). Queueing writes a BQ intent event — it never deploys anything.
export async function showQueueForm() {
    const _ph = opening('Add to next release');
    const ctx = await getContext(QUEUE_PATH, { queue: [], known_charts: [], default_repo: '' });
    _ph.remove();

    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm';
    wrap.innerHTML =
        '<div class="mb-1 font-semibold flex items-center gap-2 text-emerald-300">' +
        '<i class="fa-solid fa-cart-plus"></i> Add to next release</div>' +
        '<div class="text-slate-400 text-xs mb-3">Register your chart for the upcoming release — ' +
        'no need to come back on release day. DevOps reviews the accumulated list when creating it. ' +
        'Nothing deploys from here.</div>';
    const qNote = ctxNote(ctx, 'the queue and chart list'); if (qNote) wrap.appendChild(qNote);

    if ((ctx.queue || []).length) {
        const hdr = document.createElement('div');
        hdr.className = 'text-[11px] text-slate-500 mb-1';
        hdr.textContent = 'Already queued (' + ctx.queue.length + ')';
        wrap.appendChild(hdr);
        const list = document.createElement('div');
        list.className = 'border border-slate-700 rounded-lg px-3 py-1.5 mb-3 text-[11px] font-mono text-slate-400';
        ctx.queue.forEach(q => {
            const row = document.createElement('div');
            row.className = 'flex justify-between gap-2 py-0.5';
            row.innerHTML = '<span class="truncate">' + esc(q.artifact_name) + ':' + esc(q.artifact_version) +
                (q.prl1_only ? ' <span class="text-violet-400">PRL1</span>' : '') +
                (q.df_only ? ' <span class="text-sky-400">DF</span>' : '') + '</span>' +
                '<span class="text-slate-600 truncate">' + esc((q.requested_by || '').split('@')[0]) + '</span>';
            list.appendChild(row);
        });
        wrap.appendChild(list);
    }

    // One ROW per chart, because one build run produces exactly one tag: chart,
    // version, its own run URL and its own ticket. A change spanning three
    // charts often spans two tickets, so the ticket cannot be a shared field.
    // Everything genuinely shared — who, what changed, the note — is asked once.
    if ((ctx.known_charts || []).length) {
        const dl = document.createElement('datalist');
        dl.id = 'q-charts-list';
        ctx.known_charts.forEach(c => {
            const o = document.createElement('option'); o.value = c; dl.appendChild(o);
        });
        wrap.appendChild(dl);
    }

    const rowsLabel = document.createElement('div');
    rowsLabel.className = 'text-[11px] text-slate-400 mb-1';
    rowsLabel.textContent = 'Charts * — one row per chart; each build run builds one tag';
    wrap.appendChild(rowsLabel);

    const rowsBox = document.createElement('div');
    rowsBox.className = 'mb-1';
    wrap.appendChild(rowsBox);

    const rows = [];
    const fld = (ph, cls, listId) => {
        const el = document.createElement('input');
        el.type = 'text'; el.placeholder = ph;
        if (listId) el.setAttribute('list', listId);
        el.className = 'bg-slate-900 border border-slate-700 rounded-lg px-2 py-1 ' +
            'text-xs text-white focus:outline-none ' + cls;
        return el;
    };
    const addRow = (focus) => {
        const row = document.createElement('div');
        row.className = 'flex gap-1.5 mb-1 items-center';
        const chart = fld('chart name', 'flex-1 min-w-0', 'q-charts-list');
        const ver = fld('version', 'w-24 shrink-0');
        const run = fld('build run URL', 'flex-1 min-w-0');
        const jira = fld('JIRA', 'w-28 shrink-0');
        // First row keeps the original ids so the chat/tests and the
        // replaces-warning keep working against a known handle.
        if (!rows.length) { chart.id = 'q-chart'; ver.id = 'q-version'; run.id = 'q-run'; jira.id = 'q-jira'; }
        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'text-slate-600 hover:text-red-400 text-xs px-1 shrink-0';
        del.innerHTML = '<i class="fa-solid fa-xmark"></i>';
        del.title = 'Remove this chart';
        const entry = { row: row, chart: chart, ver: ver, run: run, jira: jira };
        del.addEventListener('click', () => {
            if (rows.length === 1) { chart.value = ver.value = run.value = jira.value = ''; checkReplaces(); return; }
            rows.splice(rows.indexOf(entry), 1);
            row.remove();
            checkReplaces();
        });
        [chart, ver].forEach(el => el.addEventListener('input', () => checkReplaces()));
        row.appendChild(chart); row.appendChild(ver); row.appendChild(run); row.appendChild(jira); row.appendChild(del);
        rowsBox.appendChild(row);
        rows.push(entry);
        if (focus) chart.focus();
        return entry;
    };

    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'text-[11px] text-slate-400 hover:text-emerald-300 mb-2';
    addBtn.innerHTML = '<i class="fa-solid fa-plus"></i> Add another chart';
    addBtn.addEventListener('click', () => addRow(true));
    wrap.appendChild(addBtn);

    const runHint = document.createElement('div');
    runHint.className = 'text-[10px] text-slate-600 -mt-1 mb-2';
    runHint.textContent = 'Each row needs the run that built THAT tag — I check the build + its ' +
        'RCTLD controls NOW, and name any that failed. Rows are checked together.';
    wrap.appendChild(runHint);

    const grid = document.createElement('div');
    grid.className = 'grid grid-cols-2 gap-2 mb-2';
    const mk = (labelText, id, placeholder) => {
        const l = document.createElement('label');
        l.className = 'text-[11px] text-slate-400 block mb-0.5';
        l.textContent = labelText;
        const el = document.createElement('input');
        el.id = id; el.type = 'text'; if (placeholder) el.placeholder = placeholder;
        el.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
        const box = document.createElement('div');
        box.appendChild(l); box.appendChild(el);
        grid.appendChild(box);
        return el;
    };
    const emailEl = mk('Your email *', 'q-email', 'you@company.com');
    emailEl.value = localStorage.getItem('queue_email') || '';
    wrap.appendChild(grid);

    // Change context: the dev's what-and-why becomes the CHG description draft
    // when DevOps opens Create release — the dev knows this better on Monday
    // than anyone reconstructing it on Thursday. Shared: it is ONE change.
    const detailsLabel = document.createElement('label');
    detailsLabel.className = 'text-[11px] text-slate-400 block mb-0.5';
    detailsLabel.textContent = 'Change details * — what changed & why; pre-drafts the CHG for DevOps';
    const detailsEl = document.createElement('textarea');
    detailsEl.id = 'q-details'; detailsEl.rows = 2;
    detailsEl.placeholder = 'e.g. fixes schema drift in position feed after upstream v4 migration';
    detailsEl.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none mb-2';
    wrap.appendChild(detailsLabel); wrap.appendChild(detailsEl);

    const noteLabel = document.createElement('label');
    noteLabel.className = 'text-[11px] text-slate-400 block mb-0.5';
    noteLabel.textContent = 'Note for DevOps (optional)';
    const noteEl = document.createElement('input');
    noteEl.id = 'q-note'; noteEl.type = 'text';
    noteEl.placeholder = 'e.g. ship together with workflow-service';
    noteEl.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none mb-2';
    wrap.appendChild(noteLabel); wrap.appendChild(noteEl);

    // Re-queueing a chart REPLACES its queue entry — the queue is keyed by chart
    // name and the latest event wins. Silently swapping someone's 1.4.2 for a
    // 1.4.3 is the kind of thing you only notice on release day, so say it while
    // they type. Withdrawing is the other option, hence the hint.
    const replaceNote = document.createElement('div');
    replaceNote.className = 'text-[11px] text-amber-300/90 mb-2 hidden';
    wrap.appendChild(replaceNote);
    const checkReplaces = () => {
        const hits = [];
        rows.forEach(r => {
            const typed = r.chart.value.trim().toLowerCase();
            if (!typed) return;
            const existing = (ctx.queue || []).find(
                q => String(q.artifact_name || '').toLowerCase() === typed);
            if (!existing) return;
            const same = String(existing.artifact_version || '').trim() === r.ver.value.trim();
            hits.push({ existing: existing, same: same });
        });
        if (!hits.length) { replaceNote.className = 'text-[11px] text-amber-300/90 mb-2 hidden'; return; }
        replaceNote.className = 'text-[11px] text-amber-300/90 mb-2';
        replaceNote.innerHTML = hits.map(h =>
            '<i class="fa-solid fa-arrows-rotate mr-1"></i>' +
            (h.same
                ? 'Already queued as <b>' + esc(h.existing.artifact_name) + ':' +
                  esc(h.existing.artifact_version) + '</b> — submitting again just refreshes it.'
                : 'This <b>replaces</b> the queued <b>' + esc(h.existing.artifact_name) + ':' +
                  esc(h.existing.artifact_version) + '</b>' +
                  (h.existing.requested_by ? ' (queued by ' + esc(h.existing.requested_by.split('@')[0]) + ')' : '') +
                  '. To take it out of the release instead, withdraw it.')
        ).join('<br>');
    };
    addRow(false);          // start with one row; listeners are wired per row

    // Routing, asked POSITIVELY: which release carries this, and how far it goes.
    // The API still takes the two negatives it always took, so the mapping happens
    // at submit and the developer never has to think in "only"s:
    //   CARE + PRD   -> df_only=false, prl1_only=false   (UAT, PRL1 and PRD)
    //   CARE + PRL1  -> df_only=false, prl1_only=true    (UAT and PRL1, never PRD)
    //   Dataflow     -> df_only=true                     (no helm deploy at all)
    const mkFlag = (id, text, title) => {
        const l = document.createElement('label');
        l.className = 'flex items-center gap-1 cursor-pointer';
        l.title = title;
        const cb = document.createElement('input');
        cb.type = 'checkbox'; cb.id = id;
        l.appendChild(cb); l.appendChild(document.createTextNode(' ' + text));
        return { label: l, cb: cb };
    };
    const mkFlagRow = (labelText) => {
        const r = document.createElement('div');
        r.className = 'flex items-center gap-3';
        const l = document.createElement('span');
        l.className = 'text-slate-500 w-[86px] shrink-0';
        l.textContent = labelText;
        r.appendChild(l);
        return r;
    };

    const flags = document.createElement('div');
    flags.className = 'text-[11px] text-slate-400 mb-2 space-y-1';
    const typeRow = mkFlagRow('Release type *');
    const care = mkFlag('q-care', 'CARE', 'Helm chart — rides the CARE release');
    const dfF = mkFlag('q-df', 'Dataflow',
        'DF image — rides the DF release; never enters a helm deploy workflow');
    typeRow.appendChild(care.label); typeRow.appendChild(dfF.label);
    const envRow = mkFlagRow('Goes to *');
    const prd = mkFlag('q-prd', 'PRD', 'Full path: UAT, then PRL1, then PRD');
    const prl1 = mkFlag('q-prl1', 'PRL1', 'Stops at PRL1 — never promoted to PRD');
    const envHint = document.createElement('span');
    envHint.className = 'text-slate-600';
    envRow.appendChild(prd.label); envRow.appendChild(prl1.label); envRow.appendChild(envHint);
    flags.appendChild(typeRow); flags.appendChild(envRow);
    wrap.appendChild(flags);

    // Exactly one release type is always on: unticking one ticks the other, so
    // the pair reads as tick boxes but cannot land in a state the API has no
    // value for.
    // Both environments are independently tickable in BOTH lanes: the CHG is
    // raised once and which pipeline is triggered is decided at deploy time, so
    // the developer records what is in scope rather than a single destination.
    //
    // The hint is where the two lanes differ, because CARE has a second
    // consumer the tick boxes do not control. prl1_only routes the release
    // FILE-SET, and it has only two states — so ticking PRD alone records the
    // pipeline you intend to trigger, it does NOT hold the chart out of the
    // PRL1 file-set: _service_routing generates a standard chart into every
    // environment's workflow. Only PRL1-without-PRD actually excludes PRD.
    // Saying so here is cheaper than someone discovering it on release day.
    const ticks = () => ({ df: dfF.cb.checked, prd: prd.cb.checked, prl1: prl1.cb.checked });
    const syncFlags = () => { envHint.textContent = tickHint(ticks()); };
    care.cb.addEventListener('change', () => { dfF.cb.checked = !care.cb.checked; syncFlags(); });
    dfF.cb.addEventListener('change', () => { care.cb.checked = !dfF.cb.checked; syncFlags(); });
    prd.cb.addEventListener('change', syncFlags);
    prl1.cb.addEventListener('change', syncFlags);
    // Default to the full path — the common case, and the one the file-set
    // generates anyway for a standard chart.
    care.cb.checked = true; prd.cb.checked = true; prl1.cb.checked = true;
    syncFlags();

    const row = document.createElement('div');
    row.className = 'flex items-center gap-3 mt-1';
    const submit = document.createElement('button');
    submit.className = 'bg-emerald-600 hover:bg-emerald-500 px-4 py-1.5 rounded-lg text-sm font-medium';
    submit.textContent = 'Queue it';
    const err = document.createElement('span');
    err.className = 'text-[11px] text-red-400';
    // Retract a routing complaint the moment the routing changes — ticking
    // Dataflow makes "Tick where it goes" untrue, and an error that outlives its
    // cause reads as a second, unexplained problem.
    [care.cb, dfF.cb, prd.cb, prl1.cb].forEach(
        cb => cb.addEventListener('change', () => { err.textContent = ''; }));
    submit.addEventListener('click', async () => {
        err.textContent = '';
        wrap.querySelectorAll('.batch-result').forEach(n => n.remove());
        const email = emailEl.value.trim();
        const details = detailsEl.value.trim();

        // Every field is required per row and every bad row is reported at once,
        // named by chart; rows left empty are not part of the submission
        // (core/queue.js holds the rules).
        const { filled, problems } = queueSubmissionProblems(
            rows.map(r => ({ chart: r.chart.value, version: r.ver.value, run: r.run.value, jira: r.jira.value })),
            { email, details, ticks: ticks() });
        if (problems.length) { err.innerHTML = problems.map(esc).join('<br>'); return; }

        localStorage.setItem('queue_email', email);
        submit.disabled = true;
        submit.textContent = filled.length > 1 ? 'Checking ' + filled.length + ' builds…' : 'Queueing…';
        let result = null;
        try {
            result = await queueBatch({
                requested_by: email,
                change_details: details,
                note: noteEl.value.trim(),
                rows: filled.map(r => batchRow(r, ticks())),
            });
        } catch (e) { result = { ok: false, error: String(e) }; }
        submit.disabled = false; submit.textContent = 'Queue it';

        if (!result || (!result.queued && !result.refused)) {
            err.textContent = (result && result.error) || 'Could not queue — try again.';
            return;
        }

        const queued = result.queued || [];
        const refused = result.refused || [];

        // A refusal names the control AND its job — on a many-job run "a control
        // failed" is not enough to go and fix it.
        const refusedRow = (r) => {
            const detail = r.failed_controls_detail;
            const parts = (detail && detail.length)
                ? detail.map(c => 'control ' + esc(c.control) + (c.job ? ' in job ' + esc(c.job) : ''))
                : (r.failed_controls || []).map(c => 'control ' + esc(c));
            (r.failed_steps || []).forEach(st => parts.push('step ' + esc(st.name || st) +
                (st.job ? ' in job ' + esc(st.job) : '')));
            (r.open_controls || []).forEach(c => parts.push('control ' + esc(c.control) +
                (c.job ? ' in job ' + esc(c.job) : '') + ' not passed yet (' +
                esc(c.status || c.conclusion || 'not run') + ')'));
            const why = parts.length ? parts.join('; ') : esc(r.error || 'not eligible');
            return '<div class="font-mono">❌ ' + esc(r.artifact) + ' — ' + why +
                (r.run_url ? ' <a href="' + esc(r.run_url) + '" target="_blank" class="underline">open run</a>' : '') +
                '</div>';
        };

        // PARTIAL SUCCESS: eligible rows are queued even when a sibling fails.
        // Losing good work because one control failed is the worse outcome — but
        // it does split one change across releases, so say so plainly.
        if (refused.length) {
            const box = document.createElement('div');
            box.className = 'batch-result w-full border border-red-500/40 bg-red-500/10 ' +
                'rounded-lg px-3 py-2 text-[11px] text-red-300 mt-2';
            box.innerHTML =
                (queued.length
                    ? '<b>Queued ' + queued.length + ', refused ' + refused.length + '.</b><br>'
                    : '<b>Not eligible for the release — nothing queued.</b><br>') +
                refused.map(refusedRow).join('') +
                (queued.length
                    ? '<div class="mt-1 text-amber-300">⚠ This splits your change — ' +
                      queued.map(q => esc(q.artifact)).join(', ') +
                      ' will ship without the above unless you fix and re-queue before release day.</div>'
                    : '<div class="mt-1">Every control must pass to queue — fix these (or let the run ' +
                      'finish), then queue again with a run where they all pass.</div>');
            row.parentNode.insertBefore(box, row);
            if (!queued.length) return;
        }

        if (!queued.length) return;
        const line = (q) => {
            const open = q.open_controls || [];
            const badge = q.eligible === true
                ? '<span class="text-emerald-400">✓ build + controls passed</span>'
                : open.length
                    ? '<span class="text-amber-400">⚠ ' + open.length + ' control(s) not passed yet: ' +
                      open.map(c => esc(c.control)).join(', ') + '</span>'
                    : '<span class="text-amber-400">⚠ no traceable build</span>';
            return '<div class="text-[11px] font-mono mt-0.5">' + esc(q.artifact) + ' — ' + badge + '</div>';
        };
        wrap.innerHTML =
            '<div class="font-semibold text-emerald-300 mb-1"><i class="fa-solid fa-circle-check"></i> Queued for the next release (' +
            queued.length + ')</div>' + queued.map(line).join('') +
            // The refusal REASON has to survive into this card: the red box above
            // was rendered into `wrap`, which this innerHTML replaces, and
            // "not queued" without the control name sends nobody anywhere.
            (refused.length
                ? '<div class="border border-red-500/40 bg-red-500/10 rounded-lg px-3 py-2 ' +
                  'text-[11px] text-red-300 mt-2"><b>Not queued (' + refused.length + ')</b>' +
                  refused.map(refusedRow).join('') +
                  '<div class="mt-1 text-amber-300">⚠ Your change is split — fix these and ' +
                  're-queue before release day.</div></div>'
                : '') +
            '<div class="text-[11px] text-slate-500 mt-2">You\'re done — they will be in the ' +
            '<b>' + (dfF.cb.checked ? 'DF' : 'CARE') + ' Release</b> form automatically. ' +
            'Withdraw any time from the Insights panel or by asking me.</div>';
        withDismiss(wrap);          // innerHTML above wiped the original ✕
        loadReleaseStatus(true);
    });
    row.appendChild(submit); row.appendChild(err);
    wrap.appendChild(row);
    withDismiss(wrap);
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
}
