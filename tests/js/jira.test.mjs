// The JIRA field every deploy and release form carries (static/core/jira.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { cleanJira, jiraError } from '../../src/release_agent/static/core/jira.js';

test('a JIRA is required, and blank space is not a JIRA', () => {
    assert.match(jiraError(''), /required/);
    assert.match(jiraError('   '), /required/);
    assert.match(jiraError(undefined), /required/);
    assert.equal(jiraError('ABC-1234'), '');
});

test('it is sent as typed, only trimmed — no format is imposed', () => {
    assert.equal(cleanJira('  ABC-1234 '), 'ABC-1234');
    assert.equal(cleanJira('abc-12'), 'abc-12');
});
