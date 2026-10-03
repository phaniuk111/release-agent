// The Support triage pill's wording (static/core/support_triage.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
    ENABLE_HINT, actionLabel, askPrompt, emptyText, jobHref, metaLine, summaryLine,
} from '../../src/release_agent/static/core/support_triage.js';

test('the summary leads with what L1 must look at, or says all clear', () => {
    assert.equal(summaryLine({ ok: true, date_label: 'COB', business_date: '2026-10-02',
        counts: { items: 610, failed: 42, stuck: 3, missing: 5 } }),
    'COB 2026-10-02 · 42 failed · 3 stuck · 5 missing of 610 runs');
    assert.equal(summaryLine({ ok: true, business_date: '2026-10-02', counts: { items: 1 } }),
        'Business date 2026-10-02 · all clear · 1 run');
    assert.equal(summaryLine({ ok: false, disabled: true }), 'Support triage is disabled');
    assert.equal(summaryLine({ ok: false, error: 'x' }), 'Support triage is unavailable');
    assert.equal(summaryLine({ ok: true }), 'no runs in the dates read');
    assert.equal(summaryLine(null), 'no runs in the dates read');
});

test('each action has a name, and an unknown one reads as check', () => {
    assert.equal(actionLabel('wait'), 'Wait for data');
    assert.equal(actionLabel('retrigger'), 'Re-trigger once');
    assert.equal(actionLabel('escalate'), 'Escalate');
    assert.equal(actionLabel('reboot'), 'Check');
});

test('the small print says retries that recovered and whether a runbook is in use', () => {
    assert.equal(metaLine({ ok: true, counts: { recovered: 2 }, previous_date: '2026-10-01', runbook_entries: 4 }),
        '2 recovered after a retry · compared with 2026-10-01 · 4 runbook entries');
    assert.match(metaLine({ ok: true }), /no runbook configured/);
    assert.equal(metaLine({ ok: false }), '');
});

test('a job id becomes a link only from an http(s) template', () => {
    assert.equal(jobHref('https://console.example/jobs/{job_id}?p=x', 'job 1'), 'https://console.example/jobs/job%201?p=x');
    assert.equal(jobHref('', 'j'), '');
    assert.equal(jobHref('https://console.example/jobs', 'j'), '');
    assert.equal(jobHref('javascript:alert(1)//{job_id}', 'j'), '');
});

test('"Ask why" names the incident and the date', () => {
    assert.equal(askPrompt({ title: '5 failed · upstream' }, { business_date: '2026-10-02' }),
        'Support triage for 2026-10-02: explain incident "5 failed · upstream" — why it most likely happened, ' +
        'what L1 should do now, and the ticket note.');
    assert.match(askPrompt(null, null), /incident "\?"/);
});

test('an empty date and the enable hint say what to do', () => {
    assert.equal(emptyText({ empty: true, business_date: '2026-10-02' }), 'No runs for 2026-10-02 in the control table.');
    assert.equal(emptyText({}), 'Nothing for L1 on this date.');
    assert.ok(ENABLE_HINT.includes('SUPPORT_TABLE') && ENABLE_HINT.includes('private'));
});

import { investigatePrompt } from '../../src/release_agent/static/core/support_triage.js';

test('"Investigate" hands the skill the incident, its date, shared values and jobs', () => {
    const inc = { id: 'e-1a2b3c4d', title: '4 failed · process · Report REPORT-B', shared: { system: 'SYS-B', source: 'sys-b-fetcher', process: 'REPORT-B' },
        job_ids: ['j1', 'j2'] };
    const rep = { business_date: '2026-10-02', date_label: 'COB', labels: { system: 'Feed', source: 'Source', process: 'Report' } };
    assert.equal(investigatePrompt(inc, rep),
        'Investigate incident e-1a2b3c4d · COB 2026-10-02 · 4 failed · process · Report REPORT-B · Feed SYS-B · Source sys-b-fetcher · Report REPORT-B · ' +
        'jobs j1, j2. Why did it fail, and what should L1 do?');
    assert.equal(investigatePrompt(null, null), 'Investigate incident ? · ?. Why did it fail, and what should L1 do?');
});
