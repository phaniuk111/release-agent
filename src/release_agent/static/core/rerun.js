// Re-run a job — the words and rules the card shows (design/RERUN_JOB.md).
// Pure: no DOM, no fetch, so the Backstage plugin can reuse them. The answers
// themselves come from src/release_agent/tools/rerun_job.py.

// A preview answer → how it reads. `tone` is one of ok | info | warn | bad.
const PREVIEW = {
    rerun:      { tone: 'info', label: 'will re-run' },
    passed:     { tone: 'ok',   label: 'already passed' },
    running:    { tone: 'warn', label: 'still running' },
    too_old:    { tone: 'warn', label: 'too old' },
    protected:  { tone: 'bad',  label: 'refused' },
    ambiguous:  { tone: 'warn', label: 'ambiguous' },
    no_job:     { tone: 'warn', label: 'no such job' },
    unreadable: { tone: 'bad',  label: 'unreadable' },
    over_cap:   { tone: 'warn', label: 'not in this batch' },
};

const RESULT = {
    pending:      { tone: 'info', label: 'waiting to start' },
    running:      { tone: 'info', label: 'running' },
    passed:       { tone: 'ok',   label: 'passed' },
    failed_again: { tone: 'bad',  label: 'failed again' },
    not_started:  { tone: 'warn', label: 'not started' },
    unfollowed:   { tone: 'warn', label: 'not followed to the end' },
    skipped:      { tone: 'muted', label: 'not re-run' },
};

/** @param {{state?: string, job?: string}} row */
export function previewTag(row) {
    const s = PREVIEW[(row && row.state) || ''] || { tone: 'warn', label: (row && row.state) || 'unknown' };
    if (row && row.state === 'rerun' && row.job) return { tone: s.tone, label: 'will re-run ' + row.job };
    return s;
}

/** @param {{result?: string}} row */
export function resultTag(row) {
    return RESULT[(row && row.result) || ''] || { tone: 'muted', label: (row && row.result) || 'unknown' };
}

function hhmm(epoch) {
    const n = Number(epoch);
    if (!n) return '';
    const d = new Date(n * 1000);
    return String(d.getUTCHours()).padStart(2, '0') + ':' + String(d.getUTCMinutes()).padStart(2, '0') + ' UTC';
}

/** What the plan costs and whether the token can afford it — one sentence. */
export function budgetLine(budget, runs) {
    const n = Number(runs) || 0;
    if (!budget || !n) return '';
    if (!budget.known) return 'About ' + budget.needed + ' GitHub API calls; the token\'s budget could not be read.';
    const head = 'About ' + budget.needed + ' GitHub API calls · ' + budget.remaining + ' left, resets at ' +
        hhmm(budget.reset) + '.';
    if (budget.fits >= n) return head;
    if (budget.fits <= 0) return head + ' Not enough to start any now — wait for the reset.';
    return head + ' Only ' + budget.fits + ' of ' + n + ' will start; the rest are kept for after the reset.';
}

/** The yes/no question, or '' when nothing would start. */
export function confirmQuestion(plan) {
    const runs = (plan && plan.runs) || [];
    if (!plan || !plan.plan_id || !runs.length) return '';
    // The job the name resolved to — "1691" asks for RCTLDEF… or sdlc-peer-review.
    const names = [...new Set(runs.map(r => r.job).filter(Boolean))];
    const what = names.length === 1 ? names[0] : plan.job;
    return 'Re-run ' + what + ' in ' + runs.length + ' run' + (runs.length === 1 ? '' : 's') + '?';
}

/** "10 images → 8 re-runs (3 share one run). 2 will not be started." */
export function previewHeadline(plan) {
    const rows = (plan && plan.rows) || [];
    const runs = (plan && plan.runs) || [];
    const going = rows.filter(r => r.state === 'rerun');
    const shared = going.filter(r => r.shared).length;
    const not = rows.length - going.length;
    return rows.length + ' image' + (rows.length === 1 ? '' : 's') + ' → ' + runs.length + ' re-run' +
        (runs.length === 1 ? '' : 's') + (shared ? ' (' + shared + ' share a run)' : '') + '.' +
        (not ? ' ' + not + ' will not be started.' : '');
}

