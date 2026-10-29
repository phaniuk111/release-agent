// The release-notification card's wording (static/core/deploy_tracking.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { checkLine, openPrLine, overall, prHeading, weekLabel }
    from '../../src/release_agent/static/core/deploy_tracking.js';

test('a check reads ok, warning or problem', () => {
    assert.equal(checkLine({ name: 'a', ok: true, detail: 'x' }).state, 'ok');
    assert.equal(checkLine({ name: 'a', ok: false, detail: 'x' }).state, 'problem');
    assert.equal(checkLine({ name: 'a', ok: null, detail: 'x' }).state, 'warn');
});

test('an open PR says what happens when it merges', () => {
    assert.equal(openPrLine({ tracking: false }).state, 'problem');
    assert.match(openPrLine({ tracking: true, watched: true }).text, /^Watched — Start is sent when it merges/);
    const pending = openPrLine({ tracking: true, watched: false }, 5);
    assert.equal(pending.state, 'warn');
    assert.match(pending.text, /next check \(every 5 min\)/);
});

test('the headline is the first failing check, else all fine with the open count', () => {
    assert.equal(overall({ ok: true, checks: [{ name: 'Tracking switched on', ok: false, detail: 'off' }] }).text,
        'Tracking switched on: off');
    assert.equal(overall({ ok: true, checks: [{ ok: true }], prs: [] }).text,
        'Everything a notification needs is in place');
    assert.equal(overall({ ok: true, checks: [{ ok: null }], prs: [{}, {}] }).text,
        'Everything a notification needs is in place · 2 release PRs open this week');
    assert.equal(overall(null).state, 'problem');
});

test('a PR is named by environment and tickets', () => {
    assert.equal(prHeading({ env: 'PRL1', chg: 'CHG0099001', rmg: 'RMG1', pr_number: 7 }),
        'PRL1 · CHG0099001 · RMG1 — PR #7');
    assert.equal(prHeading({ pr_number: 7 }), 'PR #7');
});

test('the week is named by its Monday', () => {
    assert.equal(weekLabel('2026-10-26T00:00:00+00:00'), 'Mon 26 Oct');
    assert.equal(weekLabel('nonsense'), 'this week');
});
