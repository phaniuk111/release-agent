// Which pills a person sees (static/core/capabilities.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { hasFeature, llmEnabled, visibleCapabilities } from '../../src/release_agent/static/core/capabilities.js';

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

test('a pill that names a feature shows only when the server listed that feature', () => {
    const caps = CAPS.concat([{ group: 'Check', label: 'Auto-triage', feature: 'auto-triage' }]);
    assert.deepEqual(visibleCapabilities(GROUPS, caps, { features: ['auto-triage'] }).capabilities.map(c => c.label),
        ['Queue', 'Monitoring', 'Auto-triage']);
    assert.deepEqual(visibleCapabilities(GROUPS, caps, { features: [] }).capabilities.map(c => c.label), ['Queue', 'Monitoring']);
    assert.deepEqual(visibleCapabilities(GROUPS, caps, undefined).capabilities.map(c => c.label), ['Queue', 'Monitoring']);
    assert.equal(hasFeature({ features: ['notifications'] }, 'notifications'), true);
    assert.equal(hasFeature({ features: ['notifications'] }, 'auto-triage'), false);
    assert.equal(hasFeature({}, 'auto-triage'), false);
    assert.equal(hasFeature(undefined, 'auto-triage'), false);
});

test('only an explicit llm: false switches the model-only controls off', () => {
    assert.equal(llmEnabled({ llm: false }), false);
    assert.equal(llmEnabled({ llm: true }), true);
    assert.equal(llmEnabled({}), true);          // a page from before the flag
    assert.equal(llmEnabled(undefined), true);
});

import { isPreviewFeature } from '../../src/release_agent/static/core/capabilities.js';

test('isPreviewFeature: only a feature the server lists as still in preview', () => {
    assert.equal(isPreviewFeature({ previewFeatures: ['auto-triage'] }, 'auto-triage'), true);
    assert.equal(isPreviewFeature({ previewFeatures: ['auto-triage'] }, 'notifications'), false);
    assert.equal(isPreviewFeature({ features: ['auto-triage'] }, 'auto-triage'), false, 'an older page: no tag');
    assert.equal(isPreviewFeature(undefined, 'auto-triage'), false);
});
