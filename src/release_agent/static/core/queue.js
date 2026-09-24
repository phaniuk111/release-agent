// The release-queue rules both UIs must agree on. Pure: no DOM, no fetch — the
// Node tests import it directly and a React port (the Backstage plugin
// included) can use it as is. Before this module the same rules lived in the
// queue form, the release form, the queue table and the Backstage plugin, and
// every change had to land in all four.
//
// The developer answers two POSITIVE questions per chart — which release
// carries it (CARE or Dataflow) and how far it goes (PRD and/or PRL1). The API
// stores the two negatives it always took plus the full selection:
//   CARE + PRD (+PRL1) -> df_only=false, prl1_only=false  (UAT, PRL1 and PRD)
//   CARE + PRL1 only   -> df_only=false, prl1_only=true   (UAT and PRL1, never PRD)
//   Dataflow           -> df_only=true                    (no helm deploy at all)
// target_envs keeps "prd", "prl1" or "prd,prl1": for a Dataflow image the
// pipeline is chosen at deploy time, and prl1_only cannot say "both".

/** @typedef {{df: boolean, prd: boolean, prl1: boolean}} Ticks */

/** The API fields for a set of ticks. @param {Ticks} t */
export function ticksToFlags(t) {
    return {
        df_only: !!t.df,
        // Only PRL1 WITHOUT PRD restricts the chart.
        prl1_only: !!t.prl1 && !t.prd,
        target_envs: [t.prd && 'prd', t.prl1 && 'prl1'].filter(Boolean).join(','),
    };
}

/** Why these ticks cannot be submitted, or '' when they can. @param {Ticks} t */
export function tickProblem(t) {
    if (!t.df && !t.prd && !t.prl1) return 'Tick where it goes: PRD or PRL1.';
    return '';
}

/**
 * The one-line hint beside the tick boxes — where these ticks actually send
 * the chart. For CARE, PRD alone records the pipeline to trigger; it does NOT
 * hold the chart out of the PRL1 file-set (prl1_only has only two states).
 * @param {Ticks} t
 */
export function tickHint(t) {
    const picked = [t.prd && 'PRD', t.prl1 && 'PRL1'].filter(Boolean);
    if (t.df) {
        return picked.length
            ? picked.join(' + ') + ' pipeline' + (picked.length > 1 ? 's' : '') + ' — triggered at deploy time'
            : '';
    }
    if (t.prd && t.prl1) return 'UAT → PRL1 → PRD';
    if (t.prd) return 'PRD pipeline — release files still cover PRL1';
    if (t.prl1) return 'UAT → PRL1, never PRD';
    return '';
}

/** The environments a queued row names, e.g. ["prd", "prl1"]. */
export function envsOf(q) {
    return String((q && q.target_envs) || '').split(',').map(e => e.trim()).filter(Boolean);
}

/** How a queued row reads in a table — the same words as the Backstage queue tab. */
export function queueDestination(q) {
    const picked = envsOf(q);
    const envs = ['prd', 'prl1'].filter(e => picked.includes(e)).map(e => e.toUpperCase());
    if (q.df_only) return envs.length ? 'DF → ' + envs.join(', ') : 'DF (Dataflow)';
    return q.prl1_only ? 'CARE → UAT, PRL1' : 'CARE → UAT, PRL1, PRD';
}

/** How a queued row reads inside a release form of that kind (routing only). */
export function releaseRouteText(q, isDf) {
    if (isDf) {
        // Same order as the queue form's tick boxes (PRD, then PRL1).
        const envs = ['prd', 'prl1'].filter(e => envsOf(q).includes(e)).map(e => e.toUpperCase());
        return envs.length
            ? envs.join(' + ') + ' pipeline' + (envs.length > 1 ? 's' : '') + ' — triggered at deploy time'
            : 'pipelines chosen at deploy time';
    }
    return q.prl1_only ? 'UAT → PRL1 · PRL1-only, held back from PRD' : 'UAT → PRL1 → PRD';
}

/** Queued rows belonging to one release: a DF release carries only DF images. */
export function forRelease(queue, isDf) {
    return (queue || []).filter(q => (isDf ? !!q.df_only : !q.df_only));
}

/**
 * Everything wrong with a queue submission, as sentences — every bad row at
 * once, named by chart, so one attempt tells the developer everything to fix.
 * Rows with nothing typed are not part of the submission.
 * @param {{chart: string, version: string, run: string, jira: string}[]} rows
 * @param {{email: string, details: string, ticks: Ticks}} shared
 * @returns {{filled: object[], problems: string[]}}
 */
export function queueSubmissionProblems(rows, shared) {
    const clean = (rows || []).map(r => ({
        chart: String(r.chart || '').trim(), version: String(r.version || '').trim(),
        run: String(r.run || '').trim(), jira: String(r.jira || '').trim(),
    }));
    const filled = clean.filter(r => r.chart || r.version || r.run || r.jira);
    if (!filled.length) return { filled, problems: ['Add at least one chart.'] };
    const problems = [];
    filled.forEach((r, i) => {
        const miss = [[r.chart, 'chart name'], [r.version, 'version'], [r.jira, 'JIRA ticket']]
            .filter(pair => !pair[0]).map(pair => pair[1]);
        if (!r.run || r.run.indexOf('/actions/runs/') === -1) miss.push('build run URL (…/actions/runs/<id>)');
        if (miss.length) problems.push((r.chart || 'row ' + (i + 1)) + ': ' + miss.join(', '));
    });
    const sharedMissing = [[String(shared.email || '').trim(), 'your email'],
                           [String(shared.details || '').trim(), 'change details']]
        .filter(pair => !pair[0]).map(pair => pair[1]);
    if (sharedMissing.length) problems.push('Still needed: ' + sharedMissing.join(', '));
    const tick = tickProblem(shared.ticks || {});
    if (tick) problems.push(tick);
    return { filled, problems };
}

