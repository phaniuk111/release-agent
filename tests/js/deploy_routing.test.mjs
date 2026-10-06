// Where a typed deploy request goes (static/core/deploy_routing.js) and how the
// chat-box parser reports it (static/forms/parse.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { PROD_IS_RELEASE_ONLY, deployRoute } from '../../src/release_agent/static/core/deploy_routing.js';
import { parseDeployIntent } from '../../src/release_agent/static/forms/parse.js';

test('UAT opens the deploy form', () => {
    assert.deepEqual(deployRoute('uat'), { form: 'uat', releaseOnly: false, message: '' });
});

test('every spelling of production is release-only, with the one sentence', () => {
    for (const env of ['prod', 'prd', 'production', 'PROD']) {
        const r = deployRoute(env);
        assert.equal(r.releaseOnly, true, env);
        assert.equal(r.form, null, env);
        assert.equal(r.message, PROD_IS_RELEASE_ONLY, env);
    }
    assert.match(PROD_IS_RELEASE_ONLY, /not a single-chart deploy/);
});

test('an environment we do not deploy to has no route', () => {
    assert.equal(deployRoute('sit'), null);
    assert.equal(deployRoute(''), null);
    assert.equal(deployRoute(undefined), null);
});

test('"deploy X:1 to uat" still opens the UAT form', () => {
    const di = parseDeployIntent('deploy payments-api:1.2.3 to uat');
    assert.deepEqual(di, {
        env: 'uat', name: 'payments-api', version: '1.2.3',
        form: 'uat', releaseOnly: false, message: '',
    });
});

test('"deploy X:1 to prod" is refused as release-only, never a form', () => {
    for (const text of ['deploy payments-api:1.2.3 to prod',
                        'ship payments-api:1.2.3 to production',
                        'promote payments-api=1.2.3 to PRD']) {
        const di = parseDeployIntent(text);
        assert.equal(di.releaseOnly, true, text);
        assert.equal(di.form, null, text);
        assert.equal(di.message, PROD_IS_RELEASE_ONLY, text);
        assert.equal(di.name, 'payments-api', text);
    }
});

test('text that is not a deploy command is left to the agent', () => {
    assert.equal(parseDeployIntent('what is deployed in prod?'), null);   // no chart:version
    assert.equal(parseDeployIntent('deploy payments-api:1.2.3'), null);   // no environment
    assert.equal(parseDeployIntent('promote the release to prd'), null);  // the pill text
});
