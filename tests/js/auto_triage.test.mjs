// Auto-triage's feed rules (static/core/auto_triage.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
    ago, asInvestigation, askPrompt, counts, groupFindings, hhmm, lastRunText, metaLine, statusActions,
    statusText, stripLine, watcherHealth,
} from '../../src/release_agent/static/core/auto_triage.js';

const NOW = Date.parse('2026-10-07T12:00:00Z');
const minutesAgo = (m) => new Date(NOW - m * 60000).toISOString();

test('groupFindings puts open high and critical first, then other open, then the closed', () => {
    const groups = groupFindings([
        { business_date: 'd', incident_id: 'low', status: 'open', priority: 'low' },
        { business_date: 'd', incident_id: 'done', status: 'resolved', priority: 'high' },
        { business_date: 'd', incident_id: 'hot', status: 'open', priority: 'high' },
        { business_date: 'd', incident_id: 'gone', status: 'cleared', priority: 'critical' },
    ]);
    assert.deepEqual(groups.map(g => [g.key, g.label, g.items.map(i => i.incident_id)]), [
        ['attention', 'Needs attention', ['hot']],
        ['open', 'Open', ['low']],
        ['closed', 'Resolved, dismissed and cleared', ['done', 'gone']],
    ]);
});

test('groupFindings leaves out empty groups and survives junk', () => {
    assert.deepEqual(groupFindings([]), []);
    assert.deepEqual(groupFindings(undefined), []);
    assert.deepEqual(groupFindings([null, 'x']), []);
    assert.deepEqual(counts([{ status: 'open', priority: 'critical' }, { status: 'open' }, { status: 'dismissed', priority: 'high' }]),
        { attention: 1, open: 2 });
});

test('ago reads like a person says it', () => {
    assert.equal(ago(0), 'just now');
    assert.equal(ago(4), '4 min ago');
    assert.equal(ago(120), '2 h ago');
    assert.equal(ago(60 * 72), '3 days ago');
    assert.equal(hhmm('2026-10-07T09:41:12Z'), '09:41');
    assert.equal(hhmm(''), '');
    assert.equal(hhmm('nonsense'), '');
});

test('watcherHealth is fine within its interval and names the next pass', () => {
    assert.deepEqual(watcherHealth({ enabled: true, interval_minutes: 15, last_run_at: minutesAgo(4) }, NOW), {
        tone: 'ok', text: 'Ran 4 min ago', detail: 'Next in about 11 min',
    });
    assert.equal(watcherHealth({ enabled: true, interval_minutes: 15, last_run_at: minutesAgo(4),
        next_at: '2026-10-07T12:11:00Z' }, NOW).detail, 'Next at 12:11');
});

test('watcherHealth warns when it has missed two runs — a stopped watcher must not look quiet', () => {
    assert.equal(watcherHealth({ enabled: true, interval_minutes: 15, last_run_at: minutesAgo(45) }, NOW).tone, 'warn');
    assert.equal(watcherHealth({ enabled: true, interval_minutes: 15 }, NOW).text, 'Has not run yet');
});

test('watcherHealth shows the error of a failed run, running, on demand, and unconfigured', () => {
    assert.deepEqual(watcherHealth({ enabled: true, interval_minutes: 15, error: 'no table' }, NOW),
        { tone: 'error', text: 'Last run failed', detail: 'no table' });
    assert.equal(watcherHealth({ running: true }, NOW).text, 'Running now');
    assert.equal(watcherHealth({ enabled: false }, NOW).tone, 'off');
    assert.equal(watcherHealth({ enabled: false, last_run_at: minutesAgo(3) }, NOW).text, 'Ran 3 min ago');
    assert.equal(watcherHealth({ enabled: false, configured: false }, NOW).text, 'Not configured');
    assert.equal(watcherHealth(undefined, NOW).tone, 'off');
});

test('the strip says the cadence, the last check, the counts and the next pass', () => {
    const feed = {
        ok: true,
        findings: [{ status: 'open', priority: 'high' }, { status: 'open', priority: 'high' }, { status: 'open', priority: 'low' }],
        status: { interval_minutes: 10, last_run_at: '2026-10-07T09:41:00Z', next_at: '2026-10-07T09:51:00Z' },
    };
    assert.deepEqual(stripLine(feed), {
        text: 'Auto-triage · every 10 min · last check 09:41 · 2 needs attention · 3 open · next 09:51', tone: 'warn',
    });
    assert.deepEqual(stripLine({ ok: true, findings: [], status: { interval_minutes: 0 } }),
        { text: 'Auto-triage · on demand · not run yet · 0 needs attention · 0 open', tone: 'ok' });
    assert.equal(stripLine({ status: { interval_minutes: 10, running: true } }).text,
        'Auto-triage · every 10 min · running now · 0 needs attention · 0 open');
});

