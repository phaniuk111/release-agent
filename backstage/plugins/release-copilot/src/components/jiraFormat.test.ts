import {
  cleanJira,
  JIRA_LABEL,
  JIRA_PLACEHOLDER,
  jiraError,
} from './jiraFormat';

describe('the JIRA field every deploy and release form carries', () => {
  it('is required, and blank space is not a JIRA', () => {
    expect(jiraError('')).toMatch(/required/);
    expect(jiraError('   ')).toMatch(/required/);
    expect(jiraError(undefined)).toMatch(/required/);
    expect(jiraError(null)).toMatch(/required/);
    expect(jiraError('ABC-1234')).toBe('');
  });

  it('says the same sentence as the portal', () => {
    expect(jiraError('')).toBe(
      'JIRA is required — every commit message of this change starts with it.',
    );
    expect(JIRA_LABEL).toBe('JIRA *');
    expect(JIRA_PLACEHOLDER).toBe(
      'e.g. ABC-1234 — starts every commit message',
    );
  });

  it('is sent as typed, only trimmed — no format is imposed', () => {
    expect(cleanJira('  ABC-1234 ')).toBe('ABC-1234');
    expect(cleanJira('abc-12')).toBe('abc-12');
  });
});
