// How a PR's CHG / RMG tickets read on the banner (static/core/tickets.js). Run
// by tests/test_ui_js.py through Node's built-in runner — nothing to install.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { TICKET_PREFIXES, ticketParts, ticketText } from '../../src/release_agent/static/core/tickets.js';

const chg = { id: 'CHG-12345', state: 'approved', source: 'comment', url: 'https://github.com/example-org/deploy/pull/7#issuecomment-1' };
const rmg = { id: 'RMG-678', state: '', source: 'body', url: 'https://github.com/example-org/deploy/pull/7' };

test('a ticket shows its id, its state and where it was said', () => {
    const [part] = ticketParts({ chg, rmg: null }, ['CHG']);
    assert.deepEqual(part, { prefix: 'CHG', id: 'CHG-12345', state: 'approved', url: chg.url,
        pending: false, text: 'CHG-12345 approved' });
});

test('a ticket with no state word is just its id', () => {
    assert.equal(ticketText({ chg: null, rmg }, ['RMG']), 'RMG-678');
});

test('a prefix the PR does not name yet is "pending", in grey, in the same place', () => {
    const parts = ticketParts({ chg, rmg: null });
    assert.deepEqual(parts.map((p) => [p.text, p.pending]), [['CHG-12345 approved', false], ['RMG pending', true]]);
    assert.equal(ticketText({ chg: null, rmg: null }), 'CHG pending · RMG pending');
});

test('the line keeps the banner order, CHG then RMG, whatever the object order', () => {
    assert.equal(ticketText({ rmg, chg }), 'CHG-12345 approved · RMG-678');
    assert.deepEqual(TICKET_PREFIXES, ['CHG', 'RMG']);
});

test('no tickets object, or an error reading the PR, says nothing rather than "pending"', () => {
    assert.deepEqual(ticketParts(undefined), []);
    assert.deepEqual(ticketParts(null), []);
    assert.deepEqual(ticketParts({ error: 'GithubException: 404' }), []);
    assert.equal(ticketText('not an object'), '');
});

test('whitespace around what the server sent is not part of the ticket', () => {
    assert.equal(ticketText({ chg: { id: ' CHG-1 ', state: ' created ' } }, ['CHG']), 'CHG-1 created');
});
