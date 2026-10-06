// The release-history rules both UIs share (static/core/history.js). Run by
// tests/test_ui_js.py through Node's built-in runner — nothing to install.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
    filterReleases, itemKind, itemState, releaseSummary, requeueAllIndices,
} from '../../src/release_agent/static/core/history.js';

const RUN = 'https://github.com/org/build/actions/runs/123456';

// A never-queued row by default: typed straight into a release form.
const item = (over = {}) => ({
    artifact_name: 'chart', artifact_version: '1.0.0', jira_ticket: '', build_run_url: '',
    prl1_only: false, df_only: false, target_envs: 'prd,prl1', change_details: '', note: '',
    queued_by: '', in_queue: false, from_queue: false, requeueable: false, ...over,
});

const RELEASES = [
    { release_name: 'CARE-42', pr_number: 7, items: [
        item({ artifact_name: 'payments-api', artifact_version: '2.3.1', jira_ticket: 'PAY-101', from_queue: true }),
        item({ artifact_name: 'ledger', artifact_version: '5.0.0', jira_ticket: 'LED-9', in_queue: true, from_queue: true }),
        item({ artifact_name: 'fx-rates', artifact_version: '0.9.0' }),
    ] },
    { release_name: 'DF-7', pr_number: 8, items: [
        item({ artifact_name: 'df-ingest', artifact_version: '3.1.0', jira_ticket: 'ING-5', df_only: true, from_queue: true }),
    ] },
    { release_name: 'CARE-41', pr_number: 6, items: [
        item({ artifact_name: 'payments-ui', artifact_version: '1.2.3', jira_ticket: 'PAY-77',
               build_run_url: RUN }),
    ] },
];

// Only the original positions: what the tick keys ("ri:ii") are built from.
const positions = (shown) => shown.map(r => [r.index, r.items.map(x => x.index)]);

test('a Dataflow image is DF, everything else CARE', () => {
    assert.equal(itemKind(item({ df_only: true })), 'DF');
    assert.equal(itemKind(item({ df_only: false })), 'CARE');
    assert.equal(itemKind({}), 'CARE');
});

test('a row already in the next release is in-queue, whatever else it has', () => {
    assert.equal(itemState(item({ in_queue: true, from_queue: true })), 'in-queue');
    assert.equal(itemState(item({ in_queue: true, build_run_url: RUN, jira_ticket: 'X-1' })), 'in-queue');
});

test('a row that qualified once goes straight back — no run or ticket needed here', () => {
    assert.equal(itemState(item({ from_queue: true })), 'ready');
});

test('a never-queued row is ready only with a real run URL AND a ticket', () => {
    assert.equal(itemState(item({ build_run_url: RUN, jira_ticket: 'PAY-1' })), 'ready');
    // Pasted with surrounding whitespace still counts.
    assert.equal(itemState(item({ build_run_url: '  ' + RUN + '  ', jira_ticket: 'PAY-1' })), 'ready');
    // Missing the ticket, the run, or both.
    assert.equal(itemState(item({ build_run_url: RUN })), 'needs-input');
    assert.equal(itemState(item({ build_run_url: RUN, jira_ticket: '   ' })), 'needs-input');
    assert.equal(itemState(item({ jira_ticket: 'PAY-1' })), 'needs-input');
    assert.equal(itemState(item()), 'needs-input');
    // A URL the gate cannot read a run from is as good as none.
    for (const bad of [
        'https://github.com/org/build/actions/runs/',          // no run id
        'https://github.com/org/build/actions/runs/latest',    // not an id
        'https://github.com/org/build/actions',                // not a run
        'github.com/org/build/actions/runs/123',               // no scheme
        'ftp://github.com/org/build/actions/runs/123',
        'see https://github.com/org/build/actions/runs/123',   // not the start
    ]) {
        assert.equal(itemState(item({ build_run_url: bad, jira_ticket: 'PAY-1' })), 'needs-input', bad);
    }
    // A job or attempt link under the run is still that run.
    assert.equal(itemState(item({ build_run_url: RUN + '/job/99', jira_ticket: 'PAY-1' })), 'ready');
});

