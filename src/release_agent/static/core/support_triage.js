// Wording for the Support triage pill (L1 view) — shared with the Backstage
// port, so pure and tested in tests/js/support_triage.test.mjs. The server
// decides (tools/support_triage.py: what failed, the category, the action, the
// owner, the ticket note); this only says it. Every function takes a payload
// with fields missing and still answers.

function num(v) {
    const n = Number(v);
    return v === null || v === undefined || v === '' || !Number.isFinite(n) ? 0 : n;
}

/** What each L1 action is called on the card. */
export const ACTION_LABELS = {
    wait: 'Wait for data',
    retrigger: 'Re-trigger once',
    check: 'Check',
    escalate: 'Escalate',
};

export function actionLabel(action) {
    return ACTION_LABELS[action] || 'Check';
}

/** "COB 2026-10-02 · 42 failed · 3 stuck · 5 missing of 610 runs", or "… all clear". */
export function summaryLine(report) {
    const r = report && typeof report === 'object' ? report : {};
    const label = String(r.date_label || 'Business date');
    if (r.disabled) return 'Support triage is disabled';
    if (r.ok === false) return 'Support triage is unavailable';
    if (!r.business_date) return 'no runs in the dates read';
    const c = r.counts && typeof r.counts === 'object' ? r.counts : {};
    const head = label + ' ' + r.business_date;
    if (!num(c.failed) && !num(c.stuck) && !num(c.missing)) {
        return head + ' · all clear · ' + num(c.items) + (num(c.items) === 1 ? ' run' : ' runs');
    }
    const parts = [];
    if (num(c.failed)) parts.push(num(c.failed) + ' failed');
    if (num(c.stuck)) parts.push(num(c.stuck) + ' stuck');
    if (num(c.missing)) parts.push(num(c.missing) + ' missing');
    return head + ' · ' + parts.join(' · ') + ' of ' + num(c.items) + (num(c.items) === 1 ? ' run' : ' runs');
}

/** A line under the summary: retries that recovered and what was read. */
export function metaLine(report) {
    const r = report && typeof report === 'object' ? report : {};
    const parts = [];
    const c = r.counts || {};
    if (num(c.recovered)) parts.push(num(c.recovered) + ' recovered after a retry');
    if (r.previous_date) parts.push('compared with ' + r.previous_date);
    if (num(r.runbook_entries)) parts.push(num(r.runbook_entries) + ' runbook entries');
    else if (r.ok !== false && !r.disabled) parts.push('no runbook configured — built-in actions');
    if (r.scanned_at) parts.push('read ' + r.scanned_at);
    return parts.join(' · ');
}

/** A job id as a link when SUPPORT_JOB_URL is set ("{job_id}" replaced), else "". */
export function jobHref(template, jobId) {
    const t = String(template || '');
    const id = String(jobId || '');
    if (!t || !id || t.indexOf('{job_id}') === -1) return '';
    if (!(t.startsWith('https://') || t.startsWith('http://'))) return '';
    return t.split('{job_id}').join(encodeURIComponent(id));
}

/** What the chat is asked from an incident's "Ask why" — the model reads the
 *  same report through the support_triage tool; this names the incident. */
export function askPrompt(incident, report) {
    const inc = incident && typeof incident === 'object' ? incident : {};
    const date = report && report.business_date ? ' for ' + report.business_date : '';
    return 'Support triage' + date + ': explain incident "' + String(inc.title || '?') +
        '" — why it most likely happened, what L1 should do now, and the ticket note.';
}

/** "Empty" wording when a date has nothing to triage. */
export function emptyText(report) {
    const r = report && typeof report === 'object' ? report : {};
    if (r.empty && r.business_date) return 'No runs for ' + r.business_date + ' in the control table.';
    if (r.empty) return 'No runs in the dates read.';
    return 'Nothing for L1 on this date.';
}

export const ENABLE_HINT = 'To enable it, set SUPPORT_TABLE (project.dataset.table) and SUPPORT_COLUMNS ' +
    '(role=column, at least date, run_id and status) in your private values file; the service account needs ' +
    'BigQuery Data Viewer on that table and Job User on the project.';