/** Is the batch over (nothing more will change on its own)? */
export function isDone(batch) {
    return !!batch && (batch.status === 'finished' || batch.status === 'stopped');
}

/**
 * The images a follow-up re-run would take: those that failed again, or those
 * a limit or an error kept from starting. Unique, in the batch's order.
 * @param {{rows?: {artifact: string, result?: string}[]}} batch
 * @param {'failed_again'|'not_started'} which
 */
export function imagesFor(batch, which) {
    const seen = new Set();
    return ((batch && batch.rows) || [])
        .filter(r => r.result === which && r.artifact && !seen.has(r.artifact) && seen.add(r.artifact))
        .map(r => r.artifact);
}

/** The one line the batch's header says while it runs and after. */
export function batchHeadline(batch) {
    if (!batch) return '';
    if (batch.status === 'starting') return 'Starting the re-runs…';
    if (batch.status === 'following') return batch.title || 'Watching the re-runs';
    return batch.title || '';
}

// Groups, in the order a person reads them: for a preview what WILL happen
// first, then each reason something will not; for a result the problems first.
const PREVIEW_ORDER = ['rerun', 'passed', 'running', 'too_old', 'protected', 'ambiguous', 'no_job', 'unreadable', 'over_cap'];
const RESULT_ORDER = ['failed_again', 'not_started', 'unfollowed', 'running', 'pending', 'passed', 'skipped'];

/**
 * Rows grouped by outcome, and within a group by reason, so a reason is said
 * once for every image it applies to.
 * @param {object[]} rows  preview rows (state) or batch rows (result)
 * @param {'preview'|'result'} kind
 * @returns {{key: string, tone: string, label: string, count: number, quiet: boolean,
 *            reasons: {text: string, rows: object[]}[]}[]}
 */
export function groupRows(rows, kind) {
    const order = kind === 'preview' ? PREVIEW_ORDER : RESULT_ORDER;
    const field = kind === 'preview' ? 'state' : 'result';
    const groups = new Map();
    for (const r of rows || []) {
        const key = r[field] || 'unknown';
        if (!groups.has(key)) groups.set(key, new Map());
        // A re-run's reason is the job (and any caution); anything else says its detail.
        const why = kind === 'preview' && key === 'rerun' ? (r.caution || '') : (r.detail || '');
        const reasons = groups.get(key);
        if (!reasons.has(why)) reasons.set(why, []);
        reasons.get(why).push(r);
    }
    const rank = (k) => (order.indexOf(k) < 0 ? order.length : order.indexOf(k));
    return [...groups.keys()].sort((a, b) => rank(a) - rank(b)).map(key => {
        const reasons = [...groups.get(key)].map(([text, rs]) => ({ text, rows: rs }));
        const count = reasons.reduce((n, x) => n + x.rows.length, 0);
        const first = reasons[0].rows[0];
        const tag = kind === 'preview' ? previewTag(first) : resultTag(first);
        // Quiet groups fold away: nothing for the person to act on.
        const quiet = kind === 'preview' ? key === 'passed' : key === 'passed' || key === 'skipped';
        return { key, tone: tag.tone, label: tag.label, count, quiet, reasons };
    });
}

/** "12 of 23 started" / "15 of 23 finished" — progress through the batch. */
export function progressLine(batch) {
    const runs = (batch && batch.runs) || [];
    const n = runs.length;
    if (!n) return '';
    if (batch.status === 'starting') {
        const sent = runs.filter(r => r.state !== 'pending').length;
        return sent + ' of ' + n + ' sent';
    }
    const done = runs.filter(r => r.state !== 'started' && r.state !== 'pending').length;
    return done + ' of ' + n + ' run' + (n === 1 ? '' : 's') + ' settled';
}
