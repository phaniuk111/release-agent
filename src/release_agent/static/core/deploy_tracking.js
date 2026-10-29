// The release-notification card's wording (/api/deploy-tracking). Pure: no DOM,
// no fetch — the card and a Backstage port read the same sentences. The server
// reports facts (checks, this week's open release PRs); this decides how each
// reads and whether it is fine, a warning, or the reason nothing would be sent.

/** @typedef {{state: 'ok'|'warn'|'problem'|'info', text: string}} Line */

/** One check of the report as a line. ok true → ok, false → problem, null → warn. */
export function checkLine(c) {
    const state = c.ok === true ? 'ok' : c.ok === false ? 'problem' : 'warn';
    return { state, text: c.name + ' — ' + (c.detail || '') };
}

/** What will happen to one open release PR when it merges. */
export function openPrLine(pr, everyMinutes) {
    if (!pr.tracking) return { state: 'problem', text: 'Tracking is off — nothing is sent when this merges' };
    if (pr.watched) return { state: 'ok', text: 'Watched — Start is sent when it merges, Complete when its deployment succeeds' };
    return { state: 'warn', text: 'Not seen by the tracker yet — it is picked up on the next check' +
        (everyMinutes ? ' (every ' + everyMinutes + ' min)' : '') };
}

/** The card's one-line answer: would a notification go out right now? */
export function overall(report) {
    if (!report || report.ok === false) {
        return { state: 'problem', text: 'The report could not be read' +
            (report && report.error ? ': ' + report.error : '') };
    }
    const failing = (report.checks || []).find(c => c.ok === false);
    if (failing) return { state: 'problem', text: failing.name + ': ' + failing.detail };
    const n = (report.prs || []).length;
    return { state: 'ok', text: 'Everything a notification needs is in place' +
        (n ? ' · ' + n + ' release PR' + (n > 1 ? 's' : '') + ' open this week' : '') };
}

/** "PRL1 · CHG0099001 · RMG0099001 — PR #12" — how a PR is named on the card. */
export function prHeading(entry) {
    const env = String(entry.env || entry.base || '').toUpperCase();
    return [env, entry.chg, entry.rmg].filter(Boolean).join(' · ') + (env || entry.chg ? ' — ' : '') +
        'PR #' + entry.pr_number;
}

/** "Mon 26 Oct" — the week the list starts from. */
export function weekLabel(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return 'this week';
    const day = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getUTCDay()];
    const mon = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getUTCMonth()];
    return day + ' ' + d.getUTCDate() + ' ' + mon;
}
