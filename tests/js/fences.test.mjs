// Fenced code blocks in chat text (static/core/fences.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { liftFences } from '../../src/release_agent/static/core/fences.js';

test('a preview block is lifted whole and the inline token after it stays intact', () => {
    // The shape of every deploy/release preview (deploy_workflow._preview_text).
    const text = '**Create release R → SIT**\n\n```json\n{\n  "a": 1\n}\n```\n\nReply `CONFIRM-ABC123` to confirm.';
    const { text: rest, blocks } = liftFences(text);
    assert.deepEqual(blocks, [{ lang: 'json', code: '{\n  "a": 1\n}' }]);
    assert.ok(rest.includes('CODESLOT0ENDCODE'));
    assert.ok(rest.endsWith('Reply `CONFIRM-ABC123` to confirm.'), 'its backticks pair only with each other');
    assert.equal((rest.match(/`/g) || []).length, 2);
});

test('several blocks, no language, and text between them', () => {
    const { text, blocks } = liftFences('a\n```\nx\n```\nb\n```sh\ny\n```\nc');
    assert.deepEqual(blocks, [{ lang: '', code: 'x' }, { lang: 'sh', code: 'y' }]);
    assert.ok(text.startsWith('a\n') && text.includes('\nb\n') && text.endsWith('\nc'));
});

test('a block still streaming in stays text until it closes', () => {
    const { text, blocks } = liftFences('Here:\n```json\n{"a": ');
    assert.equal(blocks.length, 0);
    assert.equal(text, 'Here:\n```json\n{"a": ');
});

test('no fences, no change', () => {
    assert.deepEqual(liftFences('plain `code` only'), { text: 'plain `code` only', blocks: [] });
    assert.deepEqual(liftFences(null), { text: '', blocks: [] });
});
