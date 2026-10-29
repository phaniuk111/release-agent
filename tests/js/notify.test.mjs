// The notification bell's rules (static/core/notify.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
    age, badgeText, bodyLine, checkedText, dotStyle, emptyText, groupItems, kindLabel, routeLink, shouldPoll,
} from '../../src/release_agent/static/core/notify.js';

const item = (over) => Object.assign({ subject: 's', status: 'unread', severity: 'info', at: '2026-10-10T10:00:00Z' }, over);

test('only a visible tab polls', () => {
    assert.equal(shouldPoll('visible'), true);
    assert.equal(shouldPoll('hidden'), false);
    assert.equal(shouldPoll(undefined), false);
});

test('items are grouped unread first, newest first, dismissed gone', () => {
    const g = groupItems([
        item({ subject: 'old-read', status: 'read', at: '2026-10-09T10:00:00Z' }),
        item({ subject: 'new-unread', at: '2026-10-10T12:00:00Z' }),
        item({ subject: 'gone', status: 'dismissed', at: '2026-10-10T13:00:00Z' }),
        item({ subject: 'old-unread', at: '2026-10-10T09:00:00Z' }),
        item({ subject: 'new-read', status: 'read', at: '2026-10-10T11:00:00Z' }),
    ]);
    assert.deepEqual(g.unread.map(i => i.subject), ['new-unread', 'old-unread']);
    assert.deepEqual(g.read.map(i => i.subject), ['new-read', 'old-read']);
    assert.deepEqual(groupItems(null), { unread: [], read: [] });
});

test('the dot is filled when unread, hollow when read, coloured by severity', () => {
    assert.equal(dotStyle(item()).filled, true);
    assert.equal(dotStyle(item({ status: 'read' })).filled, false);
    assert.equal(dotStyle(item({ severity: 'warning' })).colour, '#fbbf24');
    assert.equal(dotStyle(item({ severity: 'critical' })).colour, '#f87171');
    assert.equal(dotStyle(item({ severity: 'nonsense' })).colour, dotStyle(item()).colour, 'unknown reads as info');
    assert.match(dotStyle(item({ status: 'read' })).style, /background:transparent/);
    assert.match(dotStyle(item()).style, /background:#34d399/);
});

test('the badge is empty at zero and capped at 9+', () => {
    assert.equal(badgeText(0), '');
    assert.equal(badgeText(undefined), '');
    assert.equal(badgeText(3), '3');
    assert.equal(badgeText(9), '9');
    assert.equal(badgeText(42), '9+');
});

test('age is the shortest wording that still reads', () => {
    const now = Date.parse('2026-10-10T12:00:00Z');
    assert.equal(age('2026-10-10T11:59:40Z', now), 'now');
    assert.equal(age('2026-10-10T11:58:00Z', now), '2m');
    assert.equal(age('2026-10-10T10:30:00Z', now), '1h');
    assert.equal(age('2026-10-09T06:00:00Z', now), 'yesterday');
    assert.equal(age('2026-10-07T12:00:00Z', now), '3d');
    assert.equal(age('', now), '');
    assert.equal(age('junk', now), '');
});

test('the footer says when the list was last checked', () => {
    const now = Date.parse('2026-10-10T12:00:00Z');
    assert.equal(checkedText(0, now), 'Not checked yet');
    assert.equal(checkedText(now - 2000, now), 'Checked just now');
    assert.equal(checkedText(now - 15000, now), 'Checked 15s ago');
    assert.equal(checkedText(now - 3 * 60000, now), 'Checked 3m ago');
    assert.equal(checkedText(now - 2 * 3600000, now), 'Checked 2h ago');
});

test('the body line is the first line, cut with an ellipsis', () => {
    assert.equal(bodyLine('first line\nsecond'), 'first line');
    assert.equal(bodyLine(null), '');
    const long = 'x'.repeat(200);
    assert.equal(bodyLine(long).length, 110);
    assert.ok(bodyLine(long).endsWith('…'));
});

test('portal links route to the page action, chat links to the chat, URLs to a tab', () => {
    assert.deepEqual(routeLink('portal:queue'), { kind: 'queue' });
    assert.deepEqual(routeLink('portal:rerun:abc123'), { kind: 'rerun', batchId: 'abc123' });
    assert.deepEqual(routeLink('portal:rerun:'), { kind: 'none' });
    assert.deepEqual(routeLink('portal:support:abc123:2026-10-09'),
        { kind: 'auto-triage', incidentId: 'abc123', date: '2026-10-09' });
    assert.deepEqual(routeLink('portal:support:stuck'), { kind: 'auto-triage', incidentId: 'stuck', date: '' });
    assert.deepEqual(routeLink('portal:deploy:example-org/deploy#42'),
        { kind: 'chat', message: 'What is the state of the deploy example-org/deploy#42?' });
    assert.deepEqual(routeLink('chat:why did df-position-agg:2.0.0 fail?'),
        { kind: 'chat', message: 'why did df-position-agg:2.0.0 fail?' });
    assert.deepEqual(routeLink('https://github.com/example-org/deploy/pull/42'),
        { kind: 'url', url: 'https://github.com/example-org/deploy/pull/42' });
});

test('a link that is not http(s), portal: or chat: does nothing — never runs', () => {
    assert.deepEqual(routeLink('javascript:alert(1)'), { kind: 'none' });
    assert.deepEqual(routeLink('ftp://x'), { kind: 'none' });
    assert.deepEqual(routeLink(''), { kind: 'none' });
    assert.deepEqual(routeLink(null), { kind: 'none' });
    assert.deepEqual(routeLink('chat:   '), { kind: 'none' });
    assert.deepEqual(routeLink('portal:deploy:'), { kind: 'none' });
    assert.deepEqual(routeLink('portal:unknown'), { kind: 'none' });
});

test('the empty state says why', () => {
    assert.equal(emptyText({ ok: true }), 'Nothing new in the last 14 days.');
    assert.equal(emptyText({ ok: false, error: 'BigQuery unavailable' }), 'BigQuery unavailable');
    assert.equal(emptyText({ ok: true, needsEmail: true }), 'Enter your email to see your notifications.');
    assert.equal(emptyText(null), 'Notifications could not be read.');
});

test('kinds have a short label and an unknown one shows as itself', () => {
    assert.equal(kindLabel('release_pr'), 'Release PR');
    assert.equal(kindLabel('auto_triage'), 'Auto-triage');
    assert.equal(kindLabel('something_new'), 'something_new');
    assert.equal(kindLabel(''), '');
});
