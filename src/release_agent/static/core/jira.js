// The JIRA field on Deploy to CARE UAT, Deploy to DF UAT and the CARE/DF
// release forms — shared with the Backstage port, so pure and tested in
// tests/js/jira.test.mjs. Taken as the developer types it (no lookup): the
// backend starts every commit message and PR title of that action with it.

export const JIRA_LABEL = 'JIRA *';
export const JIRA_PLACEHOLDER = 'e.g. ABC-1234 — starts every commit message';

/** The value to send: trimmed, nothing else changed. */
export function cleanJira(value) {
    return String(value == null ? '' : value).trim();
}

/** '' when the field is filled; otherwise the sentence the form shows. */
export function jiraError(value) {
    return cleanJira(value) ? '' : 'JIRA is required — every commit message of this change starts with it.';
}
