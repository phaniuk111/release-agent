// Deploy forms: the GKE JSON editor, the Dataflow image+tag form, and the
// client-side parsers that pop them from typed chat commands. On submit each
// form sends a JSON payload through the normal /api/chat SSE flow; the backend
// previews the exact change and asks for the CONFIRM token.
import { API_BASE } from './state.js';
import { sendMessage, escapeHtml as esc } from './chat.js';
import { loadReleaseStatus } from './status.js';


// ---- Add to next release (intake queue) ----------------------------------
// A dev ready on Monday registers chart:version + routing here; DevOps sees
// the accumulated list on release day (and the Create-release form pre-fills
// from it). Queueing writes a BQ intent event — it never deploys anything.
export async function showQueueForm() {
    let ctx = { queue: [], known_charts: [], default_repo: '' };
    try {
        const r = await fetch(API_BASE + '/api/release-queue');
        if (r.ok) ctx = await r.json();
    } catch (e) {}

    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm';
    wrap.innerHTML =
        '<div class="mb-1 font-semibold flex items-center gap-2 text-emerald-300">' +
        '<i class="fa-solid fa-cart-plus"></i> Add to next release</div>' +
        '<div class="text-slate-400 text-xs mb-3">Register your chart for the upcoming release — ' +
        'no need to come back on release day. DevOps reviews the accumulated list when creating it. ' +
        'Nothing deploys from here.</div>';

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

    const grid = document.createElement('div');
    grid.className = 'grid grid-cols-2 gap-2 mb-2';
    const mk = (labelText, id, placeholder, listId) => {
        const l = document.createElement('label');
        l.className = 'text-[11px] text-slate-400 block mb-0.5';
        l.textContent = labelText;
        const el = document.createElement('input');
        el.id = id; el.type = 'text'; if (placeholder) el.placeholder = placeholder;
        if (listId) el.setAttribute('list', listId);
        el.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
        const box = document.createElement('div');
        box.appendChild(l); box.appendChild(el);
        grid.appendChild(box);
        return el;
    };
    // Known charts from the build catalog → typo-proof picking, still free-text.
    if ((ctx.known_charts || []).length) {
        const dl = document.createElement('datalist');
        dl.id = 'q-charts-list';
        ctx.known_charts.forEach(c => {
            const o = document.createElement('option'); o.value = c; dl.appendChild(o);
        });
        wrap.appendChild(dl);
    }
    const chartEl = mk('Chart name *', 'q-chart', 'e.g. acme-risk-fetcher', 'q-charts-list');
    const verEl = mk('Version *', 'q-version', 'e.g. 4.0.154');
    const emailEl = mk('Your email *', 'q-email', 'you@company.com');
    emailEl.value = localStorage.getItem('queue_email') || '';
    const jiraEl = mk('JIRA / ticket (optional)', 'q-jira', 'e.g. REL-1234');
    const runEl = mk('Build run URL *', 'q-run', 'https://github.com/…/actions/runs/…');
    wrap.appendChild(grid);
    const runHint = document.createElement('div');
    runHint.className = 'text-[10px] text-slate-600 -mt-1 mb-2';
    runHint.textContent = 'Required — the run that built this tag. I check the build + RLFT/RFTL controls NOW and only queue release-eligible builds.';
    wrap.appendChild(runHint);

    // Change context: the dev's what-and-why becomes the CHG description draft
    // when DevOps opens Create release — the dev knows this better on Monday
    // than anyone reconstructing it on Thursday.
    const detailsLabel = document.createElement('label');
    detailsLabel.className = 'text-[11px] text-slate-400 block mb-0.5';
    detailsLabel.textContent = 'Change details (optional) — what changed & why; pre-drafts the CHG for DevOps';
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

    const flagRow = document.createElement('div');
    flagRow.className = 'flex items-center gap-4 text-[11px] text-slate-400 mb-2';
    flagRow.innerHTML =
        '<label class="flex items-center gap-1"><input type="checkbox" id="q-prl1"> PRL1-only (never PRD)</label>' +
        '<label class="flex items-center gap-1"><input type="checkbox" id="q-df"> Dataflow image</label>';
    wrap.appendChild(flagRow);

    const row = document.createElement('div');
    row.className = 'flex items-center gap-3 mt-1';
    const submit = document.createElement('button');
    submit.className = 'bg-emerald-600 hover:bg-emerald-500 px-4 py-1.5 rounded-lg text-sm font-medium';
    submit.textContent = 'Queue it';
    const err = document.createElement('span');
    err.className = 'text-[11px] text-red-400';
    submit.addEventListener('click', async () => {
        err.textContent = '';
        const chart = chartEl.value.trim(), ver = verEl.value.trim(), email = emailEl.value.trim();
        if (!chart || !ver || !email) { err.textContent = 'Chart, version and your email are required.'; return; }
        if (!runEl.value.trim() || runEl.value.indexOf('/actions/runs/') === -1) {
            err.textContent = 'The build run URL is required (…/actions/runs/<id>) — it proves the tag is release-eligible.'; return;
        }
        localStorage.setItem('queue_email', email);
        submit.disabled = true; submit.textContent = 'Queueing…';
        let result = null;
        try {
            const r = await fetch(API_BASE + '/api/release-queue', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    artifact: chart + ':' + ver,
                    requested_by: email,
                    prl1_only: document.getElementById('q-prl1').checked,
                    df_only: document.getElementById('q-df').checked,
                    note: noteEl.value.trim(),
                    jira_ticket: jiraEl.value.trim(),
                    change_details: detailsEl.value.trim(),
                    build_run_url: runEl.value.trim(),
                }),
            });
            result = await r.json();
        } catch (e) { result = { ok: false, error: String(e) }; }
        submit.disabled = false; submit.textContent = 'Queue it';
        if (result && result.eligible === false) {
            // The provided run failed its build or controls — NOT queued.
            // Show the verdict with exactly what to fix; the form stays usable.
            const items = (result.failed_controls || []).map(c => '❌ control: ' + esc(c))
                .concat((result.failed_steps || []).map(s => '❌ step: ' + esc(s.name || s) + (s.job ? ' (' + esc(s.job) + ')' : '')));
            err.innerHTML = '';
            const box = document.createElement('div');
            box.className = 'w-full border border-red-500/40 bg-red-500/10 rounded-lg px-3 py-2 text-[11px] text-red-300';
            box.innerHTML = '<b>Not eligible for the release — not queued.</b><br>' +
                items.map(i => '<span class="font-mono">' + i + '</span>').join('<br>') +
                '<br>Fix these, re-run the build, then queue again with the new run. ' +
                (result.run_url ? '<a href="' + esc(result.run_url) + '" target="_blank" class="underline">open run</a>' : '');
            row.parentNode.insertBefore(box, row);
            return;
        }
        if (!result || !result.ok) {
            err.textContent = (result && result.error) || 'Could not queue — try again.';
            return;
        }
        // Replace the form with a human confirmation: eligibility verdict + last-time context.
        const verified = result.build_verified;
        const vBadge = result.eligible === true
            ? '<span class="text-emerald-400"><i class="fa-solid fa-circle-check"></i> build + RLFT/RFTL controls passed — eligible for the release</span>'
            : verified === true
                ? '<span class="text-emerald-400"><i class="fa-solid fa-circle-check"></i> build verified</span>'
                : verified === false
                    ? '<span class="text-amber-400"><i class="fa-solid fa-triangle-exclamation"></i> no traceable build for this tag (queued anyway — is it built yet?)</span>'
                    : '<span class="text-slate-500">build check skipped</span>';
        const warn = (result.warnings || []).map(w =>
            '<div class="text-[11px] text-amber-400 mt-1"><i class="fa-solid fa-triangle-exclamation"></i> ' + esc(w) + '</div>').join('');
        const last = result.last_shipped
            ? '<div class="text-[11px] text-slate-500 mt-1">Last shipped in “' + esc(result.last_shipped.release_name) +
              '” as ' + esc(result.last_shipped.version) + '.</div>' : '';
        wrap.innerHTML =
            '<div class="font-semibold text-emerald-300 mb-1"><i class="fa-solid fa-circle-check"></i> Queued for the next release</div>' +
            '<div class="text-xs text-slate-300 font-mono">' + esc(chart) + ':' + esc(ver) +
            (document.getElementById('q-prl1') && result.intent && result.intent.prl1_only ? ' · PRL1-only' : '') + '</div>' +
            '<div class="text-[11px] mt-1">' + vBadge + '</div>' + warn + last +
            '<div class="text-[11px] text-slate-500 mt-2">You\'re done — it will be in the ' +
            '<b>' + (document.getElementById('q-df').checked ? 'DF' : 'CARE') + ' Release</b> form automatically. ' +
            'Withdraw any time from the Insights panel or by asking me.</div>';
        loadReleaseStatus();
    });
    row.appendChild(submit); row.appendChild(err);
    wrap.appendChild(row);
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
}


