import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
    batchHeadline, budgetLine, confirmQuestion, imagesFor, isDone, previewHeadline, previewTag, resultTag,
} from '../../src/release_agent/static/core/rerun.js';

test('preview tags name the job that will re-run, and every refusal', () => {
    assert.deepEqual(previewTag({ state: 'rerun', job: 'sdlc-peer-review' }),
        { tone: 'info', label: 'will re-run sdlc-peer-review' });
    assert.equal(previewTag({ state: 'protected' }).tone, 'bad');
    assert.equal(previewTag({ state: 'too_old' }).label, 'too old');
    assert.equal(previewTag({ state: 'weird' }).label, 'weird');
    assert.equal(previewTag(null).label, 'unknown');
});

test('result tags', () => {
    assert.deepEqual(resultTag({ result: 'failed_again' }), { tone: 'bad', label: 'failed again' });
    assert.equal(resultTag({ result: 'unfollowed' }).tone, 'warn');
    assert.equal(resultTag({}).label, 'unknown');
});

test('the budget line says how many will start', () => {
    const reset = Date.UTC(2026, 9, 29, 14, 52) / 1000;
    assert.equal(budgetLine({ known: true, needed: 24, fits: 8, remaining: 4000, reset }, 8),
        'About 24 GitHub API calls · 4000 left, resets at 14:52 UTC.');
    assert.match(budgetLine({ known: true, needed: 24, fits: 3, remaining: 19, reset }, 8),
        /Only 3 of 8 will start/);
    assert.match(budgetLine({ known: true, needed: 24, fits: 0, remaining: 5, reset }, 8), /Not enough to start any/);
    assert.match(budgetLine({ known: false, needed: 9, fits: 3 }, 3), /could not be read/);
    assert.equal(budgetLine(null, 3), '');
    assert.equal(budgetLine({ known: true }, 0), '');
});

test('the question and the headline', () => {
    const plan = { plan_id: 'p', job: '1691', runs: [{ job: 'sdlc-peer-review' }, { job: 'sdlc-peer-review' }],
        rows: [{ state: 'rerun', shared: true }, { state: 'rerun', shared: true }, { state: 'rerun' },
               { state: 'too_old' }] };
    assert.equal(confirmQuestion(plan), 'Re-run sdlc-peer-review in 2 runs?');
    assert.equal(confirmQuestion({ ...plan, plan_id: undefined }), '');
    assert.equal(confirmQuestion({ ...plan, runs: [{ job: 'a' }, { job: 'b' }] }), 'Re-run 1691 in 2 runs?');
    assert.equal(previewHeadline(plan), '4 images → 2 re-runs (2 share a run). 1 will not be started.');
    assert.equal(previewHeadline({ rows: [{ state: 'rerun' }], runs: [{}] }), '1 image → 1 re-run.');
});

test('follow-up images are unique and in order', () => {
    const b = { rows: [{ artifact: 'a', result: 'failed_again' }, { artifact: 'b', result: 'passed' },
                       { artifact: 'a', result: 'failed_again' }, { artifact: 'c', result: 'not_started' },
                       { artifact: 'd', result: 'failed_again' }] };
    assert.deepEqual(imagesFor(b, 'failed_again'), ['a', 'd']);
    assert.deepEqual(imagesFor(b, 'not_started'), ['c']);
    assert.deepEqual(imagesFor(null, 'not_started'), []);
});

test('done and the headline', () => {
    assert.equal(isDone({ status: 'finished' }), true);
    assert.equal(isDone({ status: 'stopped' }), true);
    assert.equal(isDone({ status: 'following' }), false);
    assert.equal(batchHeadline({ status: 'starting' }), 'Starting the re-runs…');
    assert.equal(batchHeadline({ status: 'finished', title: 'x finished: 1 passed' }), 'x finished: 1 passed');
});

test('rows group by outcome and reason, problems first in a result', async () => {
    const { groupRows } = await import('../../src/release_agent/static/core/rerun.js');
    const preview = groupRows([
        { artifact: 'a', state: 'rerun', job: 'j' }, { artifact: 'b', state: 'rerun', job: 'j' },
        { artifact: 'c', state: 'protected', detail: 'rebuilds' }, { artifact: 'd', state: 'too_old', detail: 'old' },
        { artifact: 'e', state: 'too_old', detail: 'old' }, { artifact: 'f', state: 'passed', detail: 'passed' },
    ], 'preview');
    assert.deepEqual(preview.map(g => [g.key, g.count, g.quiet]),
        [['rerun', 2, false], ['passed', 1, true], ['too_old', 2, false], ['protected', 1, false]]);
    assert.equal(preview[0].label, 'will re-run j');
    assert.deepEqual(preview[2].reasons.map(r => [r.text, r.rows.length]), [['old', 2]]);
    const result = groupRows([
        { artifact: 'a', result: 'passed', detail: 'ok' }, { artifact: 'b', result: 'not_started', detail: 'limit' },
        { artifact: 'c', result: 'failed_again', detail: 'step x' }, { artifact: 'd', result: 'not_started', detail: 'limit' },
    ], 'result');
    assert.deepEqual(result.map(g => g.key), ['failed_again', 'not_started', 'passed']);
    assert.equal(result[1].reasons[0].rows.length, 2);
    assert.equal(result[2].quiet, true);
    assert.deepEqual(groupRows(null, 'result'), []);
});

test('progress through a batch', async () => {
    const { progressLine } = await import('../../src/release_agent/static/core/rerun.js');
    assert.equal(progressLine({ status: 'starting', runs: [{ state: 'started' }, { state: 'pending' }] }), '1 of 2 sent');
    assert.equal(progressLine({ status: 'following', runs: [{ state: 'passed' }, { state: 'started' }] }), '1 of 2 runs settled');
    assert.equal(progressLine({ runs: [] }), '');
});
