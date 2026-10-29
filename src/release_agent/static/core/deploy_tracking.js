// The release-notification report's wording (/api/deploy-tracking). Pure: no
// DOM, no fetch — the card and a Backstage port read the same sentences. The
// server reports facts (checks, rows, runs); this decides how each reads and
// whether it is fine, a warning, or the reason nothing was sent.

/** @typedef {{state: 'ok'|'warn'|'problem'|'info', text: string, url?: string}} Line */

const short = (sha) => String(sha || '').slice(0, 7);

/** One check of the report as a line. ok true → ok, false → problem, null → warn. */
export function checkLine(c) {
    const state = c.ok === true ? 'ok' : c.ok === false ? 'problem' : 'warn';
    return { state, text: c.name + ' — ' + (c.detail || '') };
}

/** What one Start or Complete dispatch did, from what the tracker recorded. */
export function sentLine(label, sent) {
    if (!sent) return null;
    if (sent.run_id) return { state: 'ok', text: label + ' sent', url: sent.run_url || '' };
    if (sent.error) return { state: 'problem', text: label + ' NOT sent — the dispatch failed: ' + sent.error };
    if (sent.skipped) return { state: 'warn', text: label + ' not sent — ' + sent.skipped };
    return { state: 'warn', text: label + ': dispatched with no run id recorded — ' +
        'NOTIFY_WORKFLOW may have been empty then (older rows do not say)' };
}

/** While a merged PR waits for its runs: what is on the merge commit and what counts. */
export function waitingLine(entry) {
    if (entry.runs_error) return { state: 'warn', text: 'Could not read the runs on the merge commit: ' + entry.runs_error };
    const runs = entry.runs_now;
    if (!runs) return { state: 'info', text: 'Waiting for the deployment runs on ' + short(entry.merge_sha) };
    const counting = runs.filter(r => r.counts);
    if (!runs.length) {
        return { state: 'warn', text: 'No workflow run on the merge commit ' + short(entry.merge_sha) +
            ' yet — Complete is sent when the deployment runs there conclude' };
    }
    if (!counting.length) {
        return { state: 'problem', text: 'No run on the merge commit matches DEPLOY_WORKFLOWS, so Complete is ' +
            'never sent. Runs there: ' + runs.map(r => r.workflow || r.name).join(', ') };
    }
    const open = counting.filter(r => !r.conclusion);
    if (open.length) {
        return { state: 'info', text: 'Deployment running — ' + open.length + ' of ' + counting.length +
            ' run' + (counting.length > 1 ? 's' : '') + ' not finished (' + open.map(r => r.name).join(', ') + ')',
            url: open[0].url || '' };
    }
    return { state: 'info', text: 'All runs concluded — Complete goes out on the next check' };
}

/** Everything that happened to one release PR, in order, as lines. */
export function prStory(entry) {
    if (entry.state === 'unseen') {
        return [{ state: 'problem', text: 'Merged, but the tracker never saw this PR open, so nothing was sent. ' +
            'Tracking was off or not running while it was open, or the pod restarted with the memory store.' }];
    }
    const lines = [];
    if (entry.state === 'watching') {
        lines.push({ state: 'info', text: 'Open — Start is sent when it merges' });
        return lines;
    }
    if (entry.state === 'closed') {
        lines.push({ state: 'info', text: 'Closed without a merge — nothing is sent' });
        return lines;
    }
    const start = sentLine('Start', entry.start);
    if (start) lines.push(start);
    if (entry.state === 'merged') lines.push(waitingLine(entry));
    const finished = (entry.steps || []).filter(s => s.type === 'finished').pop();
    if (entry.state === 'finished' && finished) {
        if (finished.conclusion === 'success') {
            lines.push({ state: 'ok', text: 'Deployment succeeded (' + (finished.workflow || 'run') + ')',
                         url: finished.run_url || '' });
            const done = sentLine('Complete', entry.complete);
            if (done) lines.push(done);
        } else {
            const at = (finished.failed_steps || [])[0];
            lines.push({ state: 'problem', text: 'Deployment failed' + (at ? ' at ' + at.job + ' › ' + at.step : '') +
                ' — a failure sends no notification', url: finished.run_url || '' });
        }
    }
    if (entry.state === 'abandoned_tracking') {
        const stop = (entry.steps || []).filter(s => s.type === 'abandoned_tracking').pop();
        lines.push({ state: 'problem', text: 'Stopped following' + (stop && stop.note ? ': ' + stop.note : '') +
            ' — no Complete is sent (TRACK_RUN_MAX_MINUTES)' });
    }
    return lines;
}

/** The card's one-line answer: is anything stopping notifications? */
export function overall(report) {
    if (!report || report.ok === false) {
        return { state: 'problem', text: 'The report could not be read' +
            (report && report.error ? ': ' + report.error : '') };
    }
    const failing = (report.checks || []).find(c => c.ok === false);
    if (failing) return { state: 'problem', text: failing.name + ': ' + failing.detail };
    if (report.last_error) return { state: 'problem', text: 'The last check failed: ' + report.last_error };
    const prs = report.prs || [];
    const bad = prs.filter(p => prStory(p).some(l => l.state === 'problem'));
    if (bad.length) {
        return { state: 'warn', text: bad.length + ' release PR' + (bad.length > 1 ? 's' : '') +
            ' did not get every notification — see below' };
    }
    return { state: 'ok', text: 'Everything a notification needs is in place' };
}

/** "CARE PRL1 · CHG0099001 · PR #12" — how a PR is named on the card. */
export function prHeading(entry) {
    const env = String(entry.env || entry.base || '').toUpperCase();
    return [env, entry.chg, entry.rmg].filter(Boolean).join(' · ') + (env || entry.chg ? ' — ' : '') +
        'PR #' + entry.pr_number;
}
