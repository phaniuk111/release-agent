// The Monitoring pill's wording (static/core/monitoring.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { explainPrompt, formatValue, monitoringSummary, seriesLabel }
    from '../../src/release_agent/static/core/monitoring.js';

test('the summary counts firing and broken checks, and never calls a broken one OK', () => {
    assert.equal(monitoringSummary({ checks: [] }), 'no checks configured');
    assert.equal(monitoringSummary({ checks: [{ state: 'ok' }] }), 'the check is OK');
    assert.equal(monitoringSummary({ checks: [{ state: 'ok' }, { state: 'ok' }] }), 'all 2 checks OK');
    assert.equal(monitoringSummary({ checks: [{ state: 'firing' }, { state: 'unknown' }, { state: 'ok' }] }),
        '1 firing · 1 could not run');
    assert.equal(monitoringSummary(null), 'no checks configured');
});

test('series labels drop Prometheus bookkeeping', () => {
    assert.equal(seriesLabel({ __name__: 'up', job: 'api', instance: 'a:9090' }), 'job=api, instance=a:9090');
    assert.equal(seriesLabel({ __name__: 'up' }), 'up');
    assert.equal(seriesLabel({ service: 'aiplatform.googleapis.com', monitored_resource: 'consumed_api' }),
        'service=aiplatform.googleapis.com');
    assert.equal(seriesLabel({}), 'value');
});

test('values read cleanly', () => {
    assert.equal(formatValue(63), '63');
    assert.equal(formatValue(12.3456), '12.35');
    assert.equal(formatValue(0.000034722), '3.47e-5');
    assert.equal(formatValue(null), '—');
    assert.equal(formatValue('NaN'), '—');
});

test('asking the chat about a check names it and its query', () => {
    const p = explainPrompt({ name: 'Targets down', state: 'firing', query: 'up == 0' });
    assert.ok(p.includes('"Targets down"') && p.includes('up == 0') && p.includes('firing'));
});
