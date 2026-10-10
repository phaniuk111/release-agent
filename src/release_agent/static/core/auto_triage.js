// Auto-triage's feed: the pure rules the card, the strip and the Insights
// section render (grouping, the watcher's health, the strip's one line, what
// a finding offers a person). Shared with the Backstage port, so pure and
// tested in tests/js/auto_triage.test.mjs. The server decides every finding
// (tools/support/watch.py); nothing here judges a cause.

const URGENT = new Set(['critical', 'high']);

function list(findings) {
    return Array.isArray(findings) ? findings.filter(f => f && typeof f === 'object') : [];
}

/** Needs attention (open, high or critical), then the other open ones, then the
 *  closed — resolved, dismissed and cleared. Empty groups are left out. */
export function groupFindings(findings) {
    const all = list(findings);
    const open = all.filter(f => f.status === 'open');
    return [
        { key: 'attention', label: 'Needs attention', items: open.filter(f => URGENT.has(f.priority || '')) },
        { key: 'open', label: 'Open', items: open.filter(f => !URGENT.has(f.priority || '')) },
        { key: 'closed', label: 'Resolved, dismissed and cleared', items: all.filter(f => f.status !== 'open') },
    ].filter(g => g.items.length > 0);
}

/** {attention, open}: what the strip and the tiles count. */
export function counts(findings) {
    const open = list(findings).filter(f => f.status === 'open');
    return { attention: open.filter(f => URGENT.has(f.priority || '')).length, open: open.length };
}

function minutesSince(iso, now) {
    if (!iso) return null;
    const t = Date.parse(iso);
    return Number.isNaN(t) ? null : Math.max(0, Math.round((now - t) / 60000));
}

export function ago(minutes) {
    if (minutes < 1) return 'just now';
    if (minutes < 60) return minutes + ' min ago';
    const h = Math.round(minutes / 60);
    return h < 48 ? h + ' h ago' : Math.round(h / 24) + ' days ago';
}

/** "HH:MM" (UTC, like the release banner) from an ISO time; '' when there is none. */
export function hhmm(iso) {
    if (!iso) return '';
    const t = new Date(iso);
    return Number.isNaN(t.getTime()) ? '' : t.toISOString().slice(11, 16);
}

/**
 * One line on whether Auto-triage is doing its job: {tone: ok|warn|error|off,
 * text, detail}. A watcher that stopped must be obvious: no run for twice its
 * interval is a warning, not silence.
 */
export function watcherHealth(status, now = Date.now()) {
    const s = status && typeof status === 'object' ? status : {};
    const since = minutesSince(s.last_run_at, now);
    const every = Number(s.interval_minutes) || 0;
    if (s.running) return { tone: 'ok', text: 'Running now', detail: 'Investigating new problems' };
    if (s.error) return { tone: 'error', text: 'Last run failed', detail: String(s.error) };
    if (s.configured === false) {
        return { tone: 'off', text: 'Not configured', detail: 'Set SUPPORT_TABLE — Auto-triage reads what Support triage reads' };
    }
    if (!s.enabled) {
        return {
            tone: 'off',
            text: since === null ? 'Not scheduled' : 'Ran ' + ago(since),
            detail: 'Runs only when you press Run now (SUPPORT_WATCH_MINUTES is 0)',
        };
    }
    if (since === null) return { tone: 'warn', text: 'Has not run yet', detail: 'Runs every ' + every + ' min' };
    if (since > 2 * every) return { tone: 'warn', text: 'Ran ' + ago(since), detail: 'Overdue — it runs every ' + every + ' min' };
    const next = s.next_at ? hhmm(s.next_at) : '';
    return { tone: 'ok', text: 'Ran ' + ago(since), detail: next ? 'Next at ' + next : 'Next in about ' + Math.max(0, every - since) + ' min' };
}

/**
 * The strip under Release status: "Auto-triage · every 10 min · last check
 * 09:41 · 2 needs attention · 3 open · next 09:51". {text, tone}: amber when
 * something needs attention, red when the last pass failed.
 */