test('with no query and every kind, every release and row is shown, in order', () => {
    const shown = filterReleases(RELEASES);
    assert.deepEqual(positions(shown), [[0, [0, 1, 2]], [1, [0]], [2, [0]]]);
    assert.equal(shown[0].rel, RELEASES[0]);
    assert.equal(shown[0].items[2].it, RELEASES[0].items[2]);
    assert.deepEqual(positions(filterReleases(RELEASES, { query: '   ', kind: 'all' })), positions(shown));
});

test('the query finds a row by chart name, keeping its original positions', () => {
    assert.deepEqual(positions(filterReleases(RELEASES, { query: 'fx-rates' })), [[0, [2]]]);
    // A substring matches across releases; the one with no match is dropped.
    assert.deepEqual(positions(filterReleases(RELEASES, { query: 'payments' })), [[0, [0]], [2, [0]]]);
});

test('the query finds a row by version and by JIRA key', () => {
    assert.deepEqual(positions(filterReleases(RELEASES, { query: '3.1.0' })), [[1, [0]]]);
    assert.deepEqual(positions(filterReleases(RELEASES, { query: 'LED-9' })), [[0, [1]]]);
    assert.deepEqual(positions(filterReleases(RELEASES, { query: 'PAY-' })), [[0, [0]], [2, [0]]]);
});

test('the query ignores case and surrounding whitespace', () => {
    assert.deepEqual(positions(filterReleases(RELEASES, { query: '  Led-9 ' })), [[0, [1]]]);
    assert.deepEqual(positions(filterReleases(RELEASES, { query: 'PAYMENTS-UI' })), [[2, [0]]]);
});

test('the query does not match the release name or anything outside the three fields', () => {
    assert.deepEqual(filterReleases(RELEASES, { query: 'CARE-42' }), []);
    assert.deepEqual(filterReleases(RELEASES, { query: 'prd,prl1' }), []);
});

test('the kind filter keeps only CARE charts or only DF images', () => {
    assert.deepEqual(positions(filterReleases(RELEASES, { kind: 'DF' })), [[1, [0]]]);
    assert.deepEqual(positions(filterReleases(RELEASES, { kind: 'CARE' })), [[0, [0, 1, 2]], [2, [0]]]);
    assert.deepEqual(positions(filterReleases(RELEASES, { kind: 'all' })), [[0, [0, 1, 2]], [1, [0]], [2, [0]]]);
});

test('query and kind combine; nothing left means nothing shown', () => {
    assert.deepEqual(positions(filterReleases(RELEASES, { query: 'ing', kind: 'DF' })), [[1, [0]]]);
    assert.deepEqual(filterReleases(RELEASES, { query: 'df-ingest', kind: 'CARE' }), []);
    assert.deepEqual(filterReleases(RELEASES, { query: 'no-such-chart' }), []);
});

test('a release with no rows, or no releases at all, shows nothing', () => {
    assert.deepEqual(filterReleases([{ release_name: 'empty', items: [] }]), []);
    assert.deepEqual(filterReleases([]), []);
    assert.deepEqual(filterReleases(undefined), []);
});

test('"Re-queue all" ticks only the ready rows, never in-queue or needs-input', () => {
    // payments-api (qualified once) yes; ledger (already queued) and fx-rates (nothing on record) no.
    assert.deepEqual(requeueAllIndices(RELEASES[0]), [0]);
    assert.deepEqual(requeueAllIndices(RELEASES[2]), [0]);
    assert.deepEqual(requeueAllIndices({ items: [
        item({ build_run_url: RUN }),                          // no ticket
        item({ from_queue: true }),
        item({ jira_ticket: 'X-1' }),                          // no run
        item({ build_run_url: RUN, jira_ticket: 'X-1' }),
        item({ in_queue: true, from_queue: true }),
    ] }), [1, 3]);
    assert.deepEqual(requeueAllIndices({ items: [] }), []);
    assert.deepEqual(requeueAllIndices(undefined), []);
});

