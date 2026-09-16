// Which pills a person sees (static/core/capabilities.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { visibleCapabilities } from '../../src/release_agent/static/core/capabilities.js';

const GROUPS = [{ name: 'Release' }, { name: 'Check' }];
const CAPS = [{ group: 'Release', label: 'Queue' }, { group: 'Check', label: 'Monitoring' }];

test('a hidden group loses its heading and every pill in it', () => {
    const v = visibleCapabilities(GROUPS, CAPS, { hiddenGroups: ['Check'] });
    assert.deepEqual(v.groups.map(g => g.name), ['Release']);
    assert.deepEqual(v.capabilities.map(c => c.label), ['Queue']);
});

test('a tester sees the preview group, tagged as preview', () => {
    const v = visibleCapabilities(GROUPS, CAPS, { hiddenGroups: [], previewGroups: ['Check'] });
    assert.deepEqual(v.groups, [{ name: 'Release', preview: false }, { name: 'Check', preview: true }]);
    assert.equal(v.capabilities.length, 2);
});

test('no config shows everything untagged', () => {
    assert.equal(visibleCapabilities(GROUPS, CAPS, undefined).capabilities.length, 2);
});
