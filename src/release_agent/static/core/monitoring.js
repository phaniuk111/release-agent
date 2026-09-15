// Wording for the Monitoring pill — shared with the Backstage port, so pure and
// tested in tests/js/monitoring.test.mjs. The server decides whether a check
// fires (tools/monitoring.py); this only says it.

/** "3 firing · 1 unknown" / "all 4 checks OK" / "no checks configured". */
export function monitoringSummary(result) {
    const checks = (result && result.checks) || [];
    if (!checks.length) return 'no checks configured';
    const firing = checks.filter(c => c.state === 'firing').length;
    const unknown = checks.filter(c => c.state === 'unknown').length;
    if (!firing && !unknown) return checks.length === 1 ? 'the check is OK' : 'all ' + checks.length + ' checks OK';
    return [firing ? firing + ' firing' : '', unknown ? unknown + ' could not run' : '']
        .filter(Boolean).join(' · ');
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
