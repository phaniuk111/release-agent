// The release-queue rules both UIs share (static/core/queue.js). Run by
// tests/test_ui_js.py through Node's built-in runner — nothing to install.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
    batchRow, buildSummary, envsOf, forRelease, previousTag, queueDestination, queueSubmissionProblems,
    releaseRouteText, tickHint, tickProblem, ticksToFlags,
} from '../../src/release_agent/static/core/queue.js';
import { requeuePlan } from '../../src/release_agent/static/core/queue.js';

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

test('the build badge reads the same on every screen, and unknown is not a failure', () => {
    assert.deepEqual(buildSummary({ build_verified: true }).state, 'verified');
    assert.equal(buildSummary({ build_verified: true }).label, 'verified');
    assert.ok(buildSummary({ build_verified: true }).title.includes('built this version'));
    assert.equal(buildSummary({ build_verified: false }).state, 'unverified');
    assert.equal(buildSummary({ build_verified: false }).label, 'not verified');
    // Never checked is its own state — the release form shows no badge for it.
    assert.equal(buildSummary({ build_verified: null }).state, 'unknown');
    assert.equal(buildSummary({}).state, 'unknown');
    assert.equal(buildSummary(undefined).label, 'not checked');
});

test('the Controls column shows an allowed control as open, by number, else all passed', async () => {
    const { controlsSummary } = await import('../../src/release_agent/static/core/queue.js');
    const failed = controlsSummary({ build_verified: true,
        allowed_failures: 'RCTLDEF0001691 - Peer review evidence in job build-deploy-publish' });
    assert.equal(failed.state, 'open');
    assert.equal(failed.label, '1691 open');
    assert.ok(failed.title.includes('Peer review evidence') && failed.title.includes('close it manually'));
    assert.equal(controlsSummary({ build_verified: true, allowed_failures: 'RCTLDEF0001691, RCTLDEF0000043 in job b' }).label,
        '1691, 43 open');
    assert.equal(controlsSummary({ build_verified: true }).label, 'all passed');
    assert.equal(controlsSummary({ build_verified: null }).label, 'not checked');
});

test('history ticks: a chart that qualified goes back directly; one that never queued takes the gate, once it has run + ticket', () => {
    const { direct, gated, skipped } = requeuePlan([
        { artifact_name: 'a', artifact_version: '1.0.0', from_queue: true, build_run_url: 'https://x/run/1', jira_ticket: 'ABC-1' },
        { artifact_name: 'b', artifact_version: '2.0.0', from_queue: false, build_run_url: '' },
        { artifact_name: 'c', artifact_version: '3.0.0', from_queue: true, in_queue: true },
        { artifact_name: 'd', artifact_version: '4.0.0', from_queue: false, build_run_url: 'https://x/run/4' },
        { artifact_name: 'e', artifact_version: '5.0.0', from_queue: false, build_run_url: 'https://x/run/5', jira_ticket: 'ABC-5',
          prl1_only: true, target_envs: 'prl1', change_details: 'typed in', note: 'n' },
    ]);
    assert.deepEqual(direct, [{ artifact_name: 'a', artifact_version: '1.0.0' }]);
    assert.deepEqual(gated, [{ artifact: 'e:5.0.0', build_run_url: 'https://x/run/5', jira_ticket: 'ABC-5',
                               prl1_only: true, df_only: false, target_envs: 'prl1', change_details: 'typed in', note: 'n' }]);
    assert.deepEqual(skipped.map(s => s.artifact), ['b:2.0.0', 'c:3.0.0', 'd:4.0.0']);
    assert.match(skipped[0].reason, /paste the run/);
    assert.match(skipped[1].reason, /already queued/);
    assert.match(skipped[2].reason, /JIRA ticket/);
    assert.deepEqual(requeuePlan([]), { direct: [], gated: [], skipped: [] });
});

import { artifactName, dataflowFields } from '../../src/release_agent/static/core/queue.js';

test('an artifact line names its service, typed or as a registry URL', () => {
    assert.equal(artifactName('order-enrichment:1.4.2'), 'order-enrichment');
    assert.equal(artifactName('https://artifactory.example.com/docker/example-ds/df-agg:2.1.0/'), 'df-agg');
    assert.equal(artifactName('no-version-here'), null);
    assert.equal(artifactName(''), null);
});

test('the Dataflow block of a CARE release: queued choice first, the form box for typed images', () => {
    const queued = { 'df-agg': { prl1_only: true }, 'df-pnl': { prl1_only: false } };
    const out = dataflowFields(
        ['https://artifactory.example.com/docker/example-ds/df-agg:0.0.561', 'df-pnl:1.2.0', ' df-new:3.0.0 ', ''],
        queued, new Set(['df-new', 'df-pnl']));
    assert.deepEqual(out.dataflow_artefact, [
        'https://artifactory.example.com/docker/example-ds/df-agg:0.0.561', 'df-pnl:1.2.0', 'df-new:3.0.0']);
    // df-pnl is queued for PRD too, so the stray manual tick does not make it PRL1-only.
    assert.deepEqual(out.dataflow_prl1_only, ['df-agg', 'df-new']);
});

