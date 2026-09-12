// The release-queue rules both UIs share (static/core/queue.js). Run by
// tests/test_ui_js.py through Node's built-in runner — nothing to install.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
    batchRow, envsOf, forRelease, queueDestination, queueSubmissionProblems,
    releaseRouteText, tickHint, tickProblem, ticksToFlags,
} from '../../src/release_agent/static/core/queue.js';

test('ticks map onto the fields the API stores', () => {
    assert.deepEqual(ticksToFlags({ df: false, prd: true, prl1: true }),
        { df_only: false, prl1_only: false, target_envs: 'prd,prl1' });
    // Only PRL1 WITHOUT PRD restricts a chart.
    assert.deepEqual(ticksToFlags({ df: false, prd: false, prl1: true }),
        { df_only: false, prl1_only: true, target_envs: 'prl1' });
    assert.deepEqual(ticksToFlags({ df: false, prd: true, prl1: false }),
        { df_only: false, prl1_only: false, target_envs: 'prd' });
    assert.deepEqual(ticksToFlags({ df: true, prd: true, prl1: true }),
        { df_only: true, prl1_only: false, target_envs: 'prd,prl1' });
});

test('a CARE chart must go somewhere; a DF image need not name a pipeline yet', () => {
    assert.equal(tickProblem({ df: false, prd: false, prl1: false }), 'Tick where it goes: PRD or PRL1.');
    assert.equal(tickProblem({ df: true, prd: false, prl1: false }), '');
    assert.equal(tickProblem({ df: false, prd: true, prl1: false }), '');
});

test('the hint says where the ticks send it — PRD alone does not hold CARE out of PRL1', () => {
    assert.equal(tickHint({ df: false, prd: true, prl1: true }), 'UAT → PRL1 → PRD');
    assert.equal(tickHint({ df: false, prd: true, prl1: false }), 'PRD pipeline — release files still cover PRL1');
    assert.equal(tickHint({ df: false, prd: false, prl1: true }), 'UAT → PRL1, never PRD');
    assert.equal(tickHint({ df: true, prd: true, prl1: true }), 'PRD + PRL1 pipelines — triggered at deploy time');
    assert.equal(tickHint({ df: true, prd: false, prl1: true }), 'PRL1 pipeline — triggered at deploy time');
    assert.equal(tickHint({ df: true, prd: false, prl1: false }), '');
});

test('a queued row reads the same as in the Backstage queue tab', () => {
    assert.equal(queueDestination({ df_only: false, prl1_only: false }), 'CARE → UAT, PRL1, PRD');
    assert.equal(queueDestination({ df_only: false, prl1_only: true }), 'CARE → UAT, PRL1');
    assert.equal(queueDestination({ df_only: true, target_envs: 'prl1, prd' }), 'DF → PRD, PRL1');
    assert.equal(queueDestination({ df_only: true, target_envs: '' }), 'DF (Dataflow)');
});

test('inside a release form, routing is worded for that release', () => {
    assert.equal(releaseRouteText({ prl1_only: true }, false), 'UAT → PRL1 · PRL1-only, held back from PRD');
    assert.equal(releaseRouteText({ prl1_only: false }, false), 'UAT → PRL1 → PRD');
    assert.equal(releaseRouteText({ target_envs: 'prd' }, true), 'PRD pipeline — triggered at deploy time');
    assert.equal(releaseRouteText({ target_envs: '' }, true), 'pipelines chosen at deploy time');
    assert.deepEqual(envsOf({ target_envs: ' prd ,, prl1' }), ['prd', 'prl1']);
});

test('each release carries only its own kind', () => {
    const q = [{ artifact_name: 'a', df_only: false }, { artifact_name: 'b', df_only: true }];
    assert.deepEqual(forRelease(q, true).map(x => x.artifact_name), ['b']);
    assert.deepEqual(forRelease(q, false).map(x => x.artifact_name), ['a']);
    assert.deepEqual(forRelease(undefined, false), []);
});

const CARE = { df: false, prd: true, prl1: true };
const row = (over) => ({ chart: 'orders-api', version: '1.0.0', jira: 'REL-1',
                         run: 'https://github.com/o/r/actions/runs/1', ...over });

test('a complete submission has no problems and empty rows are dropped', () => {
    const { filled, problems } = queueSubmissionProblems(
        [row(), { chart: ' ', version: '', run: '', jira: '' }],
        { email: 'dev@example.com', details: 'fixes it', ticks: CARE });
    assert.deepEqual(problems, []);
    assert.equal(filled.length, 1);
});

test('every problem is reported at once, named by chart', () => {
    const { problems } = queueSubmissionProblems(
        [row({ jira: '' }), row({ chart: '', run: 'not-a-run' })],
        { email: '', details: 'x', ticks: { df: false, prd: false, prl1: false } });
    assert.deepEqual(problems, [
        'orders-api: JIRA ticket',
        'row 2: chart name, build run URL (…/actions/runs/<id>)',
        'Still needed: your email',
        'Tick where it goes: PRD or PRL1.',
    ]);
});

test('nothing typed at all asks for a chart', () => {
    assert.deepEqual(queueSubmissionProblems([], { email: 'a@b', details: 'x', ticks: CARE }).problems,
        ['Add at least one chart.']);
});

test('a batch row carries the artifact, its run and the routing', () => {
    assert.deepEqual(batchRow(row(), { df: false, prd: false, prl1: true }), {
        artifact: 'orders-api:1.0.0', build_run_url: 'https://github.com/o/r/actions/runs/1',
        jira_ticket: 'REL-1', df_only: false, prl1_only: true, target_envs: 'prl1',
    });
});