test('the strip is red when the last pass failed or the findings could not be read', () => {
    const failed = stripLine({ ok: true, findings: [], status: { interval_minutes: 10, last_run_at: '2026-10-07T09:41:00Z', error: 'no table' } });
    assert.equal(failed.tone, 'error');
    assert.match(failed.text, /last pass failed$/);
    assert.equal(stripLine({ ok: false, error: 'down', status: {} }).tone, 'error');
    assert.equal(stripLine(null).tone, 'ok');
});

test('the last run reads as a sentence', () => {
    assert.equal(lastRunText(null), 'not run yet');
    assert.equal(lastRunText({ investigated: ['a', 'b'], waiting: 1, updated: ['c'], cleared: [] }),
        'last run: 2 investigated, 1 waiting, 1 updated');
    assert.equal(lastRunText({ investigated: [] }), 'last run: 0 investigated');
});

test('the meta line is source · owner · category · rows, whatever of it there is', () => {
    assert.equal(metaLine({ source: 'svc-a', owner: 'data-platform', category: 'process', count: 3 }),
        'svc-a · data-platform · process · 3 rows');
    assert.equal(metaLine({ category: 'stuck', count: 1 }), 'stuck · 1 row');
    assert.equal(metaLine({}), '');
});

test('statuses: resolve and dismiss on an open finding, reopen otherwise, and who closed it', () => {
    assert.deepEqual(statusActions('open').map(a => a.status), ['resolved', 'dismissed']);
    assert.deepEqual(statusActions('cleared').map(a => a.status), ['open']);
    assert.equal(statusText({ status: 'resolved', actor: 'carol@example.com' }), 'Resolved by carol@example.com');
    assert.equal(statusText({ status: 'dismissed', actor: 'watcher' }), 'Dismissed');
    assert.match(statusText({ status: 'cleared', actor: 'watcher' }), /^Cleared/);
    assert.equal(statusText({ status: 'open' }), 'Open');
});

test('feedback and Ask why are recorded against the same incident and model', () => {
    assert.deepEqual(asInvestigation({ business_date: 'd', incident_id: 'a', status: 'open', model: 'm', model_calls: 1, seconds: 3 }),
        { business_date: 'd', incident_id: 'a', title: undefined, category: undefined, action: undefined, model: 'm',
            model_calls: 1, seconds: 3 });
    assert.equal(asInvestigation({}).model_calls, null);
    const prompt = askPrompt({ business_date: '2026-10-02', title: '3 failed · process', cause: 'schema change', do_now: 'escalate' });
    assert.match(prompt, /^Auto-triage finding for 2026-10-02: incident "3 failed · process" \(cause: schema change; do now: escalate\)/);
    assert.match(askPrompt({}), /incident "\?"/);
});

import { rowsOf } from '../../src/release_agent/static/core/auto_triage.js';

test('rowsOf: the rows behind a finding, jobs linked only through a {job_id} template', () => {
    const f = { facts: ['3 runs of REPORT-B failed', ''], error_text: 'quota exceeded',
        runs: [{ run_id: 'run-003', at: '2026-10-11T18:33:00Z' }, { at: 'x' }], job_ids: ['j 1'] };
    const r = rowsOf(f, 'https://console.example.com/jobs/{job_id}');
    assert.deepEqual(r.facts, ['3 runs of REPORT-B failed']);
    assert.equal(r.error, 'quota exceeded');
    assert.deepEqual(r.runs.map(x => x.id), ['run-003'], 'a run without an id is left out');
    assert.equal(r.jobs[0].href, 'https://console.example.com/jobs/j%201');
    assert.equal(rowsOf(f, 'https://console.example.com/jobs').jobs[0].href, '', 'no template, no link');
    assert.equal(r.empty, false);
    assert.equal(rowsOf({}).empty, true);
    assert.equal(rowsOf(null).empty, true);
});
