// The "Was this right?" row's wording and request (static/core/support_feedback.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
    CAUSE_PROMPT, MAX_CAUSE, VERDICTS, asksForCause, feedbackPayload, statsLine, thanksText,
} from '../../src/release_agent/static/core/support_feedback.js';

const EVENT = { business_date: '2026-10-02', incident_id: 'e-0000aaaa', title: 'Feed A late',
    model: 'm-1', model_calls: 3, seconds: 12.5 };

test('the three answers come in the order the buttons show them', () => {
    assert.deepEqual(VERDICTS, [
        { key: 'right', label: 'Right' },
        { key: 'direction', label: 'Right direction' },
        { key: 'wrong', label: 'Wrong' },
    ]);
});

test('only a not-quite-right answer asks for the actual cause', () => {
    assert.equal(asksForCause('right'), false);
    assert.equal(asksForCause('direction'), true);
    assert.equal(asksForCause('wrong'), true);
    assert.equal(CAUSE_PROMPT, 'What was the actual cause?');
    assert.equal(MAX_CAUSE, 500);
});

test('the payload carries the investigation and the answer, and the cause as typed', () => {
    assert.deepEqual(feedbackPayload(EVENT, 'wrong', '  The vendor re-sent a file  '), {
        business_date: '2026-10-02', incident_id: 'e-0000aaaa', title: 'Feed A late',
        verdict: 'wrong', actual_cause: 'The vendor re-sent a file', category: '', action: '',
        model: 'm-1', model_calls: 3, seconds: 12.5,
    });
});

test('a right answer never sends a cause, even from a half-typed box', () => {
    assert.equal(feedbackPayload(EVENT, 'right', 'left over text').actual_cause, '');
});

test('the payload never names who answered — the server knows', () => {
    const p = feedbackPayload({ ...EVENT, actor: 'x@example.com', requested_by: 'y@example.com' }, 'right', '');
    assert.equal('actor' in p, false);
    assert.equal('requested_by' in p, false);
});

test('missing fields never throw, and absent timings are left out', () => {
    const p = feedbackPayload(null, 'direction', undefined);
    assert.equal(p.business_date, '');
    assert.equal(p.incident_id, '');
    assert.equal(p.actual_cause, '');
    assert.equal('model_calls' in p, false);
    assert.equal('seconds' in p, false);
    assert.equal(feedbackPayload({ model_calls: 0, seconds: 0 }, 'right').model_calls, 0);
});

test('each answer is thanked in its own words', () => {
    assert.equal(thanksText('right'), 'Thanks — marked right.');
    assert.match(thanksText('direction'), /right direction/);
    assert.match(thanksText('wrong'), /runbook/);
    assert.equal(thanksText('???'), 'Thanks for the feedback.');
    assert.equal(thanksText(undefined), 'Thanks for the feedback.');
});

test('the stats line says how many were rated and how often the agent was right', () => {
    assert.equal(statsLine({ ok: true, days: 30, total: 42, right: 23, direction: 13,
        accuracy: 23 / 42, right_or_direction: 36 / 42 }),
    '30 days: 42 investigations rated · 55% right · 86% right or right direction');
    assert.equal(statsLine({ ok: true, days: 7, total: 1, accuracy: 1, right_or_direction: 1 }),
        '7 days: 1 investigation rated · 100% right · 100% right or right direction');
});

test('the stats line works from the counts when the ratios are missing', () => {
    assert.equal(statsLine({ ok: true, days: 30, total: 4, right: 2, direction: 1, wrong: 1 }),
        '30 days: 4 investigations rated · 50% right · 75% right or right direction');
});

test('the stats line copes with nothing rated, a disabled memory, and no payload', () => {
    assert.equal(statsLine({ ok: true, days: 30, total: 0, accuracy: null }), '30 days: no investigations rated yet');
    assert.equal(statsLine({ ok: true }), '30 days: no investigations rated yet');
    assert.equal(statsLine({ ok: false, disabled: true }), 'Feedback is not being kept');
    assert.equal(statsLine({ ok: false, error: 'x' }), 'Feedback is unavailable');
    assert.equal(statsLine(null), '30 days: no investigations rated yet');
    assert.equal(statsLine({ ok: true, days: 1, total: 0 }), '1 day: no investigations rated yet');
});
