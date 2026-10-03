// Wording and payload for "Was this right?" under an investigation's answer —
// shared with the Backstage port, so pure and tested in
// tests/js/support_feedback.test.mjs. The server decides what is stored
// (tools/support_feedback.py: validation, caps, who answered — the verified
// caller, never a field sent from here); this only builds the request and says
// the words. Every function takes a payload with fields missing and still answers.

function num(v) {
    const n = Number(v);
    return v === null || v === undefined || v === '' || !Number.isFinite(n) ? 0 : n;
}

function text(v) {
    return v === null || v === undefined ? '' : String(v);
}

/** The three answers, in the order the buttons show them. */
export const VERDICTS = [
    { key: 'right', label: 'Right' },
    { key: 'direction', label: 'Right direction' },
    { key: 'wrong', label: 'Wrong' },
];

/** The server's cap on the cause (tools/support_feedback.MAX_CAUSE) — the input
 *  stops there so nothing the person typed is silently cut. */
export const MAX_CAUSE = 500;

export const CAUSE_PROMPT = 'What was the actual cause?';

/** Only these answers ask for the real cause: "right" has nothing to correct. */
export function asksForCause(verdict) {
    return verdict === 'direction' || verdict === 'wrong';
}

/** The POST body for an investigation event ({business_date, incident_id, title,
 *  model, model_calls, seconds, …}) and the person's answer. The cause is sent
 *  as typed (trimmed) and only with an answer that asks for it. */
export function feedbackPayload(data, verdict, actualCause) {
    const d = data && typeof data === 'object' ? data : {};
    const body = {
        business_date: text(d.business_date),
        incident_id: text(d.incident_id),
        title: text(d.title),
        verdict: text(verdict),
        actual_cause: asksForCause(verdict) ? text(actualCause).trim() : '',
        category: text(d.category),
        action: text(d.action),
        model: text(d.model),
    };
    // Timings are context, not the answer: omitted when the event had none.
    if (d.model_calls !== null && d.model_calls !== undefined && d.model_calls !== '') body.model_calls = num(d.model_calls);
    if (d.seconds !== null && d.seconds !== undefined && d.seconds !== '') body.seconds = num(d.seconds);
    return body;
}

/** What replaces the buttons once the answer is saved. */
export function thanksText(verdict) {
    if (verdict === 'right') return 'Thanks — marked right.';
    if (verdict === 'direction') return 'Thanks — marked right direction. What you wrote helps the next investigation.';
    if (verdict === 'wrong') return 'Thanks — marked wrong. What you wrote is kept for the team\'s runbook.';
    return 'Thanks for the feedback.';
}

function percent(part, whole) {
    return Math.round((100 * part) / whole) + '%';
}

/** "30 days: 42 investigations rated · 55% right · 86% right or right direction". */
export function statsLine(stats) {
    const s = stats && typeof stats === 'object' ? stats : {};
    if (s.disabled) return 'Feedback is not being kept';
    if (s.ok === false) return 'Feedback is unavailable';
    const days = num(s.days) || 30;
    const head = days + (days === 1 ? ' day' : ' days') + ': ';
    const total = num(s.total);
    if (!total) return head + 'no investigations rated yet';
    // The server sends ratios; fall back to the counts when only those came.
    const right = s.accuracy !== null && s.accuracy !== undefined ? num(s.accuracy) : num(s.right) / total;
    const near = s.right_or_direction !== null && s.right_or_direction !== undefined
        ? num(s.right_or_direction) : (num(s.right) + num(s.direction)) / total;
    return head + total + (total === 1 ? ' investigation' : ' investigations') + ' rated · ' +
        percent(right, 1) + ' right · ' + percent(near, 1) + ' right or right direction';
}
