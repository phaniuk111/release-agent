// How a PR's CHG / RMG tickets read on the banner (static/core/tickets.js). Run
// by tests/test_ui_js.py through Node's built-in runner — nothing to install.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { TICKET_PREFIXES, ticketParts, ticketText } from '../../src/release_agent/static/core/tickets.js';

const chg = { id: 'CHG-12345', source: 'comment', url: 'https://github.com/example-org/deploy/pull/7#issuecomment-1' };
const rmg = { id: 'RMG-678', source: 'body', url: 'https://github.com/example-org/deploy/pull/7' };

test('a ticket shows its id and where it was named — never a state', () => {
    const [part] = ticketParts({ chg: { ...chg, state: 'approved' }, rmg: null }, ['CHG']);
    assert.deepEqual(part, { prefix: 'CHG', id: 'CHG-12345', url: chg.url, missing: false, text: 'CHG-12345' });
    assert.equal(ticketText({ chg: null, rmg }, ['RMG']), 'RMG-678');
});

test('a prefix the PR does not name yet is "no … yet", in grey, in the same place', () => {
    const parts = ticketParts({ chg, rmg: null });
    assert.deepEqual(parts.map((p) => [p.text, p.missing]), [['CHG-12345', false], ['no RMG yet', true]]);
    assert.equal(ticketText({ chg: null, rmg: null }), 'no CHG yet · no RMG yet');
});

test('the line keeps the banner order, CHG then RMG, whatever the object order', () => {
    assert.equal(ticketText({ rmg, chg }), 'CHG-12345 · RMG-678');
    assert.deepEqual(TICKET_PREFIXES, ['CHG', 'RMG']);
});

test('no tickets object, or an error reading the PR, says nothing rather than "no CHG yet"', () => {
    assert.deepEqual(ticketParts(undefined), []);
    assert.deepEqual(ticketParts(null), []);
    assert.deepEqual(ticketParts({ error: 'GithubException: 404' }), []);
    assert.equal(ticketText('not an object'), '');
});

test('whitespace around what the server sent is not part of the ticket', () => {
    assert.equal(ticketText({ chg: { id: ' CHG-1 ' } }, ['CHG']), 'CHG-1');
});

import { releasePrs } from '../../src/release_agent/static/core/tickets.js';

test('releasePrs: every open release PR, else the single blocking_pr, else none', () => {
    const prd = { number: 312, base: 'PRD' }, prl1 = { number: 313, base: 'PRL1' };
    assert.deepEqual(releasePrs({ release_prs: [prd, prl1], blocking_pr: prd }), [prd, prl1]);
    assert.deepEqual(releasePrs({ blocking_pr: prd }), [prd], 'an older server without release_prs');
    assert.deepEqual(releasePrs({ release_prs: [], blocking_pr: null }), []);
    assert.deepEqual(releasePrs(null), []);
});
