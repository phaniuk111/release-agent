// The release-notification card's wording (static/core/deploy_tracking.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { checkLine, overall, prHeading, prStory, sentLine, waitingLine }
    from '../../src/release_agent/static/core/deploy_tracking.js';

test('a check reads ok, warning or problem', () => {
    assert.equal(checkLine({ name: 'a', ok: true, detail: 'x' }).state, 'ok');
    assert.equal(checkLine({ name: 'a', ok: false, detail: 'x' }).state, 'problem');
    assert.equal(checkLine({ name: 'a', ok: null, detail: 'x' }).state, 'warn');
});

test('a dispatch is sent, failed or skipped — never silent', () => {
    assert.deepEqual(sentLine('Start', { run_id: 5, run_url: 'u' }), { state: 'ok', text: 'Start sent', url: 'u' });
    assert.match(sentLine('Start', { error: 'HTTP 404' }).text, /NOT sent.*404/);
    assert.equal(sentLine('Start', { skipped: 'NOTIFY_WORKFLOW is not set' }).state, 'warn');
    assert.equal(sentLine('Start', {}).state, 'warn');
    assert.equal(sentLine('Start', null), null);
});

test('waiting names the runs on the merge commit and whether any counts', () => {
    const e = { merge_sha: 'abcdef123', runs_now: [] };
    assert.match(waitingLine(e).text, /No workflow run on the merge commit abcdef1/);
    e.runs_now = [{ name: 'Lint', workflow: 'lint.yml', counts: false, conclusion: 'success' }];
    const none = waitingLine(e);
    assert.equal(none.state, 'problem');
    assert.match(none.text, /DEPLOY_WORKFLOWS.*lint\.yml/);
    e.runs_now.push({ name: 'Deploy', workflow: 'deploy.yml', counts: true, conclusion: '', url: 'r' });
    assert.match(waitingLine(e).text, /1 of 1 run not finished \(Deploy\)/);
    assert.equal(waitingLine({ runs_error: 'boom' }).state, 'warn');
});

test('a PR the tracker never saw is the reason nothing was sent', () => {
    const lines = prStory({ state: 'unseen' });
    assert.equal(lines[0].state, 'problem');
    assert.match(lines[0].text, /never saw this PR open/);
});

test('a finished PR says Start, the outcome and Complete; a failure sends nothing', () => {
    const ok = prStory({ state: 'finished', start: { run_id: 1 }, complete: { run_id: 2 },
                         steps: [{ type: 'finished', conclusion: 'success', workflow: 'd.yml' }] });
    assert.deepEqual(ok.map(l => l.text), ['Start sent', 'Deployment succeeded (d.yml)', 'Complete sent']);
    const bad = prStory({ state: 'finished', start: { run_id: 1 },
                          steps: [{ type: 'finished', conclusion: 'failure',
                                    failed_steps: [{ job: 'deploy', step: 'helm' }] }] });
    assert.match(bad[1].text, /failed at deploy › helm — a failure sends no notification/);
    assert.equal(prStory({ state: 'watching' })[0].text, 'Open — Start is sent when it merges');
});

test('the headline is the first failing check, then the loop error, then a PR', () => {
    assert.equal(overall({ ok: true, checks: [{ name: 'Tracking switched on', ok: false, detail: 'off' }] }).text,
        'Tracking switched on: off');
    assert.match(overall({ ok: true, checks: [], last_error: 'x' }).text, /last check failed/);
    assert.equal(overall({ ok: true, checks: [], prs: [{ state: 'unseen' }] }).state, 'warn');
    assert.equal(overall({ ok: true, checks: [{ ok: true }], prs: [] }).state, 'ok');
    assert.equal(overall(null).state, 'problem');
});

test('a PR is named by environment and tickets', () => {
    assert.equal(prHeading({ env: 'PRL1', chg: 'CHG0099001', rmg: 'RMG1', pr_number: 7 }),
        'PRL1 · CHG0099001 · RMG1 — PR #7');
    assert.equal(prHeading({ pr_number: 7 }), 'PR #7');
});