// ---- CARE / DF release (live model) --------------------------------------
// One form, two flavors, same backend pipeline (release_details.json → the
// repo's updater script → preview → CONFIRM):
//   CARE Release: helm charts with per-service PRL1-only / DF-image flags.
//   DF Release:   Dataflow images only — every artifact is auto-flagged
//                 df_images (excluded from all helm deploy workflows), so
//                 there are no per-service checkboxes to get wrong.
// The queue pre-fill is flavor-aware: DF sees only df-flagged intents, CARE
// sees the rest.
export async function showReleaseForm(kind) {
    const isDf = kind === 'df';
    // Queue context first: the accumulated "add me to the next release" intents
    // pre-fill the artifact list, and the default deployment repo pre-fills the
    // target repo field.
    let qctx = { queue: [], default_repo: '' };
    try {
        const r = await fetch(API_BASE + '/api/release-queue');
        if (r.ok) qctx = await r.json();
    } catch (e) {}
    qctx.queue = (qctx.queue || []).filter(q => (isDf ? q.df_only : !q.df_only));

    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm';
    wrap.innerHTML = isDf
        ? '<div class="mb-1 font-semibold flex items-center gap-2 text-sky-300">' +
          '<i class="fa-solid fa-water"></i> DF Release</div>' +
          '<div class="text-slate-400 text-xs mb-3">Dataflow images + change request → the release file-set ' +
          '(artefact.json + SDLC governance), generated by the repo\'s updater script. DF images are ' +
          'excluded from the helm deploy workflows — they ship via the Dataflow dispatch. ' +
          'You\'ll see the full diff and RCTL timeline before anything is pushed.</div>'
        : '<div class="mb-1 font-semibold flex items-center gap-2 text-emerald-300">' +
          '<i class="fa-solid fa-box-open"></i> CARE Release</div>' +
          '<div class="text-slate-400 text-xs mb-3">Artifacts + change request → the release file-set ' +
          '(artefact.json, SDLC governance, per-env workflows), generated by the repo\'s updater script. ' +
          'You\'ll see the full diff and RCTL timeline before anything is pushed.</div>';

    const grid = document.createElement('div');
    grid.className = 'grid gap-2 mb-2';
    const mk = (labelText, el, id, type, placeholder) => {
        if (type) el.type = type;
        el.id = id;
        if (placeholder) el.placeholder = placeholder;
        el.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
        const l = document.createElement('label');
        l.className = 'text-[11px] text-slate-400 block mb-0.5';
        l.textContent = labelText;
        const box = document.createElement('div');
        box.appendChild(l); box.appendChild(el);
        grid.appendChild(box);
        return el;
    };
    const nameEl = mk('Release name *', document.createElement('input'), 'rel-name', 'text', 'e.g. July 20th 2026 : Release 31');
    const startEl = mk('Start *', document.createElement('input'), 'rel-start', 'datetime-local');
    const endEl = mk('End *', document.createElement('input'), 'rel-end', 'datetime-local');
    const initEl = mk('Change initiator (email) *', document.createElement('input'), 'rel-initiator', 'text', 'you@company.com');
    const sumEl = mk('Change summary *', document.createElement('input'), 'rel-summary', 'text');
    const descEl = mk('Change description', document.createElement('textarea'), 'rel-desc');
    const reasonEl = mk('Change reason', document.createElement('textarea'), 'rel-reason');
    const riskEl = mk('Associated risk', document.createElement('textarea'), 'rel-risk');
    const consEl = mk('Consequence', document.createElement('textarea'), 'rel-consequence');
    const impactEl = mk('User/service impact', document.createElement('textarea'), 'rel-impact');
    const repoEl = mk('Deployment repo (owner/repo) *', document.createElement('input'),
        'rel-repo', 'text', 'e.g. my-org/deployment-repo');
    repoEl.value = qctx.default_repo || '';
    const artEl = mk(
        isDf ? 'DF images * — one per line (full URL or name:version); all excluded from helm deploys'
             : 'Artifacts * — one per line (full URL or name:version)',
        document.createElement('textarea'), 'rel-artifacts', null,
        isDf ? 'order-enrichment:1.4.2\nhttps://artifactory…/df-position-agg:2.1.0'
             : 'acme-workflow-service:4.0.66\nhttps://artifactory…/acme-risk-fetcher:4.0.153');
    artEl.rows = 5; artEl.spellcheck = false; artEl.classList.add('font-mono');
    wrap.appendChild(grid);

    // Summary defaults to the release name.
    nameEl.addEventListener('change', () => { if (!sumEl.value.trim()) sumEl.value = nameEl.value.trim(); });

    // Per-service flags, regenerated from the artifact list.
    const flagsHdr = document.createElement('div');
    flagsHdr.className = 'text-[11px] text-slate-400 mb-1';
    flagsHdr.textContent = 'Per-service routing (from the artifact list):';
    const flags = document.createElement('div');
    flags.className = 'mb-2 space-y-1';
    wrap.appendChild(flagsHdr); wrap.appendChild(flags);

    const parseNames = () => artEl.value.split('\n').map(l => l.trim()).filter(Boolean).map(l => {
        const last = l.replace(/\/+$/, '').split('/').pop();
        const i = last.indexOf(':');
        return i > 0 ? last.slice(0, i) : null;
    }).filter(Boolean);

    const renderFlags = () => {
        flags.innerHTML = '';
        if (isDf) { flagsHdr.style.display = 'none'; return; }  // all DF — nothing to route
        const seen = new Set();
        parseNames().forEach(n => {
            if (seen.has(n)) return; seen.add(n);
            const row = document.createElement('div');
            row.className = 'flex items-center gap-3 text-[11px] text-slate-300 font-mono';
            row.innerHTML = '<span class="flex-1 truncate">' + esc(n) + '</span>' +
                '<label class="flex items-center gap-1 text-slate-400"><input type="checkbox" data-prl1="' + esc(n) + '"> PRL1-only</label>' +
                '<label class="flex items-center gap-1 text-slate-400"><input type="checkbox" data-df="' + esc(n) + '"> DF image</label>';
            flags.appendChild(row);
        });
        flagsHdr.style.display = seen.size ? '' : 'none';
    };
    artEl.addEventListener('input', renderFlags);
    renderFlags();

    // Thursday pre-fill: the intake queue arrives as a checklist — untick an
    // item to defer it (it stays queued for the next release). Ticked items
    // fill the artifact list and carry their PRL1/DF routing flags.
    if ((qctx.queue || []).length) {
        const qBox = document.createElement('div');
        qBox.className = 'border border-emerald-700/40 bg-emerald-500/5 rounded-lg px-3 py-2 mb-2';
        qBox.innerHTML = '<div class="text-[11px] text-emerald-300 mb-1"><i class="fa-solid fa-cart-plus mr-1"></i>' +
            'Queued for this release (' + qctx.queue.length + ') — untick to defer to the next one</div>';
        const syncFlags = () => {
            qctx.queue.forEach(it => {
                const cb = qBox.querySelector('input[data-q="' + it.artifact_name + '"]');
                if (!cb || !cb.checked) return;
                const p = flags.querySelector('input[data-prl1="' + it.artifact_name + '"]');
                if (p) p.checked = !!it.prl1_only;
                const d = flags.querySelector('input[data-df="' + it.artifact_name + '"]');
                if (d) d.checked = !!it.df_only;
            });
        };
        // CHG draft from the devs' own context: the description aggregates each
        // checked item's JIRA + change details; the reason lists the tickets.
        // Auto-fills only while the field is empty or still equal to the last
        // auto draft — a manual DevOps edit always wins.
        let autoDesc = '', autoReason = '';
        const composeChg = () => {
            const checked = qctx.queue.filter(it => {
                const cb = qBox.querySelector('input[data-q="' + it.artifact_name + '"]');
                return cb && cb.checked;
            });
            const descDraft = checked.map(it =>
                '- ' + it.artifact_name + ':' + it.artifact_version +
                (it.jira_ticket ? ' (' + it.jira_ticket + ')' : '') +
                (it.change_details ? ': ' + it.change_details : '') +
                (it.requested_by ? ' — ' + it.requested_by.split('@')[0] : '')
            ).join('\n');
            const jiras = checked.map(it => it.jira_ticket).filter(Boolean);
            const reasonDraft = jiras.length ? 'Delivers ' + jiras.join(', ') : '';
            if (!descEl.value.trim() || descEl.value === autoDesc) { descEl.value = descDraft; autoDesc = descDraft; }
            if (reasonDraft && (!reasonEl.value.trim() || reasonEl.value === autoReason)) { reasonEl.value = reasonDraft; autoReason = reasonDraft; }
        };
        const applyItem = (q, on) => {
            const line = q.artifact_name + ':' + q.artifact_version;
            const lines = artEl.value.split('\n').map(l => l.trim()).filter(Boolean)
                .filter(l => l.split('/').pop().indexOf(q.artifact_name + ':') !== 0);
            if (on) lines.push(line);
            artEl.value = lines.join('\n');
            renderFlags();
            syncFlags();
            composeChg();
        };
        qctx.queue.forEach(q => {
            const row = document.createElement('label');
            row.className = 'flex items-center gap-2 text-[11px] text-slate-300 font-mono py-0.5 cursor-pointer';
            const cb = document.createElement('input');
            cb.type = 'checkbox'; cb.checked = true; cb.dataset.q = q.artifact_name;
            const badge = (q.build_verified === true)
                ? ' <i class="fa-solid fa-circle-check text-emerald-400" title="build verified at queue time"></i>'
                : (q.build_verified === false)
                    ? ' <i class="fa-solid fa-triangle-exclamation text-amber-400" title="no traceable build at queue time"></i>' : '';
            const span = document.createElement('span');
            span.className = 'flex-1 truncate';
            span.innerHTML = esc(q.artifact_name) + ':' + esc(q.artifact_version) + badge +
                (q.jira_ticket ? ' <span class="text-amber-300/80">' + esc(q.jira_ticket) + '</span>' : '') +
                (q.prl1_only ? ' <span class="text-violet-400">PRL1</span>' : '') +
                (q.df_only ? ' <span class="text-sky-400">DF</span>' : '');
            const tip = [q.change_details, q.note].filter(Boolean).join(' · ');
            if (tip) span.title = tip + ' — ' + (q.requested_by || '');
            const who = document.createElement('span');
            who.className = 'text-slate-600 truncate';
            who.textContent = (q.requested_by || '').split('@')[0];
            cb.addEventListener('change', () => applyItem(q, cb.checked));
            row.appendChild(cb); row.appendChild(span); row.appendChild(who);
            qBox.appendChild(row);
        });
        wrap.insertBefore(qBox, grid);
        qctx.queue.forEach(q => applyItem(q, true));
    }

    const fmt = (v) => v ? v.replace('T', ' ') + (v.length === 16 ? ':00' : '') : '';

    const row = document.createElement('div');
    row.className = 'flex items-center gap-3 mt-1';
    const submit = document.createElement('button');
    submit.className = (isDf ? 'bg-sky-600 hover:bg-sky-500' : 'bg-emerald-600 hover:bg-emerald-500') +
        ' px-4 py-1.5 rounded-lg text-sm font-medium';
    submit.textContent = isDf ? 'Create DF release' : 'Create CARE release';
    const err = document.createElement('span');
    err.className = 'text-[11px] text-red-400';
    submit.addEventListener('click', () => {
        err.textContent = '';
        const artifacts = artEl.value.split('\n').map(l => l.trim()).filter(Boolean);
        if (!nameEl.value.trim() || !startEl.value || !endEl.value || !initEl.value.trim() || !sumEl.value.trim()) {
            err.textContent = 'Release name, start, end, initiator and summary are required.'; return;
        }
        if (!repoEl.value.trim() || repoEl.value.indexOf('/') < 1) {
            err.textContent = 'Deployment repo is required (owner/repo).'; return;
        }
        if (!artifacts.length) { err.textContent = 'At least one artifact is required.'; return; }
        if (fmt(endEl.value) <= fmt(startEl.value)) { err.textContent = 'End must be after start.'; return; }
        const payload = {
            deployment_repo: repoEl.value.trim(),
            release_name: nameEl.value.trim(),
            start_date: fmt(startEl.value),
            end_date: fmt(endEl.value),
            change_initiator: initEl.value.trim(),
            change_summary: sumEl.value.trim(),
            change_description: descEl.value.trim(),
            change_reason: reasonEl.value.trim(),
            associated_risk: riskEl.value.trim(),
            consequence: consEl.value.trim(),
            user_service_impact: impactEl.value.trim(),
            // DF release: every artifact is a Dataflow image by definition.
            prl1_only: isDf ? [] : Array.from(flags.querySelectorAll('input[data-prl1]:checked')).map(c => c.dataset.prl1),
            df_images: isDf ? Array.from(new Set(parseNames()))
                            : Array.from(flags.querySelectorAll('input[data-df]:checked')).map(c => c.dataset.df),
            artefact: artifacts,
        };
        sendMessage(JSON.stringify(payload));
    });
    row.appendChild(submit); row.appendChild(err);
    wrap.appendChild(row);
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
}