export function stripLine(feed) {
    const f = feed && typeof feed === 'object' ? feed : {};
    const s = f.status && typeof f.status === 'object' ? f.status : {};
    const every = Number(s.interval_minutes) || 0;
    const n = counts(f.findings);
    const parts = ['Auto-triage', every > 0 ? 'every ' + every + ' min' : 'on demand'];
    if (s.running) parts.push('running now');
    else parts.push(s.last_run_at ? 'last check ' + hhmm(s.last_run_at) : 'not run yet');
    parts.push(n.attention + ' needs attention', n.open + ' open');
    if (s.next_at && !s.running) parts.push('next ' + hhmm(s.next_at));
    let tone = n.attention > 0 ? 'warn' : 'ok';
    if (s.error) { tone = 'error'; parts.push('last pass failed'); }
    if (f.ok === false && f.error) { tone = 'error'; parts.push(String(f.error)); }
    return { text: parts.join(' · '), tone };
}

/** "last run: 2 investigated, 1 waiting, 1 updated, 1 cleared" or "not run yet". */
export function lastRunText(summary) {
    const s = summary && typeof summary === 'object' ? summary : null;
    if (!s) return 'not run yet';
    const parts = [((s.investigated || []).length) + ' investigated'];
    if (Number(s.waiting) > 0) parts.push(s.waiting + ' waiting');
    if ((s.updated || []).length) parts.push(s.updated.length + ' updated');
    if ((s.cleared || []).length) parts.push(s.cleared.length + ' cleared');
    return 'last run: ' + parts.join(', ');
}

/** "source · owner · category · N rows" — whatever of it the finding has. */
export function metaLine(f) {
    const x = f && typeof f === 'object' ? f : {};
    const n = Number(x.count) || 0;
    return [x.source, x.owner, x.category, n ? n + (n === 1 ? ' row' : ' rows') : ''].filter(Boolean).join(' · ');
}

/** What a finding offers a person, by its status. */
export function statusActions(status) {
    if (status === 'open') {
        return [
            { status: 'resolved', label: 'Mark resolved' },
            { status: 'dismissed', label: 'Dismiss' },
        ];
    }
    return [{ status: 'open', label: 'Reopen' }];
}

/** "Resolved by carol@example.com" — who closed it, but never the watcher. */
export function statusText(f) {
    const x = f && typeof f === 'object' ? f : {};
    const who = x.actor && x.actor !== 'watcher' ? ' by ' + x.actor : '';
    switch (x.status) {
    case 'resolved': return 'Resolved' + who;
    case 'dismissed': return 'Dismissed' + who;
    case 'cleared': return 'Cleared — no longer in the control table';
    default: return 'Open';
    }
}

/** The finding as "Was this right?" records it (forms/support_feedback.js). */
export function asInvestigation(f) {
    const x = f && typeof f === 'object' ? f : {};
    return {
        business_date: x.business_date, incident_id: x.incident_id, title: x.title, category: x.category,
        action: x.action, model: x.model,
        model_calls: x.model_calls == null ? null : x.model_calls, seconds: x.seconds == null ? null : x.seconds,
    };
}

/** What "Ask why" sends to the chat: the finding, so the model starts from it. */
export function askPrompt(f) {
    const x = f && typeof f === 'object' ? f : {};
    const date = x.business_date ? ' for ' + x.business_date : '';
    const said = [x.cause ? 'cause: ' + x.cause : '', x.do_now ? 'do now: ' + x.do_now : ''].filter(Boolean).join('; ');
    return 'Auto-triage finding' + date + ': incident "' + String(x.title || x.incident_id || '?') + '"' +
        (said ? ' (' + said + ')' : '') +
        ' — explain why it most likely happened, whether the evidence establishes it, ' +
        'how urgent it is by our priority policy, and what L1 should do now.';
}
