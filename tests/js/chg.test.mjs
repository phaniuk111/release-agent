// The change-request field rules both UIs share (static/core/chg.js). Run by
// tests/test_ui_js.py through Node's built-in runner — nothing to install.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
    CHIPS, PROSE_FIELDS, canReplace, chipState, itemsKey, monoReleaseNote,
} from '../../src/release_agent/static/core/chg.js';

const auto = (value, source) => ({ value, source });

test('the prose fields are the release file\'s keys, in its order', () => {
    assert.deepEqual(PROSE_FIELDS, ['change_description', 'change_reason', 'associated_risk',
        'consequence', 'user_service_impact']);
});

test('a field still holding the last automatic value shows where that value came from', () => {
    assert.equal(chipState('Low risk.', auto('Low risk.', 'team')), 'team');
    assert.equal(chipState('svc-a fixes swagger.', auto('svc-a fixes swagger.', 'ai')), 'ai');
    assert.equal(chipState('- svc-a:1.0', auto('- svc-a:1.0', 'fallback')), 'fallback');
    assert.equal(chipState('  Low risk. ', auto('Low risk.', 'team')), 'team', 'surrounding whitespace is not an edit');
});

test('text no automatic writer put there is the person\'s', () => {
    assert.equal(chipState('Low risk. Reviewed by the CAB.', auto('Low risk.', 'team')), 'edited');
    assert.equal(chipState('typed before anything was filled', null), 'edited');
    assert.equal(chipState('typed', undefined), 'edited');
});

test('an empty field has no chip, whatever was there before', () => {
    assert.equal(chipState('', null), '');
    assert.equal(chipState('   ', auto('Low risk.', 'team')), '');
    assert.equal(chipState('', auto('', 'team')), '');
});

test('an unknown source reads as an AI draft — the label that asks for review', () => {
    assert.equal(chipState('x', auto('x', 'model-v2')), 'ai');
    assert.equal(chipState('x', auto('x', undefined)), 'ai');
});

test('every chip state has a label and a tooltip', () => {
    for (const state of ['ai', 'team', 'edited', 'fallback']) {
        assert.ok(CHIPS[state].label && CHIPS[state].title, state);
    }
    assert.equal(CHIPS.ai.label, 'AI draft');
    assert.equal(CHIPS.team.label, 'Team wording');
    assert.equal(CHIPS.edited.label, 'Edited');
    assert.equal(CHIPS.fallback.label, 'Fallback');
});

test('an automatic value replaces only an empty field or its own last value', () => {
    assert.equal(canReplace('', null), true);
    assert.equal(canReplace('  ', auto('Low risk.', 'ai')), true, 'a cleared field is refilled');
    assert.equal(canReplace('Low risk.', auto('Low risk.', 'team')), true);
    assert.equal(canReplace('Low risk. ', auto('Low risk.', 'ai')), true);
    assert.equal(canReplace('My own words', auto('Low risk.', 'team')), false, 'an edit is never replaced');
    assert.equal(canReplace('typed first', null), false);
});

test('the chip and the replace rule agree: only an edited field is kept', () => {
    const cases = [['', null], ['a', null], ['a', auto('a', 'ai')], ['b', auto('a', 'ai')], ['', auto('a', 'team')]];
    for (const [value, last] of cases) {
        assert.equal(canReplace(value, last), chipState(value, last) !== 'edited', JSON.stringify([value, last]));
    }
});

test('a registry URL and its bare name:version are the same item', () => {
    assert.equal(itemsKey(['https://artifactory.example.com/docker/example-ds/svc-a:5.0.463']),
        itemsKey(['svc-a:5.0.463']));
    assert.equal(itemsKey(['https://artifactory.example.com/docker/svc-a:5.0.463/']), 'svc-a:5.0.463');
});

test('order, blank lines and repeats do not change the release', () => {
    assert.equal(itemsKey(['svc-b:2', '', 'svc-a:1', 'svc-a:1']), itemsKey(['svc-a:1', 'svc-b:2']));
    assert.equal(itemsKey([]), '');
    assert.equal(itemsKey(undefined), '');
});

test('a different version or another chart is a different release', () => {
    assert.notEqual(itemsKey(['svc-a:1']), itemsKey(['svc-a:2']));
    assert.notEqual(itemsKey(['svc-a:1']), itemsKey(['svc-a:1', 'svc-b:1']));
});

test('the mono header names the one file and its repo, and says a person merges', () => {
    assert.equal(monoReleaseNote('.github/release/release_details.json', 'example-org/mono-repo'),
        'One file — .github/release/release_details.json in example-org/mono-repo. The portal raises a ' +
        'PR for you to review and merge; you\'ll see the exact change before anything is pushed.');
    assert.match(monoReleaseNote('', ''), /CARE_RELEASE_REPO is not set/);
});