// ---- Dataflow flex-template deploy (workflow-dispatch golden path) ------
// The dev supplies image name + tag; deploying dispatches the DF repo's
// deploy workflow. Recent runs give context; repo override sits under
// Advanced. Same preview + CONFIRM contract as every other deploy.
export async function showDfDeployForm() {
    let ctx = { recent_runs: [], deploy_repo: '', workflow: 'df-deploy.yml' };
    try {
        const r = await fetch(API_BASE + '/api/df-template?env=uat');
        if (r.ok) ctx = await r.json();
    } catch (e) {}

    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm';
    wrap.innerHTML =
        '<div class="mb-1 font-semibold flex items-center gap-2 text-sky-300">' +
        '<i class="fa-solid fa-water"></i> Deploy to DF UAT</div>' +
        '<div class="text-slate-400 text-xs mb-3">Triggers the <code>' + (ctx.workflow || 'df-deploy.yml') +
        '</code> workflow with your image + tag. Nothing runs until you confirm the preview.</div>';

    if ((ctx.recent_runs || []).length) {
        const hdr = document.createElement('div');
        hdr.className = 'text-[11px] text-slate-500 mb-1';
        hdr.textContent = 'Recent DF deploys';
        wrap.appendChild(hdr);
        const list = document.createElement('div');
        list.className = 'border border-slate-700 rounded-lg px-3 py-1.5 mb-3 text-[11px] font-mono text-slate-400';
        ctx.recent_runs.forEach(r => {
            const row = document.createElement('div');
            row.className = 'flex justify-between py-0.5';
            row.innerHTML = '<a href="' + r.url + '" target="_blank" class="underline">run #' + r.id + '</a>' +
                '<span>' + (r.conclusion || r.status || '') + '</span>';
            list.appendChild(row);
        });
        wrap.appendChild(list);
    }

    const grid = document.createElement('div');
    grid.className = 'grid grid-cols-2 gap-2 mb-1';
    const mk = (labelText, id, placeholder) => {
        const l = document.createElement('label');
        l.className = 'text-[11px] text-slate-400 block mb-0.5';
        l.textContent = labelText;
        const el = document.createElement('input');
        el.id = id; el.type = 'text'; el.placeholder = placeholder;
        el.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
        const box = document.createElement('div');
        box.appendChild(l); box.appendChild(el);
        grid.appendChild(box);
        return el;
    };
    const imgEl = mk('Image name', 'df-image', 'e.g. order-enrichment');
    const tagEl = mk('Tag', 'df-tag', 'e.g. 1.4.2');
    wrap.appendChild(grid);

    const echo = document.createElement('div');
    echo.className = 'text-[11px] text-slate-500 mb-2 h-4';
    wrap.appendChild(echo);
    const updateEcho = () => {
        const i = imgEl.value.trim(), t = tagEl.value.trim();
        echo.textContent = (i && t) ? ('↳ will dispatch ' + i + ':' + t + ' → uat ✓') : '';
        echo.className = 'text-[11px] mb-2 h-4 ' + ((i && t) ? 'text-emerald-400' : 'text-slate-500');
    };
    imgEl.addEventListener('input', updateEcho);
    tagEl.addEventListener('input', updateEcho);

    // Advanced (collapsed): repo override.
    const adv = document.createElement('div');
    adv.className = 'mb-2';
    const advToggle = document.createElement('button');
    advToggle.className = 'text-[11px] text-slate-500 hover:text-slate-300';
    advToggle.innerHTML = '<i class="fa-solid fa-chevron-right"></i> Advanced — repo override';
    const advBody = document.createElement('div');
    advBody.className = 'hidden mt-1';
    const repoInput = document.createElement('input');
    repoInput.id = 'df-repo'; repoInput.type = 'text';
    repoInput.value = ctx.deploy_repo || '';
    repoInput.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
    advBody.appendChild(repoInput);
    advToggle.addEventListener('click', () => advBody.classList.toggle('hidden'));
    adv.appendChild(advToggle); adv.appendChild(advBody);
    wrap.appendChild(adv);

    const row = document.createElement('div');
    row.className = 'flex items-center gap-3 mt-1';
    const submit = document.createElement('button');
    submit.className = 'bg-sky-600 hover:bg-sky-500 px-4 py-1.5 rounded-lg text-sm font-medium';
    submit.textContent = 'Deploy to DF UAT';
    const err = document.createElement('span');
    err.className = 'text-[11px] text-red-400';
    submit.addEventListener('click', () => {
        err.textContent = '';
        const image = imgEl.value.trim(), tag = tagEl.value.trim();
        if (!image || !tag) { err.textContent = 'Image name and tag are both required.'; return; }
        const payload = { deployment_type: 'dataflow', environment: 'uat', image: image, tag: tag };
        const repoOverride = repoInput.value.trim();
        if (repoOverride) payload.deployment_repo = repoOverride;
        sendMessage(JSON.stringify(payload));
    });
    row.appendChild(submit); row.appendChild(err);
    wrap.appendChild(row);
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
}

