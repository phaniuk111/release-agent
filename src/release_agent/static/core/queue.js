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