/** The /api/release-queue/batch row for one filled form row. */
export function batchRow(row, ticks) {
    return {
        artifact: row.chart + ':' + row.version,
        build_run_url: row.run,
        jira_ticket: row.jira,
        ...ticksToFlags(ticks),
    };
}

/** "RCTLDEF0001691 - Peer review evidence in job x" → "1691" (the ID's number). */
function controlNumber(entry) {
    const id = String(entry || '').trim().split(/[\s-]/)[0] || '';
    let digits = '';
    for (let i = id.length - 1; i >= 0 && id[i] >= '0' && id[i] <= '9'; i--) digits = id[i] + digits;
    return digits ? String(parseInt(digits, 10)) : id;
}

/**
 * Was this version traced back to the run that BUILT it, when it was queued —
 * the queue table's Build column and the release form's tick-list badge, which
 * used to say three different things about the same field.
 * Three states, because "not checked" and "checked, nothing found" are not the
 * same answer: a screen may show nothing for the first and must not claim the
 * build failed verification when it was never looked at.
 * @param {{build_verified?: boolean|null}} q
 * @returns {{state: 'verified'|'unverified'|'unknown', label: string, title: string}}
 */
export function buildSummary(q) {
    const v = q ? q.build_verified : null;
    if (v === true) {
        return { state: 'verified', label: 'verified',
                 title: 'traced to the GitHub Actions run that built this version, at queue time' };
    }
    if (v === false) {
        return { state: 'unverified', label: 'not verified',
                 title: 'no build run could be traced to this version at queue time' };
    }
    return { state: 'unknown', label: 'not checked',
             title: 'the build was not checked when this was queued' };
}

/**
 * The release queue's Controls column — the ONE place an allowed control shows.
 * It failed on the build run but may be a false positive, so it reads "open"
 * (to close by hand), never "failed"; the release itself is not stopped.
 * @param {{build_verified?: boolean|null, allowed_failures?: string}} q
 * @returns {{state: 'passed'|'open'|'unknown', label: string, title: string}}
 */
export function controlsSummary(q) {
    const allowed = String((q && q.allowed_failures) || '').split(',').map(s => s.trim()).filter(Boolean);
    if (allowed.length) {
        return { state: 'open', label: allowed.map(controlNumber).join(', ') + ' open',
                 title: allowed.join('\n') + '\n— failed on the build run, possibly a false positive: ' +
                        'close it manually. Every other control passed.' };
    }
    if (q && q.build_verified === true) {
        return { state: 'passed', label: 'all passed', title: 'every release control passed on the build run' };
    }
    return { state: 'unknown', label: 'not checked', title: 'no verified build run at queue time' };
}

/**
 * What to do with the charts ticked in the release history. A chart that went
 * through the gate once (at that version) goes back DIRECTLY — the run it was
 * verified against has not changed, so `direct` names it for
 * /api/release-queue/requeue. A chart that never went through the queue never
 * qualified: it must take the gate like a first submission, so `gated` is its
 * /api/release-queue/batch row — and only once it has a run and a ticket.
 * Whatever cannot go either way is in `skipped` with the reason, never dropped
 * in silence.
 * @param {{artifact_name: string, artifact_version?: string, from_queue?: boolean, in_queue?: boolean,
 *          build_run_url?: string, jira_ticket?: string, prl1_only?: boolean, df_only?: boolean,
 *          target_envs?: string, change_details?: string, note?: string}[]} items
 * @returns {{direct: {artifact_name: string, artifact_version: string}[], gated: object[],
 *            skipped: {artifact: string, reason: string}[]}}
 */
export function requeuePlan(items) {
    const direct = [], gated = [], skipped = [];
    for (const it of items || []) {
        const artifact = it.artifact_name + ':' + (it.artifact_version || '');
        if (it.in_queue) { skipped.push({ artifact, reason: 'already queued for the next release' }); continue; }
        if (it.from_queue) { direct.push({ artifact_name: it.artifact_name, artifact_version: it.artifact_version || '' }); continue; }
        if (!it.build_run_url) {
            skipped.push({ artifact, reason: 'never went through the queue — paste the run that built it in the Build column, then tick' });
            continue;
        }
        if (!it.jira_ticket) {
            skipped.push({ artifact, reason: 'never went through the queue — the gate needs a JIRA ticket; add it in the JIRA column, then tick' });
            continue;
        }
        gated.push({ artifact, build_run_url: it.build_run_url, jira_ticket: it.jira_ticket || '',
                     prl1_only: !!it.prl1_only, df_only: !!it.df_only, target_envs: it.target_envs || '',
                     change_details: it.change_details || '', note: it.note || '' });
    }
    return { direct, gated, skipped };
}