// Deploy editor — shows the ACTUAL current deployment.json as editable JSON
// (pre-filled from /api/deploy-template, which reads the live uat/ or prd file;
// a chart named from a chat command is upserted in). On submit it sends
// {environment, include} through /api/chat; the backend previews the exact JSON
// it will write and asks to confirm.
export async function showDeployForm(env, name, version) {
    if (env === 'df-uat') { showDfDeployForm(); return; }
    if (env === 'release') { showReleaseForm(); return; }
    if (env === 'df-release') { showReleaseForm('df'); return; }
    if (env === 'queue') { showQueueForm(); return; }
    const isProd = env === 'prod';
    const accentT = isProd ? 'text-amber-300' : 'text-emerald-300';
    const accentBtn = isProd ? 'bg-amber-600 hover:bg-amber-500' : 'bg-emerald-600 hover:bg-emerald-500';
    const icon = isProd ? 'fa-shield-halved' : 'fa-flask';
    const heading = isProd ? 'Deploy to PROD' : 'Deploy to UAT';

    // Pre-fill the editor with the WHOLE current deployment.json ({"include":[...]})
    // from the backend (the live uat/ or prd file) — edit entries, add more to deploy
    // several charts at once. The fallback below is only used if the fetch fails.
    let fileDoc = { include: [ { helm_chart_name: name || '', helm_chart_version: version || '' } ] };
    let defaultDeployRepo = '';
    try {
        const qs = new URLSearchParams({ env: env, name: name || '', version: version || '' });
        const r = await fetch(API_BASE + '/api/deploy-template?' + qs.toString());
        if (r.ok) { const d = await r.json(); fileDoc = d.deployment; defaultDeployRepo = d.deploy_repo || ''; }
    } catch (e) {}

    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm';

    const title = document.createElement('div');
    title.className = 'mb-2 font-semibold flex items-center gap-2 ' + accentT;
    const subText = isProd
        ? '— current prd/deployment.json; edit it, then submit STAGES these charts into the PRD release (promotes via SIT→UAT→PRD when released)'
        : '— current uat/deployment.json; edit (add/remove entries), then submit OVERRIDES the file with exactly what you see';
    title.innerHTML = '<i class="fa-solid ' + icon + '"></i> ' + heading +
        ' <span class="text-slate-400 font-normal text-xs">' + subText + '</span>';
    wrap.appendChild(title);

    const taId = 'deploy-json-' + env;
    const ta = document.createElement('textarea');
    ta.id = taId;
    ta.rows = 12;
    ta.spellcheck = false;
    ta.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-xs font-mono text-white focus:outline-none mb-2';
    ta.value = JSON.stringify(fileDoc, null, 2);
    wrap.appendChild(ta);

    // Target deployment repo — part of the deploy JSON payload.
    const repoBox = document.createElement('div');
    repoBox.className = 'mb-2';
    const repoLabel = document.createElement('label');
    repoLabel.className = 'text-[11px] text-slate-400 block mb-0.5';
    repoLabel.textContent = 'Deployment repo (owner/repo)';
    const repoInput = document.createElement('input');
    repoInput.id = 'deploy-repo-' + env;
    repoInput.type = 'text';
    repoInput.placeholder = 'e.g. my-org/deployment-repo';
    repoInput.value = defaultDeployRepo;
    repoInput.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
    repoBox.appendChild(repoLabel); repoBox.appendChild(repoInput);
    wrap.appendChild(repoBox);

    // PROD requires a change request — feeds change-request.json in the release PR.
    if (isProd) {
        const hdr = document.createElement('div');
        hdr.className = 'text-[11px] font-semibold text-amber-300 mt-1 mb-1';
        hdr.textContent = 'Change request (required for PROD)';
        wrap.appendChild(hdr);
        const grid = document.createElement('div');
        grid.className = 'grid gap-2 mb-2';
        const field = (labelText, el, id, type) => {
            if (type) el.type = type;
            el.id = id;
            el.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
            const l = document.createElement('label');
            l.className = 'text-[11px] text-slate-400 block mb-0.5';
            l.textContent = labelText;
            const box = document.createElement('div');
            box.appendChild(l); box.appendChild(el);
            grid.appendChild(box);
        };
        field('Change summary', document.createElement('input'), 'chg-summary-' + env, 'text');
        field('Change description', document.createElement('textarea'), 'chg-desc-' + env);
        field('Start time', document.createElement('input'), 'chg-start-' + env, 'datetime-local');
        field('End time', document.createElement('input'), 'chg-end-' + env, 'datetime-local');
        wrap.appendChild(grid);
    }

    const row = document.createElement('div');
    row.className = 'flex items-center gap-3 mt-1';
    const submit = document.createElement('button');
    submit.className = accentBtn + ' px-4 py-1.5 rounded-lg text-sm font-medium';
    submit.textContent = heading;
    const err = document.createElement('span');
    err.className = 'text-[11px] text-red-400';

    submit.addEventListener('click', () => {
        err.textContent = '';
        const parsed = parseDeployInclude(document.getElementById(taId).value);
        if (!parsed || !parsed.include.length) {
            err.textContent = 'Could not find any chart entries — each needs helm_chart_name + helm_chart_version.';
            return;
        }
        for (const it of parsed.include) {
            if (!it || !it.helm_chart_name || !it.helm_chart_version) {
                err.textContent = 'Each entry needs a non-empty helm_chart_name + helm_chart_version.';
                return;
            }
        }
        const deployRepo = document.getElementById('deploy-repo-' + env).value.trim();
        if (!deployRepo) {
            err.textContent = 'Deployment repo is required (owner/repo).';
            return;
        }
        const payload = { environment: env, include: parsed.include, deployment_repo: deployRepo };
        if (isProd) {
            const summary = document.getElementById('chg-summary-' + env).value.trim();
            const description = document.getElementById('chg-desc-' + env).value.trim();
            const startRaw = document.getElementById('chg-start-' + env).value;
            const endRaw = document.getElementById('chg-end-' + env).value;
            if (!summary || !description || !startRaw || !endRaw) {
                err.textContent = 'PROD requires change summary, description, start time, and end time.';
                return;
            }
            const start = new Date(startRaw), end = new Date(endRaw);
            if (!(end.getTime() > start.getTime())) {
                err.textContent = 'Change end time must be after the start time.';
                return;
            }
            // datetime-local is browser-local; store as ISO-8601 UTC.
            payload.change_request = {
                chg_summary: summary,
                description: description,
                start_date: start.toISOString(),
                end_date: end.toISOString(),
            };
        }
        // Re-render the normalized JSON so the user sees exactly what we parsed
        // (commas added / wrapped into include[] when they left them out).
        document.getElementById(taId).value = JSON.stringify({ include: parsed.include }, null, 2);
        sendMessage(JSON.stringify(payload));
    });
    row.appendChild(submit);
    row.appendChild(err);
    wrap.appendChild(row);

    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
}