test('the release summary counts each state and leaves out the empty ones', () => {
    assert.equal(releaseSummary({ items: [
        item({ from_queue: true }), item({ from_queue: true }), item({ in_queue: true }),
    ] }), '3 charts · 2 ready · 1 in next release');
    assert.equal(releaseSummary(RELEASES[0]), '3 charts · 1 ready · 1 in next release · 1 needs run + ticket');
    assert.equal(releaseSummary({ items: [
        item({ from_queue: true }), item({ build_run_url: RUN }), item({ build_run_url: RUN }),
    ] }), '3 charts · 1 ready · 2 need ticket');
    assert.equal(releaseSummary({ items: [item({ from_queue: true }), item({ jira_ticket: 'X-1' })] }),
        '2 charts · 1 ready · 1 needs run');
});

test('when one state covers every chart, the summary does not repeat the count', () => {
    assert.equal(releaseSummary({ items: [item()] }), '1 chart · needs run + ticket');
    assert.equal(releaseSummary({ items: [item({ from_queue: true })] }), '1 chart · ready');
    assert.equal(releaseSummary({ items: [item({ in_queue: true })] }), '1 chart · in next release');
    assert.equal(releaseSummary({ items: [item({ from_queue: true }), item({ from_queue: true })] }),
        '2 charts · all ready');
    assert.equal(releaseSummary({ items: [item({ jira_ticket: 'X-1' }), item({ build_run_url: RUN })] }),
        '2 charts · all need run + ticket');
});

test('a release with no charts says so', () => {
    assert.equal(releaseSummary({ items: [] }), '0 charts');
    assert.equal(releaseSummary(undefined), '0 charts');
});

import { missingInputs, safeRunHref, withTyped } from '../../src/release_agent/static/core/history.js';

test('Re-queue all under a filter ticks only the rows the filter shows', () => {
    const rel = { items: [
        { artifact_name: 'a', from_queue: true },
        { artifact_name: 'b', from_queue: true },
        { artifact_name: 'c', in_queue: true },
    ] };
    assert.deepEqual(requeueAllIndices(rel), [0, 1]);
    assert.deepEqual(requeueAllIndices(rel, [1, 2]), [1], 'row 0 is hidden, so it is not ticked');
    assert.deepEqual(requeueAllIndices(rel, []), []);
});

test('missingInputs names what the gate still needs, only for a never-queued row', () => {
    const run = 'https://github.com/o/r/actions/runs/123';
    assert.deepEqual(missingInputs({}), ['run', 'ticket']);
    assert.deepEqual(missingInputs({ build_run_url: run }), ['ticket']);
    assert.deepEqual(missingInputs({ jira_ticket: 'ABC-1' }), ['run']);
    assert.deepEqual(missingInputs({ build_run_url: 'https://x/not-a-run', jira_ticket: 'ABC-1' }), ['run']);
    assert.deepEqual(missingInputs({ build_run_url: run, jira_ticket: 'ABC-1' }), []);
    assert.deepEqual(missingInputs({ from_queue: true }), []);
    assert.deepEqual(missingInputs({ in_queue: true }), []);
});

test('withTyped lays typed values over the record: trimmed, the ticket upper-cased, untyped fields kept', () => {
    const rec = { artifact_name: 'a', build_run_url: 'old', jira_ticket: 'OLD-1' };
    assert.deepEqual(withTyped(rec, { run: '  https://x/actions/runs/9 ', jira: ' abc-2 ' }),
        { artifact_name: 'a', build_run_url: 'https://x/actions/runs/9', jira_ticket: 'ABC-2' });
    assert.deepEqual(withTyped(rec, { jira: 'x-1' }), { artifact_name: 'a', build_run_url: 'old', jira_ticket: 'X-1' });
    assert.deepEqual(withTyped(rec, undefined), rec);
    assert.notEqual(withTyped(rec, {}), rec, 'a copy, never the record itself');
});

test('only an http(s) run URL becomes a link', () => {
    assert.equal(safeRunHref(' https://github.com/o/r/actions/runs/1 '), 'https://github.com/o/r/actions/runs/1');
    assert.equal(safeRunHref('javascript:alert(1)'), '');
    assert.equal(safeRunHref('JaVaScRiPt:alert(1)'), '');
    assert.equal(safeRunHref('data:text/html,x'), '');
    assert.equal(safeRunHref(''), '');
});
