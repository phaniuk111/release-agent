// Wording for the Monitoring pill — shared with the Backstage port, so pure and
// tested in tests/js/monitoring.test.mjs. The server decides whether a check
// fires (tools/monitoring.py); this only says it.

/** "2 firing · 3 OK · 4 not measured here" — every state named, so a
 * project with nothing to measure never reads as "all clear". */
export function monitoringSummary(result) {
    const checks = (result && result.checks) || [];
    if (!checks.length) return 'no checks configured';
    const n = (state) => checks.filter(c => c.state === state).length;
    const parts = [
        n('firing') ? n('firing') + ' firing' : '',
        n('unknown') ? n('unknown') + ' could not run' : '',
        n('ok') ? n('ok') + ' OK' : '',
        n('no_data') ? n('no_data') + ' not measured here' : '',
    ].filter(Boolean);
    if (!n('firing') && !n('unknown') && !n('no_data')) return n('ok') === 1 ? 'the check is OK' : 'all ' + n('ok') + ' checks OK';
    return parts.join(' · ');
}

/** Firing first, then broken, then healthy, then unmeasured — what needs a
 * human is at the top. Stable within a state. */
export function orderChecks(checks) {
    const rank = { firing: 0, unknown: 1, ok: 2, no_data: 3 };
    return (checks || []).map((c, i) => [c, i])
        .sort((a, b) => ((rank[a[0].state] ?? 1) - (rank[b[0].state] ?? 1)) || (a[1] - b[1]))
        .map(([c]) => c);
}

/** "watching 17" for a healthy check that measured something. */
export function watchingText(check) {
    return check && typeof check.watching === 'number' && check.watching > 0 ? 'watching ' + check.watching : '';
}

/** A series' labels, most telling first, without Prometheus' own bookkeeping. */
export function seriesLabel(labels) {
    const skip = new Set(['__name__', 'monitored_resource', 'project_id', 'location']);
    const entries = Object.entries(labels || {}).filter(([k]) => !skip.has(k));
    if (!entries.length) return (labels && labels.__name__) || 'value';
    return entries.map(([k, v]) => k + '=' + v).join(', ');
}

/** 63 → "63", 0.000034722 → "3.47e-5", 12.5 → "12.5". */
export function formatValue(v) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
    const n = Number(v);
    if (Number.isInteger(n)) return String(n);
    if (Math.abs(n) < 0.001) return n.toExponential(2);
    return String(Math.round(n * 100) / 100);
}

/** What the chat is asked when someone clicks "Ask why" on a check. */
export function explainPrompt(check) {
    return 'The monitoring check "' + check.name + '" is ' + check.state +
        ' (PromQL: ' + check.query + '). Show me what it found and help me work out why.';
}