// Detect a deploy command typed in the chat box so we can pop the editable
// JSON instead of sending it straight to the agent. Needs a deploy verb, a
// target env, and a <name>:<version> token.
// Regex-free tokenizers (mirror the Python no-regex parsing style).
function _isAlnum(ch) {
    return (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9');
}
function _wordSet(text) {            // lowercased alphanumeric words
    const words = new Set();
    let cur = '';
    for (const ch of text) {
        if (_isAlnum(ch)) { cur += ch.toLowerCase(); }
        else { if (cur) words.add(cur); cur = ''; }
    }
    if (cur) words.add(cur);
    return words;
}
function _wsTokens(text) {           // whitespace-separated raw tokens
    const out = [];
    let cur = '';
    for (const ch of text) {
        if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r') { if (cur) out.push(cur); cur = ''; }
        else { cur += ch; }
    }
    if (cur) out.push(cur);
    return out;
}
export function parseDeployIntent(text) {
    const w = _wordSet(text);
    const hasVerb = w.has('deploy') || w.has('promote') || w.has('ship') ||
                    w.has('rollout') || (w.has('roll') && w.has('out'));
    if (!hasVerb) return null;
    const env = (w.has('prod') || w.has('prd') || w.has('production')) ? 'prod'
              : (w.has('uat') ? 'uat' : null);
    if (!env) return null;
    // Find a <name>:<version> (or name=version) token without regex.
    for (const tok of _wsTokens(text)) {
        let i = tok.indexOf(':');
        if (i === -1) i = tok.indexOf('=');
        if (i <= 0) continue;
        const name = tok.slice(0, i);
        let version = tok.slice(i + 1);
        while (version && '.,;:)'.indexOf(version[version.length - 1]) !== -1) version = version.slice(0, -1);
        const c = name[0];
        if (((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')) && version) {
            return { env: env, name: name, version: version };
        }
    }
    return null;
}

// Tolerant deploy-JSON parser. Accepts a clean {"include":[...]}, a bare array,
// or a single entry; if strict JSON.parse fails (e.g. the user pasted objects
// with no commas and no include[] wrapper), it brace-scans every balanced {...}
// and keeps the chart-entry-shaped ones. Returns {include, recovered} or null.
function _extractJsonObjects(text) {
    const out = [];
    for (let i = 0; i < text.length; i++) {
        if (text[i] !== '{') continue;
        let depth = 0, inStr = false, esc = false, end = -1;
        for (let j = i; j < text.length; j++) {
            const ch = text[j];
            if (inStr) { if (esc) esc = false; else if (ch === '\\') esc = true; else if (ch === '"') inStr = false; continue; }
            if (ch === '"') inStr = true;
            else if (ch === '{') depth++;
            else if (ch === '}') { depth--; if (depth === 0) { end = j; break; } }
        }
        if (end === -1) break;
        try {
            const e = JSON.parse(text.slice(i, end + 1));
            if (e && typeof e === 'object' && !Array.isArray(e) &&
                (e.helm_chart_name !== undefined || e.helm_chart_version !== undefined)) {
                out.push(e);
            }
        } catch (_) { /* this {...} isn't a standalone object — skip */ }
    }
    return out;
}
export function parseDeployInclude(text) {
    text = (text || '').trim();
    try {
        const doc = JSON.parse(text);
        if (Array.isArray(doc)) return { include: doc, recovered: false };
        if (doc && Array.isArray(doc.include)) return { include: doc.include, recovered: false };
        if (doc && typeof doc === 'object' && doc.helm_chart_name !== undefined) return { include: [doc], recovered: false };
    } catch (_) { /* fall through to lenient recovery */ }
    const entries = _extractJsonObjects(text);
    return entries.length ? { include: entries, recovered: true } : null;
}