test('no Dataflow lines means an empty block', () => {
    assert.deepEqual(dataflowFields([], {}, new Set()), { dataflow_artefact: [], dataflow_prl1_only: [] });
});

test('the tag a build started from is shown only when it was recorded', () => {
    assert.equal(previousTag({ previous_tag: ' 5.0.989 ' }).text, 'previous 5.0.989');
    assert.match(previousTag({ previous_tag: '5.0.989' }).title, /previous release tags file/);
    assert.equal(previousTag({ previous_tag: '' }), null);
    assert.equal(previousTag(null), null);
});

import { dfChoices } from '../../src/release_agent/static/core/queue.js';

// A DF release moves its images out of the queue when its PR is raised; the
// CARE form must still offer them for the release file's Dataflow keys.
const DFQ = (name, version, extra) => Object.assign(
    { artifact_name: name, artifact_version: version, df_only: true }, extra || {});
const careRel = (at) => ({ release_name: 'CARE ' + at, pr_number: 1, released_at: at,
    items: [{ artifact_name: 'svc-a', artifact_version: '1.0.0', df_only: false }] });
const dfRel = (at, items, pr) => ({ release_name: 'DF ' + at, pr_number: pr || 9, released_at: at, items });

test('dfChoices ticks a DF release raised after the last CARE release', () => {
    const { rows } = dfChoices([], [dfRel('2026-11-03', [DFQ('df-agg', '2.1.0', { prl1_only: true })]),
                                    careRel('2026-10-27')], { artefact: [] });
    assert.deepEqual(rows.map(r => [r.line, r.source, r.ticked, r.prl1_only, r.release_name, r.pr_number]),
        [['df-agg:2.1.0', 'released', true, true, 'DF 2026-11-03', 9]]);
});

test('dfChoices leaves a DF release from before the last CARE release unticked, saying why', () => {
    const { rows } = dfChoices([], [careRel('2026-11-03'), dfRel('2026-10-30', [DFQ('df-agg', '2.0.0')])], null);
    assert.equal(rows[0].ticked, false);
    assert.match(rows[0].note, /before the last CARE release/);
});

test('dfChoices leaves unticked what the release file already carries (full URL compared by name:version)', () => {
    const cur = { artefact: ['https://artifactory.example.com/docker/df-agg:2.1.0'] };
    const { rows } = dfChoices([DFQ('df-q', '1.0.0')],
        [dfRel('2026-11-03', [DFQ('df-agg', '2.1.0'), DFQ('df-new', '3.0.0')])],
        { artefact: cur.artefact.concat(['df-q:1.0.0']) });
    const by = Object.fromEntries(rows.map(r => [r.name, r]));
    assert.equal(by['df-agg'].ticked, false);
    assert.match(by['df-agg'].note, /already in the release file/);
    assert.equal(by['df-new'].ticked, true);
    assert.equal(by['df-q'].ticked, false);         // queued, but the file already names it
});

test('dfChoices offers each image once from history (the newest release) and skips a re-queued one', () => {
    const { rows } = dfChoices([], [
        dfRel('2026-11-03', [DFQ('df-agg', '2.1.0')], 12),
        dfRel('2026-10-20', [DFQ('df-agg', '2.0.0'), DFQ('df-back', '1.0.0', { in_queue: true })], 11),
    ], { artefact: [] });
    assert.deepEqual(rows.map(r => r.line), ['df-agg:2.1.0']);
});

test('dfChoices: a ticked released version wins over the queued one of the same image', () => {
    const { rows } = dfChoices([DFQ('df-agg', '2.2.0')], [dfRel('2026-11-03', [DFQ('df-agg', '2.1.0')])],
        { artefact: [] });
    const q = rows.find(r => r.source === 'queued');
    const rel = rows.find(r => r.source === 'released');
    assert.equal(rel.ticked, true);
    assert.equal(q.ticked, false);
    assert.match(q.note, /released version of it is ticked/);
});

test('dfChoices keeps queued DF images ticked when no DF release happened (today\'s behaviour)', () => {
    const { rows } = dfChoices([DFQ('df-agg', '2.2.0'), { artifact_name: 'svc-a', artifact_version: '1' }],
        [careRel('2026-10-27')], null);
    assert.deepEqual(rows.map(r => [r.line, r.ticked]), [['df-agg:2.2.0', true]]);
});

test('dfChoices with no history and no file read still offers the queue', () => {
    assert.deepEqual(dfChoices(null, null, null), { rows: [], lastCareAt: '' });
});
