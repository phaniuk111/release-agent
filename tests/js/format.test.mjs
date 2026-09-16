// Formatting shared by every screen (static/core/format.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { escapeHtml, shortName, timeAgo } from '../../src/release_agent/static/core/format.js';

test('every character that can break out of HTML or an attribute is escaped', () => {
    assert.equal(escapeHtml(`<a href="x" onclick='y'>&</a>`),
        '&lt;a href=&quot;x&quot; onclick=&#39;y&#39;&gt;&amp;&lt;/a&gt;');
    assert.equal(escapeHtml(null), '');
    assert.equal(escapeHtml(42), '42');
});

test('time ago never says 0m and survives junk', () => {
    const now = Date.parse('2026-09-12T12:00:00Z');
    assert.equal(timeAgo('2026-09-12T11:59:50Z', now), '1m ago');
    assert.equal(timeAgo('2026-09-12T11:15:00Z', now), '45m ago');
    assert.equal(timeAgo('2026-09-12T09:00:00Z', now), '3h ago');
    assert.equal(timeAgo('2026-09-10T12:00:00Z', now), '2d ago');
    assert.equal(timeAgo('', now), '');
    assert.equal(timeAgo('not a date', now), '');
});

test('a requester is shown by the part before the @', () => {
    assert.equal(shortName('dev@example.com'), 'dev');
    assert.equal(shortName(''), '');
    assert.equal(shortName(undefined), '');
});
